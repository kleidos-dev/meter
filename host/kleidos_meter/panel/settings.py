# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The settings the panel writes, and each range's shunt with the registry's uncertainty.

Two things live here, both on the session's executor:

- **The writable settings** of :data:`~kleidos_meter.panel.schema.SETTINGS`:
  :func:`read_settings` reads every one as its query answers it, and
  :func:`write_settings` writes those named, in the schema's order, and
  reads them back from the instrument (a profile reads back the channels it
  reset and the averaging it converts with).
- **The shunt** (:class:`Calibration`). Per range, the value the instrument
  converts with (``CALibration:DATA?``), the figure the calibration registry
  files for it with its standard uncertainty and its expanded uncertainty
  ``U`` at ``k = 2``, in ppm and in micro-ohms, where the value came from,
  and the registry's verdict on the instrument
  (:func:`~kleidos_meter.registry.compare`). The registry is the
  calibration; the instrument holds a working copy.

A value of the panel's own (an **override**: a shunt swapped on the bench, a
figure from a datasheet) is taken only with its uncertainty, stated as ``U``
at ``k = 2`` or as a tolerance, a rectangular limit ``t`` whose standard
uncertainty is ``t/√3`` (GUM 4.3.7, :data:`~kleidos_meter.registry.DIVISORS`).
It is previewed (:meth:`Calibration.preview`: the unit that would be
written, the figures, how far it sits from the registry's and the
normalized error between the two), written with
``SENSe:CURRent:SHUNt:RANGe<n>`` (:meth:`Calibration.write_override`) and
kept on the host, in a file of the instrument's serial
(:class:`OverrideStore`), never in this repository. While a range converts
with it every figure the instrument gives is **not quotable**: the shunt's
view carries :data:`NOT_QUOTABLE` until the registry's copy is written back
(:meth:`Calibration.restore`, ``calibration_cli.restore``), which removes the
file.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..calibration_cli import describe, load_index
from ..calibration_cli import restore as restore_registry
from ..client import MeterError
from ..identity import SERIAL_PATTERN, load_aliases
from ..protocol import ErrorEntry, parse_fields
from ..registry import DIVISORS, Verdict, compare
from .executor import Executor, pack_units
from .paths import shown_error
from .reports import errors_json, status_for
from .schema import SCHEMA, Schema

__all__ = [
    "COVERAGE_K",
    "NOT_QUOTABLE",
    "OVERRIDE_SCHEMA",
    "Calibration",
    "InstrumentLink",
    "OverrideRequest",
    "OverrideStore",
    "SettingsRefused",
    "Uncertainty",
    "normalized_error",
    "parse_override",
    "read_settings",
    "registry_uncertainty",
    "verdict_json",
    "write_settings",
]

#: The coverage factor every expanded uncertainty here is stated at, the registry's.
COVERAGE_K = 2

#: The banner the shunt's view carries while a range converts with an override.
NOT_QUOTABLE = (
    "Not quotable: a range converts with a shunt the panel wrote, not the registry's"
    " calibration. Revert to the registry before quoting any figure."
)

#: An override file's schema.
OVERRIDE_SCHEMA = "kleidos.meter.shunt-override/1"

#: The statuses whose registry entry describes this instrument's shunt.
_ENTRY_APPLIES = frozenset({"applied", "working_copy_lost", "working_copy_differs", "expired"})

#: The statuses ``calibration_cli.restore`` writes the registry's copy over.
_RESTORABLE = frozenset({"working_copy_lost", "working_copy_differs"})

#: The ways an override may state its uncertainty: (key, a limit or ``U``, its unit).
_UNCERTAINTY_KEYS = (
    ("expanded_ppm", "expanded", "ppm"),
    ("expanded_uohm", "expanded", "uohm"),
    ("tolerance_ppm", "tolerance", "ppm"),
    ("tolerance_uohm", "tolerance", "uohm"),
)

#: A note kept with an override: printable, at most 200 characters.
_NOTE = re.compile(r"[ -~]{0,200}")


class SettingsRefused(Exception):
    """A settings or shunt route the panel refuses, with the HTTP status it maps to.

    :param status: 400, 409 or 503.
    :param error: A short machine word for the page.
    :param detail: What went wrong, for a person.
    :param errors: The instrument's errors, unchanged, when it refused.
    :param extra: More keys for the response's body.
    """

    def __init__(
        self,
        status: int,
        error: str,
        detail: str,
        errors: Sequence[ErrorEntry] = (),
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.errors = list(errors)
        self.extra = extra

    def body(self) -> dict[str, Any]:
        """The response's body."""
        return {
            "error": self.error,
            "detail": self.detail,
            "errors": errors_json(self.errors),
            **self.extra,
        }


# -- the writable settings ------------------------------------------------------------


def read_settings(executor: Executor, schema: Schema = SCHEMA) -> dict[str, str]:
    """Every setting, by name, as its query answers it.

    :raises MeterError: When a query is refused or unanswered.
    """
    settings = schema.settings
    answers = executor.query_all([setting.query for setting in settings])
    return {s.name: answer for s, answer in zip(settings, answers, strict=True)}


def write_settings(
    executor: Executor,
    wanted: Mapping[str, Any],
    schema: Schema = SCHEMA,
    before_averaging: Callable[[], object] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Write the settings *wanted* names, in the schema's order, and read them back.

    Every value is checked for its form before anything is sent; whether it
    is in range is the instrument's to say.

    :param wanted: Setting name to value, as the API's caller gave them.
    :param before_averaging: Called once the other settings are written and
        before a per-profile one is: what is found of the live profile is
        read then (:meth:`~kleidos_meter.panel.shaping.ShapingGuard.learn`).
        When *wanted* names a profile and the instrument refuses anything
        before it, no per-profile setting is written.
    :returns: The HTTP status (:func:`~kleidos_meter.panel.reports.status_for`
        of what the instrument queued) and the body: the units written, the
        errors and the settings read back.
    :raises SettingsRefused: With 400 for an empty request, an unknown or
        read-only setting, or a value of the wrong form; nothing is sent.
    """
    if not wanted:
        raise SettingsRefused(400, "bad_body", "name at least one setting")
    parameters: dict[str, str] = {}
    for name, value in wanted.items():
        setting = schema.setting(name)
        if setting is None:
            raise SettingsRefused(400, "unknown_setting", f"no setting {name!r}", setting=name)
        if not setting.writable:
            raise SettingsRefused(400, "read_only", f"{name} is shown, never written", setting=name)
        try:
            parameters[name] = setting.parameter(value)
        except ValueError as why:
            raise SettingsRefused(400, "bad_value", f"{name} takes {why}", setting=name) from None
    chosen = sorted(
        (s for s in schema.settings if s.name in parameters), key=lambda s: s.per_profile
    )  # the averaging is the live profile's: written once the profile is
    units = [s.write_unit(parameters[s.name]) for s in chosen]
    errors: list[ErrorEntry] = []
    ahead = [s for s in chosen if not s.per_profile]
    for message in pack_units(units[: len(ahead)]):
        errors += executor.call(message).errors
    if errors and "profile" in parameters:
        # The profile may not be the one asked for: an averaging written now
        # would change the measurement of another.
        chosen, units = ahead, units[: len(ahead)]
    if len(ahead) < len(chosen) and before_averaging is not None:
        before_averaging()
    for message in pack_units(units[len(ahead) :]):
        errors += executor.call(message).errors
    names = set(parameters)
    if "profile" in parameters:
        names |= {"channels"} | {s.name for s in schema.settings if s.per_profile}
    read = [s for s in schema.settings if s.name in names]
    answers = executor.query_all([s.query for s in read])
    return status_for(entry.code for entry in errors), {
        "written": units,
        "errors": errors_json(errors),
        "settings": {s.name: answer for s, answer in zip(read, answers, strict=True)},
    }


# -- the arithmetic -------------------------------------------------------------------


@dataclass(frozen=True)
class Uncertainty:
    """A shunt value and its standard uncertainty, and ``U`` at :data:`COVERAGE_K`.

    :ivar value_nohm: The value, nano-ohms.
    :ivar u_ppm: Its standard uncertainty, ppm of the value.
    :ivar k: The coverage factor ``U`` is stated at.
    """

    value_nohm: int
    u_ppm: float
    k: int = COVERAGE_K

    @property
    def expanded_ppm(self) -> float:
        """``U = k·u``, ppm."""
        return self.k * self.u_ppm

    @property
    def expanded_nohm(self) -> float:
        """``U`` in nano-ohms."""
        return self.expanded_ppm * self.value_nohm / 1e6

    @property
    def expanded_uohm(self) -> float:
        """``U`` in micro-ohms, to the thousandth."""
        return round(self.expanded_nohm / 1000, 3)

    def to_json(self) -> dict[str, Any]:
        """The figures, as the page shows them."""
        return {
            "value_uohm": round(self.value_nohm / 1000, 3),
            "u_ppm": self.u_ppm,
            "k": self.k,
            "expanded_ppm": self.expanded_ppm,
            "expanded_uohm": self.expanded_uohm,
        }


def registry_uncertainty(entry: Mapping[str, Any], range_index: int) -> Uncertainty:
    """The registry's applied value and uncertainty for one range of a calibration.

    :param entry: A calibration of the index (``build_index``), as
        :func:`~kleidos_meter.registry.select` picks it.
    :param range_index: 0 or 1.
    """
    rng = entry["ranges"][str(range_index)]
    return Uncertainty(rng["applied_nohm"], rng["u_ppm"], entry["coverage_k"])


def normalized_error(first: Uncertainty, second: Uncertainty) -> float:
    """``E = |R_2 − R_1| / √(U_1² + U_2²)`` between two values; below 1 they agree."""
    spread = math.hypot(first.expanded_nohm, second.expanded_nohm)
    return abs(second.value_nohm - first.value_nohm) / spread


@dataclass(frozen=True)
class OverrideRequest:
    """An override as asked for, checked for its form.

    :ivar range: 0 or 1.
    :ivar uncertainty: The value and the standard uncertainty it was given.
    :ivar given: The uncertainty as it was stated: ``{key: number}``.
    :ivar note: Where the figures come from, as the caller put it.
    """

    range: int
    uncertainty: Uncertainty
    given: dict[str, float]
    note: str

    @property
    def unit(self) -> str:
        """The unit that writes it: ``SENS:CURR:SHUN:RANG<n> <ohms>``, to the micro-ohm."""
        return f"SENS:CURR:SHUN:RANG{self.range} {self.uncertainty.value_nohm / 1e9:.6f}"


def _positive(value: object, name: str) -> float:
    """*value* as a finite positive number.

    :raises SettingsRefused: With 400 for anything else.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsRefused(400, "bad_override", f"{name} is a number")
    if not math.isfinite(value) or value <= 0:
        raise SettingsRefused(400, "bad_override", f"{name} is a finite number above 0")
    return float(value)


def parse_override(order: Mapping[str, Any], *, extra: Sequence[str] = ()) -> OverrideRequest:
    """Check an override's form: a range, a value, and exactly one statement of its uncertainty.

    :param order: ``range`` (0 or 1); ``ohms`` or ``uohm``, the value; one of
        ``expanded_ppm`` or ``expanded_uohm`` (``U`` at ``k = 2``) or
        ``tolerance_ppm`` or ``tolerance_uohm`` (a rectangular limit); and
        ``note``, where the figures come from, if wanted.
    :param extra: Further keys the caller may give (the write's ``confirm``).
    :raises SettingsRefused: With 400 for anything malformed, and for a value
        with no uncertainty, or with more than one: a value without one is
        a value nobody can quote.
    """
    known = {"range", "ohms", "uohm", "note", *extra, *(key for key, _, _ in _UNCERTAINTY_KEYS)}
    unknown = sorted(set(order) - known)
    if unknown:
        raise SettingsRefused(400, "bad_override", f"an override takes no {', '.join(unknown)}")
    index = order.get("range")
    if not isinstance(index, int) or isinstance(index, bool) or index not in (0, 1):
        raise SettingsRefused(400, "bad_override", "range is 0 or 1")
    values = [key for key in ("ohms", "uohm") if key in order]
    if len(values) != 1:
        raise SettingsRefused(400, "bad_override", "give the value once, as ohms or as uohm")
    number = _positive(order[values[0]], values[0])
    value_nohm = round(number * 1e9 if values[0] == "ohms" else number * 1e3, -3)
    if value_nohm <= 0:
        raise SettingsRefused(400, "bad_override", "the value rounds to 0 micro-ohms")
    stated = [(key, kind, unit) for key, kind, unit in _UNCERTAINTY_KEYS if key in order]
    if len(stated) != 1:
        raise SettingsRefused(
            400,
            "uncertainty_needed",
            "an override needs its uncertainty, once: expanded_ppm or expanded_uohm (U at k=2),"
            " or tolerance_ppm or tolerance_uohm (a limit, u = t/√3)",
        )
    key, kind, unit = stated[0]
    amount = _positive(order[key], key)
    ppm = amount if unit == "ppm" else amount * 1e6 / (value_nohm / 1000)
    u_ppm = ppm / COVERAGE_K if kind == "expanded" else ppm / DIVISORS["rectangular"]
    note = order.get("note", "")
    if not isinstance(note, str) or _NOTE.fullmatch(note) is None:
        raise SettingsRefused(400, "bad_override", "note is printable text, at most 200 characters")
    return OverrideRequest(
        index, Uncertainty(int(value_nohm), round(u_ppm, 1)), {key: amount}, note
    )


# -- the overrides on the host --------------------------------------------------------


class OverrideStore:
    """The overrides the panel wrote, one JSON file per instrument serial.

    :param directory: Where the files go; made, readable by the user alone, on the first write.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, serial: str) -> Path:
        """The file of *serial*'s overrides.

        :raises SettingsRefused: With 409 for anything that is not an instrument's serial.
        """
        if SERIAL_PATTERN.fullmatch(serial) is None:
            raise SettingsRefused(409, "no_serial", "the instrument reports no serial to key by")
        return self.directory / f"{serial}.json"

    def load(self, serial: str) -> dict[str, Any] | None:
        """*serial*'s overrides, None when there are none (or no serial to key them by)."""
        if SERIAL_PATTERN.fullmatch(serial) is None:
            return None
        try:
            overrides: dict[str, Any] = json.loads(self.path(serial).read_text(encoding="utf-8"))
            return overrides
        except FileNotFoundError:
            return None

    def save(self, serial: str, range_index: int, record: dict[str, Any]) -> dict[str, Any]:
        """Keep *record* as range *range_index*'s override of *serial*; the whole file.

        Written to a temporary file and renamed, so a reader never sees half of it.
        """
        path = self.path(serial)
        document = self.load(serial) or {
            "schema": OVERRIDE_SCHEMA,
            "serial": serial,
            "quotable": False,
            "banner": NOT_QUOTABLE,
            "ranges": {},
        }
        document["ranges"][str(range_index)] = record
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
        return document

    def clear(self, serial: str) -> bool:
        """Forget *serial*'s overrides; whether there were any."""
        if SERIAL_PATTERN.fullmatch(serial) is None:
            return False
        try:
            self.path(serial).unlink()
        except FileNotFoundError:
            return False
        return True


# -- the instrument, as calibration_cli talks to it ----------------------------------


class InstrumentLink:
    """The executor behind the four calls :mod:`~kleidos_meter.calibration_cli` makes.

    :param executor: The line's owner; every query and every write goes through it.
    """

    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def idn(self) -> str:
        """``*IDN?``."""
        return self.executor.query_all(["*IDN?"])[0]

    def info(self) -> dict[str, str]:
        """``SYSTem:INFormation?``."""
        return parse_fields(self.executor.query_all(["SYST:INF?"])[0])

    def calibration(self) -> dict[str, str]:
        """``CALibration:DATA?``."""
        return parse_fields(self.executor.query_all(["CAL:DATA?"])[0])

    def exchange(
        self, units: Sequence[str], *, raise_errors: bool = True
    ) -> tuple[list[str], list[ErrorEntry], bytes | None]:
        """Send *units*, packed into as few messages as fit, as :meth:`Meter.exchange` does.

        :returns: The query responses, the errors, and no block.
        :raises MeterError: When a unit was refused and *raise_errors*.
        """
        answers: list[str] = []
        errors: list[ErrorEntry] = []
        for message in pack_units(units):
            reply = self.executor.call(message)
            answers += reply.units
            errors += reply.errors
        if raise_errors and errors:
            raise MeterError(f"{units[0]!r}: " + "; ".join(map(str, errors)), errors)
        return answers, errors, None


def verdict_json(verdict: Verdict) -> dict[str, Any]:
    """A registry verdict as JSON: what ``calibration verify --json`` prints, and its lines."""
    return {
        "status": verdict.status,
        "ok": verdict.ok,
        "serial": verdict.serial,
        "aliased": verdict.aliased,
        "event": verdict.entry["event"] if verdict.entry else None,
        "differences": list(verdict.differences),
        "warnings": list(verdict.warnings),
        "lines": describe(verdict),
    }


class Calibration:
    """The shunt of each range against the registry, the overrides, and the restore.

    :param executor: The line's owner.
    :param registry: The registry's ``calibration/`` directory; None for none.
    :param overrides: Where the overrides are kept.
    :param today: The day verdicts are judged on.
    :param wall: The host's wall clock, for an override's ``written_at``.
    :param index_loader: How the registry's index is built from *registry*.
    :param alias_loader: The serial aliases, read again with the index
        (:func:`~kleidos_meter.identity.load_aliases`: the environment, else
        the repository's ``.env``, which git ignores).
    """

    def __init__(
        self,
        executor: Executor,
        registry: Path | None,
        overrides: OverrideStore,
        *,
        today: Callable[[], dt.date] = dt.date.today,
        wall: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        index_loader: Callable[[Path], dict[str, Any]] = load_index,
        alias_loader: Callable[[], dict[str, str]] = load_aliases,
    ) -> None:
        self.executor = executor
        self.registry = registry
        self.overrides = overrides
        self.today = today
        self.wall = wall
        self.index_loader = index_loader
        self.alias_loader = alias_loader
        self.link = InstrumentLink(executor)

    def index(self) -> tuple[dict[str, Any], str | None]:
        """The registry's index, read again each time, and why it is empty when it is.

        :returns: The index (an empty one without a registry) and the
            reason it could not be read, None when it was.
        """
        if self.registry is None:
            return {"instruments": {}}, "serve was given no registry"
        try:
            return self.index_loader(self.registry), None
        except (OSError, ValueError, KeyError, TypeError) as error:
            said = shown_error(error) if isinstance(error, OSError) else error
            return {"instruments": {}}, f"the registry could not be read: {said}"

    def lookup(self) -> tuple[dict[str, Any], dict[str, str], str | None]:
        """The index, the serial aliases, and why either could not be read.

        :returns: The index (:meth:`index`), the aliases (none when they could
            not be read) and the reasons, joined, None when both were read.
        """
        index, problem = self.index()
        try:
            aliases = self.alias_loader()
        except ValueError as error:
            aliases = {}
            alias_problem = f"the serial aliases could not be read: {error}"
            problem = alias_problem if problem is None else f"{problem}; {alias_problem}"
        return index, aliases, problem

    def _read(self) -> tuple[str, dict[str, str], dict[str, str], Verdict, str | None]:
        """``*IDN?``, ``SYSTem:INFormation?``, ``CALibration:DATA?`` and the verdict on them."""
        idn, info, cal = self.executor.query_all(["*IDN?", "SYST:INF?", "CAL:DATA?"])
        index, aliases, problem = self.lookup()
        info_fields, cal_fields = parse_fields(info), parse_fields(cal)
        verdict = compare(index, idn, info_fields, cal_fields, self.today(), aliases)
        if problem is not None:
            verdict.differences.append(problem)
        return idn, info_fields, cal_fields, verdict, problem

    def verify(self) -> dict[str, Any]:
        """The registry's verdict on the instrument (``calibration verify``)."""
        *_, verdict, problem = self._read()
        return {"verdict": verdict_json(verdict), "registry_problem": problem}

    def shunt(self) -> dict[str, Any]:
        """Each range's shunt: the instrument's value, the registry's figures, the override.

        A range's ``source`` is ``override`` when it converts with the
        panel's override, ``registry`` when with the registry's applied value
        (the working copy measured), ``nominal`` when the instrument holds no
        measured shunt, and ``device`` when it holds a value neither the
        registry nor the panel wrote. ``uncertainty`` is the figures of what
        it converts with, None for the last two: nothing states one.
        """
        _, _, cal, verdict, problem = self._read()
        serial = verdict.serial
        entry = verdict.entry if verdict.status in _ENTRY_APPLIES else None
        stored = self.overrides.load(serial)
        overrides = stored["ranges"] if stored else {}
        ranges = []
        for index in (0, 1):
            device_uohm = int(cal.get(f"shunt_uohm_r{index}", cal.get("shunt_uohm", "0")))
            registry = None if entry is None else registry_uncertainty(entry, index)
            override = overrides.get(str(index))
            figures: Uncertainty | None = None
            if override is not None and override["value_uohm"] == device_uohm:
                source = "override"
                figures = Uncertainty(device_uohm * 1000, override["u_ppm"], override["k"])
            elif (
                registry is not None
                and cal.get("shunt_measured") == "1"
                and round(registry.value_nohm / 1000) == device_uohm
            ):
                source, figures = "registry", registry
            elif cal.get("shunt_measured") != "1":
                source = "nominal"
            else:
                source = "device"
            ranges.append(
                {
                    "range": index,
                    "device_uohm": device_uohm,
                    "source": source,
                    "uncertainty": None if figures is None else figures.to_json(),
                    "registry": None
                    if registry is None or entry is None
                    else {
                        **registry.to_json(),
                        "event": entry["event"],
                        "valid_from": entry["valid_from"],
                        "valid_until": entry["valid_until"],
                        "record": entry["record"],
                    },
                    "deviation_ppm": None
                    if registry is None
                    else round(
                        (device_uohm * 1000 - registry.value_nohm) * 1e6 / registry.value_nohm
                    ),
                    "override": override,
                }
            )
        overridden = any(r["source"] == "override" for r in ranges)
        return {
            "serial": serial,
            "verdict": verdict_json(verdict),
            "registry_problem": problem,
            "ranges": ranges,
            "quotable": verdict.ok and not overridden,
            "banner": NOT_QUOTABLE if overridden else None,
        }

    def preview(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """What an override would write, and how it sits against the registry's figure.

        Sends only queries (the current value, the registry's verdict).

        :raises SettingsRefused: As :func:`parse_override`.
        """
        request = parse_override(order)
        return self._preview(request, self.shunt())

    def _preview(self, request: OverrideRequest, view: dict[str, Any]) -> dict[str, Any]:
        """The preview of *request* against the shunt's *view*."""
        current = view["ranges"][request.range]
        registry = current["registry"]
        figures = request.uncertainty
        against = (
            None
            if registry is None
            else Uncertainty(round(registry["value_uohm"] * 1000), registry["u_ppm"], registry["k"])
        )
        return {
            "range": request.range,
            "unit": request.unit,
            "uncertainty": figures.to_json(),
            "given": request.given,
            "note": request.note,
            "device_uohm": current["device_uohm"],
            "registry": registry,
            "deviation_ppm": None
            if against is None
            else round((figures.value_nohm - against.value_nohm) * 1e6 / against.value_nohm),
            "normalized_error": None
            if against is None
            else round(normalized_error(against, figures), 3),
            "quotable": False,
            "banner": NOT_QUOTABLE,
        }

    def write_override(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """Write an override to the instrument and keep it on the host.

        :param order: As :meth:`preview`, with ``confirm: true``: the page
            shows the preview first, and says so.
        :returns: The unit written, the override kept and the shunt's view after.
        :raises SettingsRefused: As :func:`parse_override`; with 400 without
            ``confirm``; with 409 and the instrument's errors unchanged when
            it refused the value; with 409 for an instrument with no serial.
        """
        request = parse_override(order, extra=("confirm",))
        if order.get("confirm") is not True:
            raise SettingsRefused(400, "confirm_needed", "preview it, then send confirm: true")
        before = self.shunt()
        preview = self._preview(request, before)
        self.overrides.path(before["serial"])  # refused before anything is written
        reply = self.executor.call(request.unit)
        if reply.errors:
            raise SettingsRefused(
                status_for(reply.codes),
                "refused",
                "the instrument refused the value",
                reply.errors,
                unit=request.unit,
            )
        record = {
            "value_uohm": request.uncertainty.value_nohm // 1000,
            "u_ppm": request.uncertainty.u_ppm,
            "k": request.uncertainty.k,
            "expanded_ppm": request.uncertainty.expanded_ppm,
            "expanded_uohm": request.uncertainty.expanded_uohm,
            "given": request.given,
            "note": request.note,
            "unit": request.unit,
            "written_at": self.wall().isoformat(timespec="seconds"),
            "replaced": {
                "device_uohm": before["ranges"][request.range]["device_uohm"],
                "source": before["ranges"][request.range]["source"],
            },
            "against_registry": {
                key: preview[key] for key in ("registry", "deviation_ppm", "normalized_error")
            },
        }
        self.overrides.save(before["serial"], request.range, record)
        return {"written": request.unit, "override": record, "shunt": self.shunt()}

    def restore(self) -> tuple[int, dict[str, Any]]:
        """Write the registry's working copy back (``calibration restore``): the revert.

        The HTTP request is the confirmation ``calibration_cli.restore`` asks
        for. Once the instrument holds the registry's copy the host's
        overrides of its serial are removed.

        :returns: 200 when it holds the registry's copy after, 409 when the
            registry has none to give it; and the body: the verdicts before
            and after, whether anything was written, what restore said.
        """
        index, aliases, problem = self.lookup()
        said: list[str] = []
        before = compare(index, *self._identity(), self.today(), aliases)
        after = restore_registry(
            self.link, index, self.today(), ask=lambda _: "y", say=said.append, aliases=aliases
        )
        cleared = self.overrides.clear(after.serial) if after.ok else False
        return (200 if after.ok else 409), {
            "before": verdict_json(before),
            "after": verdict_json(after),
            "written": before.status in _RESTORABLE,
            "said": said,
            "cleared_override": cleared,
            "registry_problem": problem,
        }

    def _identity(self) -> tuple[str, dict[str, str], dict[str, str]]:
        """``*IDN?``, ``SYSTem:INFormation?`` and ``CALibration:DATA?``'s fields."""
        idn, info, cal = self.executor.query_all(["*IDN?", "SYST:INF?", "CAL:DATA?"])
        return idn, parse_fields(info), parse_fields(cal)
