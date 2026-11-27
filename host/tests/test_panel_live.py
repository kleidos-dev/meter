# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The live events: Server-Sent Events from a bounded ring, and a slow page that costs nothing.

Most tests serve the panel over the clock-driven simulator on a fake clock
(``test_panel_http.serving_panel``) and read ``/api/events`` through a raw
socket, so a test can stop reading the way a stalled browser does. The hub's
own rules (rows merged per timestamp, the ring's bounds, the resume points)
are tested on a hub fed by hand.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from kleidos_meter.panel import live as live_module
from kleidos_meter.panel.executor import (
    DeviceErrors,
    Executor,
    LinkChanged,
    Rebooted,
    Status,
    StrayLine,
    analyze,
)
from kleidos_meter.panel.http import Api, PanelServer
from kleidos_meter.panel.live import (
    EVENT_KINDS,
    ROW_IDLE_TICKS,
    HubClosed,
    LiveHub,
    TooManyStreams,
    batch_columns,
    encode_event,
)
from kleidos_meter.panel.recorder import Recorder, Run
from kleidos_meter.panel.schema import SETTINGS
from kleidos_meter.panel.serve import Session
from kleidos_meter.protocol import (
    CHANNEL_INTEGER_KEYS,
    BlockProgress,
    ErrorEntry,
    Mark,
    Overflow,
    Sample,
)
from kleidos_meter.sim import SimulatedMeter
from test_panel_http import WAIT, Panel, serving_panel, until


@dataclass
class Event:
    """One event as a page reads it."""

    kind: str
    data: dict[str, Any]
    id: str | None = None


@dataclass
class EventReader:
    """``GET /api/events`` on a raw socket, read only when the test says so."""

    sock: socket.socket
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    buffer: bytes = b""
    comments: int = 0

    @classmethod
    def open(
        cls,
        port: int,
        *,
        last_event_id: str | None = None,
        rcvbuf: int | None = None,
        host: str | None = None,
    ) -> EventReader:
        """Connect, send the request and read the response's head."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.settimeout(WAIT)
        sock.connect(("127.0.0.1", port))
        lines = [
            "GET /api/events HTTP/1.1",
            f"Host: {host or f'127.0.0.1:{port}'}",
            "Accept: text/event-stream",
        ]
        if last_event_id is not None:
            lines.append(f"Last-Event-ID: {last_event_id}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        reader = cls(sock)
        while b"\r\n\r\n" not in reader.buffer:
            reader._fill()
        head, reader.buffer = reader.buffer.split(b"\r\n\r\n", 1)
        first, *rest = head.decode().split("\r\n")
        reader.status = int(first.split()[1])
        reader.headers = {
            k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in rest)
        }
        return reader

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("the stream ended")
        self.buffer += chunk

    def next(self) -> Event:
        """The next event, comments and the retry line skipped."""
        while True:
            while b"\n\n" not in self.buffer:
                self._fill()
            block, self.buffer = self.buffer.split(b"\n\n", 1)
            fields: dict[str, str] = {}
            for line in block.decode().split("\n"):
                if line.startswith(":"):
                    self.comments += 1
                    continue
                name, _, value = line.partition(": ")
                fields[name] = value
            if "event" in fields:
                return Event(fields["event"], json.loads(fields["data"]), fields.get("id"))

    def until(self, condition: Callable[[Event], bool]) -> list[Event]:
        """Every event up to and including the first that meets *condition*."""
        seen: list[Event] = []
        while True:
            event = self.next()
            seen.append(event)
            if condition(event):
                return seen

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def panel() -> Iterator[Panel]:
    with serving_panel() as served:
        yield served


def capture_ended(event: Event) -> bool:
    """The recorder's state after a capture: idle, with the run it finished."""
    return (
        event.kind == "status"
        and event.data.get("kind") == "capture"
        and event.data.get("state") == "idle"
    )


def rows_of(events: list[Event]) -> list[tuple[int, dict[str, int]]]:
    """The sample rows the ``samples`` events carried, in order."""
    rows: list[tuple[int, dict[str, int]]] = []
    for event in events:
        if event.kind != "samples":
            continue
        data = event.data
        names = [key for key in ("i_na", "v_uv", "p_uw", "die_mc") if key in data]
        for place, t in enumerate(data["t_us"]):
            rows.append(
                (t, {name: data[name][place] for name in names if data[name][place] is not None})
            )
    return rows


# -- a capture, over the stream and into its run ---------------------------------------


def test_a_10_s_state_capture_reaches_the_stream_and_the_run_with_every_sample(
    panel: Panel,
) -> None:
    reader = EventReader.open(panel.server.port)
    try:
        assert reader.status == 200
        assert reader.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert reader.headers["cache-control"] == "no-store"
        assert "content-length" not in reader.headers
        assert not [name for name in reader.headers if name.startswith("access-control-")]
        hello = reader.next()
        assert hello.kind == "status" and hello.data["kind"] == "hello" and hello.id is None
        assert hello.data["epoch"] == panel.live.epoch
        started = panel.post(
            "/api/capture/start",
            {"profile": "STATE", "channels": ["CURR", "VOLT"], "seconds": 10, "label": "ten s"},
        )
        assert started.status == 200, started.body
        assert started.body["state"] == "running" and started.body["profile"] == "STATE"
        events = reader.until(capture_ended)
    finally:
        reader.close()
    meta = panel.recorder.last_meta
    assert meta is not None
    stats = meta["statistics"]["fields"]
    samples = int(stats["samples"])
    assert int(stats["dropped"]) == 0
    # 10 s of the instrument's time, and what it sampled while the stop was on its way.
    assert int(stats["dur_us"]) >= 10_000_000
    assert abs(samples - int(stats["dur_us"]) / 1152) <= 2
    rows = rows_of(events)
    assert len(rows) == samples
    batches = [event for event in events if event.kind == "samples"]
    counted = 0
    for batch in batches:  # the batches are contiguous, counted from the capture's start
        assert batch.data["n0"] == counted and batch.data["count"] == len(batch.data["t_us"])
        counted += batch.data["count"]
    assert all(set(values) == {"i_na", "v_uv"} for _, values in rows)
    times = [t for t, _ in rows]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert rows[0][1]["i_na"] == 88_600_000 and rows[0][1]["v_uv"] == 4_064_000
    recorded = [
        (record[0], {CHANNEL_INTEGER_KEYS[name]: value for name, value in record[1].items()})
        for record in Run(panel.recorder.data_dir / meta["id"]).records()
        if not isinstance(record, Mark)
    ]
    assert recorded == rows  # the run's stream.txt holds the same rows
    assert meta["stream"]["samples"] == samples == meta["stream"]["records"] // 2
    assert meta["complete"] is True and meta["ended_by"] == "seconds"
    assert meta["label"] == "ten s"
    statuses = [event.data["kind"] for event in events if event.kind == "status"]
    assert "capture" in statuses and "poll" in statuses
    ids = [event.id for event in events if event.id is not None]
    seqs = [int(text.split("-")[1]) for text in ids]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # no event skipped


def test_a_stalled_stream_is_told_what_it_lost_and_the_line_never_waits() -> None:
    options = {"ring_events": 4, "interval": 0.02}
    with serving_panel(live_options=options) as panel:
        reader = EventReader.open(panel.server.port, rcvbuf=2048)
        try:
            assert reader.next().data["kind"] == "hello"
            # The page stops reading here: its socket fills, then the server's.
            started = panel.post(
                "/api/capture/start",
                {"profile": "STATE", "channels": ["CURR", "VOLT", "POW"], "seconds": 30},
            )
            assert started.status == 200
            assert panel.recorder.wait_idle(60)  # the capture ends while the page is stalled
            meta = panel.recorder.last_meta
            assert meta is not None
            samples = int(meta["statistics"]["fields"]["samples"])
            assert meta["stream"]["samples"] == samples  # the line lost nothing to the page
            assert meta["statistics"]["fields"]["dropped"] == "0" and meta["complete"]
            assert len(panel.live._ring) <= 4  # the ring stayed bounded
            events = reader.until(capture_ended)  # the page reads again
        finally:
            reader.close()
        lost = [event for event in events if event.kind == "lost"]
        assert lost, "the stalled page was not told what it lost"
        assert all(event.id is None for event in lost)
        missed = sum(event.data["samples"] for event in lost)
        assert missed > 0
        assert len(rows_of(events)) + missed == samples  # every row: received, or counted lost


def test_a_real_clock_capture_is_not_slowed_by_a_page_that_reads_nothing(tmp_path: Any) -> None:
    sim = SimulatedMeter(clocked=True, tx_capacity=16384)  # half a second of STATE output
    executor = Executor(sim)
    session = Session(executor)
    live = LiveHub(ring_events=8)
    executor.add_listener(live.on_event)
    recorder = Recorder(session, tmp_path / "runs", live=live)
    server = PanelServer(Api(session, live=live, recorder=recorder), "127.0.0.1", 0)
    executor.start()
    live.start()
    try:
        executor.wait_ready(WAIT)
        session.open()
        server.start()
        reader = EventReader.open(server.port, rcvbuf=1024)
        reader.next()
        recorder.start("STAT", ["CURR", "VOLT", "POW", "TEMP"], 1.5)
        assert recorder.wait_idle(WAIT)
        meta = recorder.last_meta
        assert meta is not None
        assert meta["statistics"]["fields"]["dropped"] == "0"
        assert meta["stream"]["samples"] == int(meta["statistics"]["fields"]["samples"]) > 1000
        reader.close()
    finally:
        server.stop()
        recorder.close()
        live.close()
        executor.close()


def test_a_page_that_reconnects_resumes_after_its_last_event(panel: Panel) -> None:
    first = EventReader.open(panel.server.port)
    try:
        first.next()
        panel.post("/api/capture/start", {"profile": "STATE", "seconds": 1})
        events = first.until(capture_ended)
    finally:
        first.close()
    middle = [event for event in events if event.id is not None][2]
    later = [event for event in events if event.id is not None][3:]
    again = EventReader.open(panel.server.port, last_event_id=middle.id)
    try:
        assert again.next().data["kind"] == "hello"
        resumed = again.until(lambda event: event.id == later[-1].id)
    finally:
        again.close()
    with_id = [event for event in resumed if event.id is not None]
    assert [event.id for event in with_id[: len(later)]] == [event.id for event in later]
    assert not [event for event in resumed if event.kind == "lost"]


def test_a_page_that_comes_back_too_late_is_told_exactly_what_it_missed() -> None:
    with serving_panel(live_options={"ring_events": 3}) as panel:
        first = EventReader.open(panel.server.port)
        try:
            first.next()
            panel.post("/api/capture/start", {"profile": "STATE", "seconds": 2})
            events = first.until(capture_ended)
        finally:
            first.close()
        rows_seen = 0
        early = None
        for event in events:
            rows_seen += event.data["count"] if event.kind == "samples" else 0
            if event.kind == "samples" and early is None:
                early = event
        assert early is not None and early.id is not None
        rows_up_to_early = int(early.id.split("-")[2])
        again = EventReader.open(panel.server.port, last_event_id=early.id)
        try:
            assert again.next().data["kind"] == "hello"
            notice = again.next()
            kept = again.until(lambda event: event.id == events[-1].id)
        finally:
            again.close()
        assert notice.kind == "lost" and notice.data["events"] > 0
        received = len(rows_of(kept))
        last = events[-1].id
        assert last is not None
        total = int(last.split("-")[2])
        assert rows_up_to_early + notice.data["samples"] + received == total


@pytest.mark.parametrize("last", ["0123abcd-5-10", "garbage", "nothex-x-y"])
def test_an_id_of_another_epoch_resumes_at_the_newest_with_the_loss_unknown(
    panel: Panel, last: str
) -> None:
    reader = EventReader.open(panel.server.port, last_event_id=last)
    try:
        assert reader.next().data["kind"] == "hello"
        notice = reader.next()
    finally:
        reader.close()
    assert notice.kind == "lost" and notice.data == {"events": None, "samples": None}


def test_an_id_from_the_future_resumes_at_the_newest(panel: Panel) -> None:
    reader = EventReader.open(panel.server.port, last_event_id=f"{panel.live.epoch}-999999-0")
    try:
        assert reader.next().data["kind"] == "hello"
        assert reader.next().kind == "lost"
    finally:
        reader.close()


def test_the_stream_limit_answers_503_and_a_closed_stream_frees_its_slot() -> None:
    with serving_panel(live_options={"max_streams": 1}) as panel:
        held = EventReader.open(panel.server.port)
        try:
            held.next()
            refused = panel.get("/api/events")
            assert refused.status == 503 and refused.body["error"] == "busy"
            assert refused.headers["retry-after"] == "5"
        finally:
            held.close()
        until(lambda: panel.live.streams == 0, "the slot freed")
        again = EventReader.open(panel.server.port)
        try:
            assert again.next().data["kind"] == "hello"
        finally:
            again.close()


def test_a_stream_from_a_foreign_host_is_refused(panel: Panel) -> None:
    reader = EventReader.open(panel.server.port, host="evil.example")
    try:
        assert reader.status == 421
    finally:
        reader.close()


def test_the_stream_ends_when_the_hub_closes_and_a_new_one_is_refused(panel: Panel) -> None:
    reader = EventReader.open(panel.server.port)
    try:
        reader.next()
        panel.live.close()
        with pytest.raises(EOFError):
            while True:  # what was sent before the close, then the stream's end
                reader.next()
    finally:
        reader.close()
    answer = panel.get("/api/events")
    assert answer.status == 503 and answer.body["error"] == "closing"


def test_a_quiet_stream_gets_a_keepalive_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    from kleidos_meter.panel import http as panel_http

    monkeypatch.setattr(panel_http, "KEEPALIVE", 0.01)
    hub = LiveHub(interval=10.0)
    hub.start()
    with serving_panel() as panel:
        panel.api.live = hub
        reader = EventReader.open(panel.server.port)
        try:
            reader.next()
            until(lambda: _read_comment(reader), "a keepalive")
        finally:
            reader.close()
    hub.close()


def _read_comment(reader: EventReader) -> bool:
    reader.sock.settimeout(0.05)
    with contextlib.suppress(TimeoutError):
        reader._fill()
    reader.sock.settimeout(WAIT)
    return b": keepalive" in reader.buffer


def test_without_a_hub_the_events_route_answers_503(panel: Panel) -> None:
    panel.api.live = None
    answer = panel.get("/api/events")
    assert answer.status == 503 and answer.body["error"] == "unavailable"
    assert panel.get("/api/status").body["live"] is None


def test_the_status_route_names_the_streams_and_the_newest_event(panel: Panel) -> None:
    body = panel.get("/api/status").body
    assert body["live"]["streams"] == 0
    assert body["live"]["last_id"].startswith(panel.live.epoch + "-")
    assert body["capture"]["state"] == "idle"


# -- the hub's own rules ------------------------------------------------------------------


def frames(hub: LiveHub, subscription: live_module.Subscription) -> list[Event]:
    """What *subscription* is owed now, as events."""
    data = subscription.next(0)
    events = []
    for block in data.decode().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        if "event" in fields:
            events.append(Event(fields["event"], json.loads(fields["data"]), fields.get("id")))
    return events


def text_sample(t: int, channel: str, value: float) -> Sample:
    return Sample(t, {channel: value}, f"METER t={t} ch={channel} v={value:.3f}")


def test_the_text_formats_lines_of_one_timestamp_become_one_row() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    for t in (100, 200):
        hub.on_event(text_sample(t, "i_ua", 1.5))
        hub.on_event(text_sample(t, "v_mv", 4064.0))
    hub.on_event(Sample(300, {"i_ua": -0.001, "die_c": 25.5}, "S,300,-0.001,,,25.500"))
    hub.flush()
    (batch,) = [event for event in frames(hub, sub) if event.kind == "samples"]
    assert batch.data == {
        "n0": 0,
        "count": 3,
        "t_us": [100, 200, 300],
        "i_na": [1500, 1500, -1],
        "v_uv": [4064000, 4064000, None],
        "die_mc": [None, None, 25500],
    }


def test_an_open_row_waits_for_its_other_channels_and_an_event_never_cuts_it() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(text_sample(100, "i_ua", 1.0))
    hub.tick()  # it just grew: it stays open
    hub.publish("sequence", {"step": 1})  # an event between its lines leaves it open
    assert [e.kind for e in frames(hub, sub)] == ["sequence"]
    hub.on_event(text_sample(100, "v_mv", 2.0))
    hub.tick()
    assert [e for e in frames(hub, sub) if e.kind == "samples"] == []
    for _ in range(ROW_IDLE_TICKS - 1):
        hub.tick()  # unchanged for ROW_IDLE_TICKS batches: closed
    (batch,) = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert batch.data["i_na"] == [1000] and batch.data["v_uv"] == [2000]


def test_a_channel_that_comes_after_its_row_closed_never_makes_a_second_row() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(text_sample(100, "i_ua", 1.0))
    for _ in range(ROW_IDLE_TICKS):
        hub.tick()  # the line is slow: the row is closed with one channel
    hub.on_event(text_sample(100, "v_mv", 2.0))  # its other channel, late
    hub.on_event(text_sample(200, "i_ua", 3.0))
    hub.on_event(text_sample(200, "v_mv", 4.0))
    hub.flush()
    batches = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert [t for batch in batches for t in batch.data["t_us"]] == [100, 200]
    assert batches[0].data["i_na"] == [1000] and "v_uv" not in batches[0].data
    assert batches[1].data == {"n0": 1, "count": 1, "t_us": [200], "i_na": [3000], "v_uv": [4000]}
    hub.capture_started()  # a new capture's first row is never taken for a late channel
    hub.on_event(text_sample(200, "i_ua", 5.0))
    hub.flush()
    (batch,) = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert batch.data == {"n0": 0, "count": 1, "t_us": [200], "i_na": [5000]}


def test_a_mark_closes_the_batch_and_keeps_its_place() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(text_sample(100, "i_ua", 1.0))
    hub.on_event(Mark("cut", 1, 101, "METER_MARK mark=cut n=1 t=101"))
    hub.on_event(text_sample(200, "i_ua", 2.0))
    hub.flush()
    events = frames(hub, sub)
    assert [e.kind for e in events] == ["samples", "mark", "samples"]
    assert events[1].data == {"label": "cut", "n": 1, "t_us": 101}
    assert events[2].data["n0"] == 1


def test_every_event_of_the_line_has_its_kind() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(Status({"sink": "ring", "samples": "40", "running": "1"}, {"uptime_us": "5"}))
    hub.on_event(BlockProgress(10, 100))
    hub.on_event(BlockProgress(50, 100))
    hub.on_event(Rebooted({"reset": "sw", "uptime_us": "1"}))
    hub.on_event(LinkChanged(False))
    hub.on_event(DeviceErrors([ErrorEntry(113, "Hold;released")]))
    hub.on_event(Overflow(70000))
    hub.on_event(StrayLine(["late"]))  # nothing for a page
    hub.publish("sequence", {"name": "HEALTH", "step": 1})
    events = frames(hub, sub)
    kinds = [(e.kind, e.data.get("kind")) for e in events]
    assert kinds == [
        ("status", "reboot"),
        ("status", "link"),
        ("error", "device"),
        ("error", "overflow"),
        ("status", "poll"),
        ("ring", None),
        ("sequence", None),
    ]
    assert events[0].data["health"] == {"reset": "sw", "uptime_us": 1}
    assert events[2].data["errors"] == [{"code": 113, "text": "Hold;released"}]
    assert events[3].data["discarded"] == 70000
    assert events[4].data["status"]["samples"] == 40
    assert events[5].data == {"samples": 40, "running": 1, "dump": {"received": 50, "total": 100}}
    assert {kind for kind, _ in kinds} <= set(EVENT_KINDS)


def test_a_message_that_changes_settings_is_told_once_a_batch() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    for message in ("SENS:AVER ON", "SENS:PROF STAT", "SENS:AVER:COUN 16", "*IDN?", "FORM TEXT"):
        hub.on_sent(analyze(message))
    hub.tick()
    hub.on_sent(analyze("MEAS:CURR?;:SYST:ERR?"))
    hub.tick()
    hub.on_sent(analyze("SAMP:COUN?;:SENS:AVER?"))  # queries change nothing
    hub.tick()
    events = [e.data for e in frames(hub, sub) if e.data.get("kind") == "settings"]
    averaging = ["average", "average_aperture", "average_count"]
    assert events == [
        {"kind": "settings", "changed": [*averaging, "channels", "format", "profile"]},
        {"kind": "settings", "changed": ["channels", "profile", "range"]},
    ]
    hub.on_sent(analyze("*RST"))
    hub.tick()
    reset = frames(hub, sub)[-1].data["changed"]
    assert reset == sorted(setting.name for setting in SETTINGS)


def test_a_page_hears_of_a_setting_another_one_wrote() -> None:
    with serving_panel() as panel:
        reader = EventReader.open(panel.server.port)
        assert panel.post("/api/scpi", "SENS:PROF STAT;:SENS:AVER ON").status == 200
        heard = reader.until(lambda e: e.data.get("kind") == "settings")[-1]
        assert heard.kind == "status" and "average" in heard.data["changed"]
        reader.sock.close()


def test_a_stream_profile_poll_has_no_ring_event() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(Status({"sink": "stream", "samples": "40"}, {}))
    hub.flush()
    assert [e.kind for e in frames(hub, sub)] == ["status"]


def test_the_ring_keeps_its_bounds_in_events_and_bytes() -> None:
    hub = LiveHub(ring_events=5)
    for n in range(20):
        hub.publish("sequence", {"n": n})
    assert [json.loads(e.frame.split(b"data: ")[1]) for e in hub._ring] == [
        {"n": n} for n in range(15, 20)
    ]
    small = LiveHub(ring_bytes=200)
    for n in range(20):
        small.publish("sequence", {"n": n, "pad": "x" * 50})
    assert small._ring_size <= 200 and len(small._ring) >= 1
    huge = LiveHub(ring_bytes=10)
    huge.publish("sequence", {"pad": "x" * 100})
    assert len(huge._ring) == 1  # the newest is kept whatever its size


def test_rows_past_the_pending_bound_are_counted_as_overrun() -> None:
    hub = LiveHub(max_pending_rows=2)
    sub = hub.subscribe()
    for t in (1, 2, 3, 4):
        hub.on_event(text_sample(t, "i_ua", 1.0))
        hub.on_event(text_sample(t, "v_mv", 1.0))  # the rest of a dropped row goes too
    hub.on_event(Mark("m", 4, 4))
    hub.flush()
    events = frames(hub, sub)
    assert [e.kind for e in events] == ["samples", "mark"]
    batch = events[0]
    assert batch.data["t_us"] == [1, 2] and batch.data["overrun"] == 2
    hub.on_event(text_sample(5, "i_ua", 1.0))
    hub.flush()
    (after,) = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert "overrun" not in after.data and after.data["n0"] == 2


def test_an_overrun_waits_for_the_next_batch_when_none_is_published() -> None:
    hub = LiveHub(max_pending_rows=0)
    sub = hub.subscribe()
    hub.on_event(text_sample(1, "i_ua", 1.0))
    hub.on_event(Mark("m", 1, 1))
    hub.publish("sequence", {"step": 1})
    assert [e.kind for e in frames(hub, sub)] == ["mark", "sequence"]
    hub.max_pending_rows = 10
    hub.on_event(text_sample(2, "i_ua", 1.0))
    hub.flush()
    (batch,) = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert batch.data["overrun"] == 1


def test_a_capture_start_counts_rows_from_zero_again() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(text_sample(1, "i_ua", 1.0))
    hub.capture_started()
    hub.on_event(text_sample(2, "i_ua", 1.0))
    hub.flush()
    batches = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert [b.data["n0"] for b in batches] == [0, 0]
    assert batches[1].id is not None and batches[1].id.endswith("-2")  # rows of the hub's life


def test_the_hello_carries_the_last_poll_and_capture() -> None:
    hub = LiveHub(epoch="abc123")
    hub.on_event(Status({"sink": "idle"}, {"uptime_us": "9"}))
    hub.publish("status", {"kind": "capture", "state": "idle"})
    hub.publish("status", {"kind": "link", "up": True})  # not kept for the hello
    first = hub.subscribe()._first.decode()
    hello = json.loads(first.split("data: ")[1].split("\n")[0])
    assert hello["kind"] == "hello" and hello["epoch"] == "abc123"
    assert hello["poll"]["health"] == {"uptime_us": 9}
    assert hello["capture"] == {"kind": "capture", "state": "idle"}
    assert first.startswith("retry: 2000\n\n")
    assert hub.last_id == "abc123-3-0"


def test_a_hub_that_never_started_closes() -> None:
    hub = LiveHub()
    hub.close()
    assert hub.closed


def test_an_epoch_is_letters_and_digits() -> None:
    with pytest.raises(ValueError):
        LiveHub(epoch="a-b")
    assert LiveHub().epoch.isalnum() and LiveHub().last_id.endswith("-0-0")


def test_a_closed_hub_refuses_streams_publishes_nothing_and_ends_its_streams() -> None:
    hub = LiveHub()
    hub.start()
    sub = hub.subscribe()
    stream = sub.frames(0.01)
    assert next(stream).startswith(b"retry:")
    hub.close()
    assert hub.closed
    assert list(stream) == []  # the stream ends
    assert hub.streams == 0
    with pytest.raises(HubClosed):
        hub.subscribe()
    with pytest.raises(HubClosed):
        sub.next(0)
    hub.publish("sequence", {})
    assert len(hub._ring) == 0
    hub.close()  # a second close does nothing


def test_too_many_streams_are_refused_and_a_closed_one_frees_its_slot() -> None:
    hub = LiveHub(max_streams=2)
    first, _ = hub.subscribe(), hub.subscribe()
    with pytest.raises(TooManyStreams):
        hub.subscribe()
    first.close()
    first.close()  # a second close does nothing
    hub.subscribe()
    assert hub.streams == 2


def test_a_waiting_stream_wakes_for_the_next_event_and_times_out_with_nothing() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    assert sub.next(0.01) == b""
    timer = threading.Timer(0.05, hub.publish, args=("sequence", {"n": 1}))
    timer.start()
    started = time.monotonic()
    data = sub.next(WAIT)
    timer.join()
    assert b"event: sequence" in data and time.monotonic() - started < WAIT


def test_a_read_hands_over_at_most_a_mebibyte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_module, "_CHUNK_BYTES", 150)  # two events
    hub = LiveHub()
    sub = hub.subscribe()
    for n in range(5):
        hub.publish("sequence", {"n": n, "pad": "x" * 60})
    first = sub.next(0)
    assert first.count(b"event: sequence") == 2
    rest = sub.next(0) + sub.next(0) + sub.next(0)
    assert rest.count(b"event: sequence") == 3


def test_the_batch_thread_publishes_on_its_own() -> None:
    hub = LiveHub(interval=0.01)
    hub.start()
    try:
        sub = hub.subscribe()
        hub.on_event(Sample(1, {"i_ua": 1.0}, "S,1,1.000,,,"))
        deadline = time.monotonic() + WAIT
        data = b""
        while b"event: samples" not in data:
            assert time.monotonic() < deadline
            data += sub.next(0.05)
    finally:
        hub.close()


def test_the_helpers_encode_events_and_columns() -> None:
    assert encode_event("mark", {"a": 1}, "e-1-0") == b'id: e-1-0\nevent: mark\ndata: {"a":1}\n\n'
    assert encode_event("mark", {"é": 1}) == 'event: mark\ndata: {"é":1}\n\n'.encode()
    assert batch_columns([], 5) == {"n0": 5, "count": 0, "t_us": []}


def test_a_channel_the_hub_does_not_know_is_left_out() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    hub.on_event(Sample(1, {"i_ua": 1.0, "x": 9.0}))
    hub.flush()
    (batch,) = [e for e in frames(hub, sub) if e.kind == "samples"]
    assert batch.data == {"n0": 0, "count": 1, "t_us": [1], "i_na": [1000]}
