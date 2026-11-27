# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The capture-shaping settings serve found, and putting back what it changed.

A host that reaches the meter after serve (a QA run through the passthrough,
a tool after serve exits) finds the settings serve found, not the ones its
last session left: the profile, the channels, the range, what a capture
keeps, its limit, the threshold and its marks, the trigger, the sample
count, the averaging, the stream's format and the I2C clock
(:data:`SETTINGS`). They are
read once at the start (:meth:`ShapingGuard.take`); every message the
executor writes is checked against the table patterns that change each one
(``*RST`` changes them all), and :meth:`ShapingGuard.restore` writes back
those it changed that differ, in an order that keeps each write valid (the
I2C clock first, the profile before the channels it resets), and reads them
back.

Two things are kept apart:

- **The averaging** (:data:`AVERAGING`) is the instrument's for each profile,
  and only the live profile's can be read. What serve found of a profile is
  read before the panel first changes it (:meth:`ShapingGuard.learn`); once a
  message has, every profile the session may have used is visited on the way
  out and put back. A profile nothing was read of is put back to off, what
  the instrument boots with.
- **The pin map** is no setting of a capture
  (:data:`~kleidos_meter.panel.schema.PIN_ORDERS`): it is never snapshotted
  and never written back. A map applied meanwhile runs the bus at its own
  clock, which is the one to leave (:meth:`ShapingGuard.adopt_bus_clock`).

The settings are the schema's (:data:`~kleidos_meter.panel.schema.SETTINGS`):
the panel writes the ones it shows from the same declaration this guard
restores, so a setting cannot be writable without being put back.
"""

from __future__ import annotations

import threading

from ..client import MeterError
from .executor import Executor, MessageInfo, pack_units
from .schema import AVERAGING_SETTERS, PROFILES, SETTINGS, Setting

__all__ = [
    "AVERAGING",
    "SETTINGS",
    "ShapingGuard",
    "ShapingSetting",
    "changed_settings",
    "changes_averaging",
]


#: A capture-shaping setting: the schema's declaration.
ShapingSetting = Setting


#: The settings the instrument keeps for each profile, in the order they are written.
AVERAGING: tuple[Setting, ...] = tuple(s for s in SETTINGS if s.per_profile)

#: The profiles that take them, as ``SENSe:PROFile?`` answers them.
_AVERAGED = tuple(p.name.upper() for p in PROFILES if p.averaging)


def changes_averaging(info: MessageInfo) -> bool:
    """Whether a message changes an averaging override: a setter, or ``*RST``.

    :param info: The message, as :func:`~kleidos_meter.panel.executor.analyze` describes it.
    """
    return any(p == "*RST" or p in AVERAGING_SETTERS for p in info.patterns)


def changed_settings(info: MessageInfo) -> set[str]:
    """The settings a message's units change.

    :param info: The message, as :func:`~kleidos_meter.panel.executor.analyze` describes it.
    :returns: Their names; every one for a message with ``*RST``.
    """
    if "*RST" in info.patterns:
        return {setting.name for setting in SETTINGS}
    return {
        setting.name
        for setting in SETTINGS
        if any(pattern in setting.changed_by for pattern in info.patterns)
    }


class ShapingGuard:
    """Keep the settings serve found, and put back those it changed.

    :param executor: The line's owner; every message it writes is checked.
    """

    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self._lock = threading.Lock()
        self._dirty: set[str] = set()
        self._averaged = False  # a message changed an averaging override
        self._found: dict[str, dict[str, str]] = {}  # each profile's averaging, as found
        #: The settings as :meth:`take` read them, by name; empty before.
        self.snapshot: dict[str, str] = {}
        executor.on_sent(self._note)

    @property
    def dirty(self) -> set[str]:
        """The settings a message changed since the snapshot or the last restore."""
        with self._lock:
            return set(self._dirty)

    def read(self, names: list[str] | None = None) -> dict[str, str]:
        """Read settings from the instrument.

        :param names: Which, every one of :data:`SETTINGS` by default.
        :returns: Their values, by name, as their queries answer them.
        """
        chosen = [s for s in SETTINGS if names is None or s.name in names]
        answers = self._executor.query_all([setting.query for setting in chosen])
        return {setting.name: answer for setting, answer in zip(chosen, answers, strict=True)}

    def take(self) -> dict[str, str]:
        """Read every setting as the snapshot to restore to; nothing is dirty after.

        :returns: The snapshot.
        """
        self.snapshot = self.read()
        with self._lock:
            self._dirty.clear()
            self._averaged = False
            self._found = {
                self.snapshot["profile"]: {s.name: self.snapshot[s.name] for s in AVERAGING}
            }
        return dict(self.snapshot)

    def learn(self) -> None:
        """Read the live profile's averaging as it is found, unless it has been already.

        Called before a message changes it (:func:`changes_averaging`).
        """
        profile, found = self._live_averaging()
        with self._lock:
            self._found.setdefault(profile, found)

    def adopt_bus_clock(self) -> None:
        """Take the bus clock as it runs for the one to leave: a pin map was applied with its own.

        :raises MeterError: When the query is refused or unanswered.
        """
        clock = self.read(["i2c_hz"])
        with self._lock:
            self.snapshot.update(clock)

    def restore(self) -> list[tuple[str, str, str]]:
        """Write back the changed settings that differ from the snapshot, and read them back.

        A write the instrument refuses (a window that is not serve's still
        runs: ``-221``) fails the restore, and the settings stay dirty.

        :returns: ``(name, found, restored)`` for each setting written back.
        :raises MeterError: When a write was refused, or a setting reads back
            other than the snapshot.
        """
        dirty = self.dirty
        if not dirty:
            return []
        if "profile" in dirty:
            dirty.add("channels")  # writing the profile resets them
        chosen = [s for s in SETTINGS if s.name in dirty and not s.per_profile]
        names = [s.name for s in chosen]
        found = self.read(names)
        writes = {name for name in names if found[name] != self.snapshot[name]}
        if "profile" in writes:
            writes.add("channels")
        put_back = [
            (s.name, found[s.name], self.snapshot[s.name]) for s in chosen if s.name in writes
        ]
        # The clock first: how short a sample BURST and an averaging may take follows it.
        self._write_settings(writes & {"i2c_hz"})
        writes.discard("i2c_hz")
        averaged: list[tuple[str, str, str]] = []
        if self._averaged:
            averaged = self._restore_averaging("profile" in dirty)
            if "profile" in dirty:
                writes |= {"profile", "channels"}  # the averaging's visits moved them
        self._write_settings(writes)
        after = self.read(names)
        wrong = [name for name in names if after[name] != self.snapshot[name]]
        if wrong:
            raise MeterError("the settings read back changed: " + ", ".join(wrong))
        with self._lock:
            self._dirty.clear()
            self._averaged = False
        return averaged + put_back

    def _write_settings(self, names: set[str]) -> None:
        """Write the settings *names* back as the snapshot has them, in their order.

        :raises MeterError: When the instrument refuses one.
        """
        self._write([s.write_unit(self.snapshot[s.name]) for s in SETTINGS if s.name in names])

    def _write(self, units: list[str]) -> None:
        """Write *units*, packed.

        :raises MeterError: When the instrument refuses one.
        """
        for message in pack_units(units):
            reply = self._executor.call(message)
            if reply.errors:
                raise MeterError(
                    "writing back the settings was refused: " + "; ".join(map(str, reply.errors)),
                    reply.errors,
                )

    def _live_averaging(self) -> tuple[str, dict[str, str]]:
        """The live profile, and its averaging as its queries answer it."""
        profile, *answers = self._executor.query_all(["SENS:PROF?", *(s.query for s in AVERAGING)])
        return profile, {s.name: answer for s, answer in zip(AVERAGING, answers, strict=True)}

    def _restore_averaging(self, visit: bool) -> list[tuple[str, str, str]]:
        """Put back the averaging of the profiles the session may have changed it on.

        :param visit: Whether the profile changed meanwhile, so that each one
            that takes averaging is selected in turn; the live one alone when not.
        :returns: ``(name, found, restored)`` for each value that differed, the
            profile in the name.
        :raises MeterError: As :meth:`restore`.
        """
        live = self._live_averaging()[0]
        restored: list[tuple[str, str, str]] = []
        for profile in sorted(_AVERAGED, key=lambda name: name != live):
            if profile == live:
                restored += self._put_averaging_back(profile)
            elif visit:
                self._write([f"SENS:PROF {profile}"])
                restored += self._put_averaging_back(profile)
        return restored

    def _put_averaging_back(self, profile: str) -> list[tuple[str, str, str]]:
        """Put the live *profile*'s averaging back as found; off when nothing was read of it."""
        now = self._live_averaging()[1]
        with self._lock:
            found = self._found.get(profile)
        if now == found and now["average"] == "1":
            return []
        if now["average"] == "1":
            self._write(["SENS:AVER OFF"])
        then = self._live_averaging()[1] if found is None else found
        values = [s.write_unit(then[s.name]) for s in AVERAGING]
        if then["average"] == "1":
            self._write(values)
        else:
            # What an override that is off would convert with cannot be read:
            # the profile's own is what it boots with. An image that refuses
            # it has nothing to put back.
            for message in pack_units(values):
                self._executor.call(message)
        if self._live_averaging()[1] != then:
            raise MeterError(f"the averaging of {profile} read back changed")
        return [
            (f"{name} of {profile}", now[name], then[name])
            for name in now
            if now[name] != then[name]
        ]

    def _note(self, info: MessageInfo) -> None:
        """Mark what a message the executor wrote changes."""
        changed = changed_settings(info)
        if changed:
            with self._lock:
                self._dirty |= changed
                self._averaged |= changes_averaging(info)
