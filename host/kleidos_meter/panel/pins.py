# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's pin map: staged, applied, and the relays checked after a change that moved one.

The map is the instrument's (``SYSTem:PINS``): it stages fields, judges the
staged map, and makes it the live one in one checked change that proves the
INA228 on its new pads or puts the last good map back. :class:`PinChanges`
carries the panel's side of it:

- **The table.** The page draws the board from the package's pin table
  (:data:`kleidos_meter.pins.BOARD`); the instrument names the digest of its
  own (``table``). While the two differ the page would judge by rules the
  instrument does not have, so staging and applying are refused; the
  defaults, which are the instrument's own, are not.
- **When it may change.** An order is refused, unsent, while a sequence or a
  capture runs or a window is open, while another controller holds the line
  and while a relay is not at rest. The instrument refuses the last and the
  windows too; here each has its own word for the page.
- **The check.** A relay pad has no proof but its level, so after a change
  that moved a relay (its GPIO or its active level) the map reads
  ``verified=0`` until a null-path check passes on it. The panel makes that
  check itself, ``ROUTe:NULLpath ON`` then ``OFF``, with no ``FORCe`` and no
  ``LIVE``, and reports it; ``OFF`` is sent whatever ``ON`` answered.

While an order and its check run, :attr:`PinChanges.running` refuses every
other change (``409``) and a passthrough client. The map is never
snapshotted and never written back (:data:`~.schema.PIN_ORDERS`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .. import pins
from ..client import MeterError
from ..protocol import parse_fields
from .executor import pack_units
from .reports import errors_json, report_json, status_for, typed_fields
from .schema import PIN_READBACK, RELAY_CHECK
from .settings import SettingsRefused

if TYPE_CHECKING:  # the types are for the checker only
    from .recorder import Recorder
    from .sequences import SequenceRunner
    from .serve import Session

__all__ = ["CHANGING", "PinChanges"]

#: Why a change is refused while an order and its check run.
CHANGING = "a pin map is being applied and checked: wait for its end"

#: The keys of a map a null-path check depends on.
_RELAY_KEYS = ("relay1", "relay1_active", "relay2", "relay2_active")


class PinChanges:
    """Stage and apply the pin map on a session's executor: see the module's description.

    :param session: Serve's session: its executor carries every message, its
        shaping guard adopts the bus clock of a map that was applied.
    :param recorder: The recorder whose captures an order must not cut; none runs without one.
    :param sequences: The sequence runner, likewise.
    :param holder: Who holds the line, None while the panel does.
    """

    def __init__(
        self,
        session: Session,
        *,
        recorder: Recorder | None = None,
        sequences: SequenceRunner | None = None,
        holder: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.session = session
        self.executor = session.executor
        self.recorder = recorder
        self.sequences = sequences
        self.holder = holder
        self._lock = threading.Lock()
        self._closed = False
        self._idle = threading.Event()
        self._idle.set()

    @property
    def running(self) -> bool:
        """Whether an order, or the check after it, is being made."""
        return not self._idle.is_set()

    def close(self, timeout: float = 60.0) -> None:
        """Take no further order, and wait for the one that runs to put the relays back.

        :param timeout: Seconds of real time to wait.
        """
        with self._lock:
            self._closed = True
        self._idle.wait(timeout)

    def state(self) -> dict[str, Any]:
        """The board, the live and the staged map, and the last order's result.

        :returns: ``GET /api/pins``'s body.
        :raises MeterError: When a query is refused or unanswered.
        """
        live, pending, result = self.executor.query_all(list(PIN_READBACK))
        running, staged = parse_fields(live), parse_fields(pending)
        return {
            "board": pins.BOARD,
            "table": {
                "package": pins.DIGEST,
                "instrument": running["table"],
                "matches": running["table"] == pins.DIGEST,
            },
            "live": report_json(live),
            "pending": report_json(pending),
            "result": report_json(result),
            "differs": [key for key in pins.KEYS if running[key] != staged[key]],
        }

    def stage(self, order: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Stage fields of the map, over the staged map or (``clear``) the live one.

        :param order: Fields of :data:`~kleidos_meter.pins.KEYS`, and ``clear``.
        :returns: The HTTP status and the body: the units sent, the errors and
            the staged map with its verdict.
        :raises SettingsRefused: 400 for an unknown field or a value of the
            wrong form, 409 when the instrument's table is another; nothing is sent.
        :raises MeterError: When the line fails.
        """
        clear, changes = _changes(order)
        live, staged = (
            parse_fields(answer) for answer in self.executor.query_all(list(PIN_READBACK[:2]))
        )
        _same_table(live)
        base = pins.PinMap.from_words(live if clear else staged)
        units = (["SYST:PINS:CLE"] if clear else []) + pins.stage_units(base, changes)
        errors = []
        for message in pack_units(units):
            errors += self.executor.call(message).errors
        pending = self.executor.query_all(["SYST:PINS:PEND?"])[0]
        return status_for(entry.code for entry in errors), {
            "sent": units,
            "errors": errors_json(errors),
            "pending": report_json(pending),
        }

    def apply(self, *, defaults: bool = False) -> tuple[int, dict[str, Any]]:
        """Make the staged map the live one, or with *defaults* the rig's own.

        :param defaults: Send ``SYSTem:PINS:DEFault`` instead of ``APPLy``.
        :returns: The HTTP status (:func:`~.reports.status_for` of what the
            order queued: 409 for one reverted or refused) and the body, the
            null-path check included when a relay moved.
        :raises SettingsRefused: For each state the module's description
            names, unsent; 503 once serve is stopping.
        :raises MeterError: When the line fails before the order is answered.
        """
        unit = "SYST:PINS:DEF" if defaults else "SYST:PINS:APPL"
        with self._changing():
            self._refuse_while_busy()
            answers = self.executor.query_all(["SYST:PINS?", "ROUT:STAT?"])
            before, circuit = (parse_fields(answer) for answer in answers)
            if not defaults:
                _same_table(before)
            if circuit["path"] != "shunt":
                raise SettingsRefused(
                    409,
                    "relays_not_at_rest",
                    f"a relay is not at rest (path={circuit['path']}): put them back first",
                    state=typed_fields(circuit),
                )
            reply = self.executor.call(";:".join([unit, *PIN_READBACK]))
            live, pending, result = reply.units
            check = None
            if not reply.errors:
                self.session.shaping.adopt_bus_clock()
                after = parse_fields(live)
                if any(after[key] != before[key] for key in _RELAY_KEYS):
                    check, live = self._check(live)
            return status_for(reply.codes), {
                "order": unit,
                "errors": errors_json(reply.errors),
                "result": report_json(result),
                "live": report_json(live),
                "pending": report_json(pending),
                "check": check,
            }

    @contextmanager
    def _changing(self) -> Iterator[None]:
        """The time an order and its check take: one at a time, and none once closed.

        :raises SettingsRefused: 409 while another runs, 503 once serve is stopping.
        """
        with self._lock:
            if self._closed:
                raise SettingsRefused(503, "closing", "serve is stopping")
            if self.running:
                raise SettingsRefused(409, "pins_changing", CHANGING)
            self._idle.clear()
        try:
            yield
        finally:
            self._idle.set()

    def _refuse_while_busy(self) -> None:
        """Refuse while a sequence or a capture runs, or another controller holds the line.

        :raises SettingsRefused: With its own word for each.
        """
        holder = self.holder()
        if holder is not None:
            raise SettingsRefused(423, "held", f"the line is held by {holder}", holder=holder)
        if self.sequences is not None and self.sequences.running:
            raise SettingsRefused(409, "sequence_running", "a sequence is running: abort it first")
        if self.recorder is not None and self.recorder.state != "idle":
            raise SettingsRefused(409, "capture_running", "a capture is running: stop it first")
        if self.session.foreign_window is not None:
            raise SettingsRefused(
                409, "window_open", "a window serve did not open is open: it is left alone"
            )

    def _check(self, live: str) -> tuple[dict[str, Any], str]:
        """Make the null path and release it, so that the relays' new pads are proved.

        :param live: ``SYSTem:PINS?``'s answer before it, kept when the line fails.
        :returns: The check's report, and ``SYSTem:PINS?``'s answer after it.
        """
        on, off = RELAY_CHECK
        errors: list[Any] = []
        failures: list[str] = []
        result = state = None
        try:
            reply = self.executor.call(f"{on};:ROUT:RES?")
            errors += reply.errors
            result = report_json(reply.units[0])
        except MeterError as error:
            failures.append(f"{on}: {error}")
        try:
            reply = self.executor.call(f"{off};:ROUT:STAT?;:SYST:PINS?")
            errors += reply.errors
            state, live = report_json(reply.units[0]), reply.units[1]
        except MeterError as error:
            failures.append(f"{off}: {error}; the circuit may be left as {on} made it")
        verified = parse_fields(live)["verified"] == "1"
        if not verified and not failures:
            failures.append("the null path was not seen to work: the relays stay unverified")
        check = {
            "passed": not errors and not failures,
            "errors": errors_json(errors),
            "result": result,
            "state": state,
            "detail": "; ".join(failures),
        }
        return check, live


def _changes(order: Mapping[str, Any]) -> tuple[bool, dict[str, int | str | None]]:
    """An order to stage: whether to start from the live map, and the typed fields.

    :raises SettingsRefused: 400 for what the route does not take.
    """
    given = dict(order)
    clear = given.pop("clear", False)
    if not isinstance(clear, bool):
        raise SettingsRefused(400, "bad_value", "clear is true or false", field="clear")
    if not given and not clear:
        raise SettingsRefused(400, "bad_body", "name at least one field, or clear")
    changes: dict[str, int | str | None] = {}
    for key, value in given.items():
        if key not in pins.KEYS:
            detail = f"no field {key!r}: one of {', '.join(pins.KEYS)}"
            raise SettingsRefused(400, "unknown_field", detail, field=key)
        try:
            changes[key] = pins.typed_change(key, value)
        except ValueError as why:
            raise SettingsRefused(400, "bad_value", str(why), field=key) from None
    return clear, changes


def _same_table(live: Mapping[str, str]) -> None:
    """Refuse when the instrument judges by another table than the package's.

    :raises SettingsRefused: 409.
    """
    if live["table"] != pins.DIGEST:
        raise SettingsRefused(
            409,
            "table_differs",
            f"the instrument's pin table is {live['table']} and this package's {pins.DIGEST}:"
            " the board shown is not the one it judges by",
        )
