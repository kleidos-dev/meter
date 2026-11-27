# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The recorder: every capture serve owns in a run directory, and what is read back from one.

The captures run on the clock-driven simulator on a fake clock, through the
panel's own routes where a route exists (``test_panel_http.serving_panel``);
``kleidos-meter capture`` runs the same recorder with no panel.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import statistics
import threading
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from kleidos_meter import cli, protocol
from kleidos_meter.capture_cli import main as capture_main
from kleidos_meter.client import MeterError, PortBusyError
from kleidos_meter.panel import executor as executor_events
from kleidos_meter.panel import lockinfo, serve
from kleidos_meter.panel import recorder as recorder_module
from kleidos_meter.panel.recorder import (
    EXPORT_SCHEMA,
    RUN_SCHEMA,
    CaptureRefused,
    DataDirRefused,
    Run,
    check_data_dir,
    run_id_ok,
    work_tree_of,
)
from kleidos_meter.protocol import Mark, Sample, check_dump, decode_dump, parse_record
from kleidos_meter.sim import SimulatedMeter
from test_panel_http import WAIT, Panel, serving_panel, until

#: The repository's work tree, which no run may go in.
REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def panel(tmp_path: Path) -> Iterator[Panel]:
    with serving_panel(data_dir=tmp_path / "runs") as served:
        yield served


def capture(panel: Panel, **order: Any) -> dict[str, Any]:
    """Run one capture through the API to its end; its ``meta.json``."""
    answer = panel.post("/api/capture/start", order)
    assert answer.status == 200, answer.body
    until(lambda: panel.recorder.state == "idle", "the capture's end")
    meta = panel.recorder.last_meta
    assert meta is not None
    return meta


def run_of(panel: Panel, meta: dict[str, Any]) -> Run:
    return Run(panel.recorder.data_dir / meta["id"])


def records_of(text: str) -> list[tuple[int, dict[str, int]] | Mark]:
    """Records read back by ``parse_record``, the ``TEXT`` lines of a timestamp merged."""
    out: list[tuple[int, dict[str, int]] | Mark] = []
    for line in text.splitlines():
        record = parse_record(line)
        assert record is not None, line
        if isinstance(record, Mark):
            out.append(record)
            continue
        values = {name: round(value * 1000) for name, value in record.values.items()}
        last = out[-1] if out else None
        if isinstance(last, tuple) and last[0] == record.t_us:
            last[1].update(values)
        else:
            out.append((record.t_us, values))
    return out


def json_records(document: dict[str, Any]) -> list[tuple[int, dict[str, int]]]:
    """The rows of a JSON export's columns, with the instrument's channel names."""
    names = {"i_na": "i_ua", "v_uv": "v_mv", "p_uw": "p_mw", "die_mc": "die_c"}
    columns = document["columns"]
    rows = []
    for place, t in enumerate(columns["t_us"]):
        values = {
            names[key]: column[place]
            for key, column in columns.items()
            if key != "t_us" and column[place] is not None
        }
        rows.append((t, values))
    return rows


# -- the run directory ------------------------------------------------------------------


def test_a_streamed_capture_is_a_self_describing_run_directory(panel: Panel) -> None:
    meta = capture(panel, profile="STATE", channels=["CURR", "VOLT"], seconds=2, label="state")
    path = panel.recorder.data_dir / meta["id"]
    assert run_id_ok(meta["id"]) and "-STATE-" in meta["id"]
    assert sorted(p.name for p in path.iterdir()) == ["meta.json", "stream.txt"]
    assert json.loads((path / "meta.json").read_text()) == meta
    assert meta["schema"] == RUN_SCHEMA and meta["state"] == "complete" and meta["complete"]
    assert meta["ended_by"] == "seconds" and meta["profile"] == "STATE" and meta["format"] == "TEXT"
    assert meta["request"] == {"seconds": 2.0} and meta["label"] == "state"
    assert meta["instrument"]["info"]["serial"] == "KM-EE529C1B03"
    assert meta["instrument"]["idn"].startswith("KLEIDOS,KLEIDOS-METER,")
    assert meta["settings"]["profile"] == "STATE" and meta["settings"]["format"] == "TEXT"
    assert meta["header"]["fields"]["period_us"] == "1152"
    assert meta["header"]["unit"].startswith("fw=")
    assert meta["host"]["started"] <= meta["host"]["ended"]
    assert "wall clock" in meta["host"]["clock"]
    stream = meta["stream"]
    data = (path / "stream.txt").read_bytes()
    assert stream["bytes"] == len(data) and stream["sha256"] == hashlib.sha256(data).hexdigest()
    assert stream["records"] == data.count(b"\n") == 2 * stream["samples"]
    assert stream["samples"] == int(meta["statistics"]["fields"]["samples"])
    lines = data.decode().splitlines()
    assert lines[0].startswith("METER t=") and lines[0].endswith(".000")  # as the line wrote it
    first = parse_record(lines[0])
    assert isinstance(first, Sample) and stream["first_t_us"] == first.t_us
    assert stream["lost"] == 0 and stream["error"] is None
    assert meta["trace"] is None and meta["errors"] == []
    assert (path.stat().st_mode & 0o777) == 0o700


def test_a_run_taken_with_the_averaging_on_names_it(panel: Panel) -> None:
    order = {"profile": "STATE", "average_count": 16, "average": True}
    assert panel.post("/api/settings", order).status == 200
    meta = capture(panel, profile="STATE", seconds=1.0)
    assert meta["noise_reduction"] == {
        "state": "on",
        "count": 16,
        "conversion_us": int(meta["header"]["fields"]["vshct_us"]),
        "period_us": int(meta["header"]["fields"]["period_us"]),
    }
    assert meta["noise_reduction"]["period_us"] > 1152
    assert (meta["settings"]["average"], meta["settings"]["average_count"]) == ("1", "16")
    assert (
        meta["settings"]["average_aperture"]
        == panel.get("/api/query?q=SENS:CURR:APER%3F").body["units"][0]
    )
    written = json.loads((run_of(panel, meta).path / "meta.json").read_text(encoding="utf-8"))
    assert written["noise_reduction"] == meta["noise_reduction"]
    assert panel.get("/api/runs").body["runs"][0]["noise_reduction"] == meta["noise_reduction"]


@pytest.mark.parametrize("profile", ["STATE", "SLEEP", "TRACE"])
def test_a_run_taken_with_the_averaging_off_says_so_with_the_profiles_own(
    panel: Panel, profile: str
) -> None:
    meta = capture(panel, profile=profile, seconds=0.5)
    reduction, header = meta["noise_reduction"], meta["header"]["fields"]
    assert reduction["state"] == "off"
    assert reduction["count"] == int(header["avg"]) == (16 if profile == "SLEEP" else 1)
    assert reduction["period_us"] == int(header["period_us"])
    assert meta["settings"]["average"] == "0"
    assert panel.get("/api/runs").body["runs"][0]["noise_reduction"] == reduction


def test_the_running_capture_says_its_averaging_once_it_has_started(panel: Panel) -> None:
    order = {"profile": "STATE", "average_count": 16, "average": True}
    assert panel.post("/api/settings", order).status == 200
    assert "noise_reduction" not in panel.get("/api/capture").body  # no capture to say it of
    begun = panel.post("/api/capture/start", {"profile": "STATE"}).body
    running = panel.get("/api/capture").body["noise_reduction"]
    meta = panel.post("/api/capture/stop").body["meta"]
    assert begun["noise_reduction"] == running == meta["noise_reduction"]
    assert running["state"] == "on"
    last = panel.get("/api/capture").body["last"]
    assert last["noise_reduction"] == running


def test_an_image_whose_header_says_nothing_of_averaging_leaves_it_unknown(panel: Panel) -> None:
    assert recorder_module._noise_reduction({"avg": "1", "period_us": "1152"}) is None
    assert Run.summary_of({"noise_reduction": None})["noise_reduction"] is None
    assert Run.summary_of({})["noise_reduction"] is None


def test_marks_are_recorded_where_the_line_put_them(panel: Panel) -> None:
    started = panel.post("/api/capture/start", {"profile": "STATE"})
    assert started.status == 200
    until(lambda: panel.recorder.state_json()["samples"] > 100, "samples")
    marked = panel.post("/api/capture/mark", {"label": "cut"})
    assert marked.status == 200 and marked.body == {"label": "cut", "errors": []}
    meta = panel.post("/api/capture/stop").body["meta"]
    assert meta["stream"]["marks"] == 1
    records = list(run_of(panel, meta).records())
    (mark,) = [r for r in records if isinstance(r, Mark)]
    assert mark.label == "cut"
    before = [r for r in records[: records.index(mark)] if not isinstance(r, Mark)]
    assert len(before) == mark.index  # a mark is behind every sample of a lower index


def test_a_mark_is_refused_without_a_capture_and_for_a_bad_label(panel: Panel) -> None:
    refused = panel.post("/api/capture/mark", {"label": "cut"})
    assert refused.status == 409 and refused.body["error"] == "no_capture"
    for label in ["", "a b", "x" * 16, "a,b", 'a"b', 7, None]:
        answer = panel.post("/api/capture/mark", {"label": label})
        assert answer.status == 400 and answer.body["error"] == "bad_label", label
        # The refusal names every rule, the space among them: "a b" is printable.
        assert answer.body["detail"] == (
            "a mark is 1 to 15 printable characters, with no space and none of , ; = \" '"
        ), label
    assert not [info for info in panel.sent if "TRAC:MARK" in info.message]


def test_stopping_through_the_api_finishes_the_run(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "SLEEP"}).status == 200
    assert panel.get("/api/capture").body["state"] == "running"
    stopped = panel.post("/api/capture/stop")
    assert stopped.status == 200
    meta = stopped.body["meta"]
    assert meta["ended_by"] == "stop" and stopped.body["run"] == meta["id"]
    assert stopped.body["summary"]["id"] == meta["id"]
    assert panel.get("/api/capture").body["last"]["id"] == meta["id"]
    again = panel.post("/api/capture/stop")
    assert again.status == 409 and again.body["error"] == "no_capture"


def test_a_capture_that_ends_by_itself_is_closed_and_recorded(panel: Panel) -> None:
    assert panel.post("/api/settings", {"limit": True, "sample_count": 50}).status == 200
    meta = capture(panel, profile="STATE")
    assert meta["ended_by"] == "ended_by_itself"
    assert meta["stream"]["samples"] == int(meta["statistics"]["fields"]["samples"]) == 50
    assert meta["complete"]


def test_a_statistics_only_capture_records_no_stream_and_is_complete(panel: Panel) -> None:
    assert panel.post("/api/settings", {"capture": "STATistics"}).status == 200
    meta = capture(panel, profile="STATE", seconds=1)
    assert meta["stream"]["samples"] == 0 and int(meta["statistics"]["fields"]["samples"]) > 0
    assert meta["complete"]


def test_a_reboot_ends_the_run_with_what_it_had(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE"}).status == 200
    until(lambda: panel.recorder.state_json()["samples"] > 50, "samples")
    panel.sim.inject_reboot()
    until(lambda: panel.recorder.state == "idle", "the end")
    meta = panel.recorder.last_meta
    assert meta is not None
    assert meta["ended_by"] == "reboot" and meta["state"] == "incomplete"
    assert meta["statistics"] is None and not meta["complete"]
    assert meta["stream"]["samples"] > 50


def test_a_line_that_goes_down_ends_the_run(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE"}).status == 200
    until(lambda: panel.recorder.state == "running", "running")
    panel.recorder.on_event(executor_events.LinkChanged(False))
    until(lambda: panel.recorder.state == "idle", "the end")
    meta = panel.recorder.last_meta
    assert meta is not None and meta["ended_by"] == "link_down"
    panel.recorder.on_event(executor_events.LinkChanged(True))  # nothing to end
    panel.recorder.on_event(executor_events.Status({"running": "0"}, {}))  # nothing to end
    assert panel.recorder.state == "idle"


def test_errors_queued_while_it_ran_are_kept(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE"}).status == 200
    errors = [protocol.ErrorEntry(113, "Hold;released")]
    panel.recorder.on_event(executor_events.DeviceErrors(errors))
    meta = panel.post("/api/capture/stop").body["meta"]
    assert meta["errors"] == [{"code": 113, "text": "Hold;released"}]
    panel.recorder.on_event(executor_events.DeviceErrors(errors))  # no capture: dropped


def test_a_failed_stop_leaves_an_incomplete_run(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE"}).status == 200
    real_call = panel.executor.call

    def failing(message: str, **kwargs: Any) -> Any:
        if message.startswith("ABOR"):
            raise MeterError("no answer")
        return real_call(message, **kwargs)

    monkeypatch.setattr(panel.executor, "call", failing)
    stopped = panel.post("/api/capture/stop")
    assert stopped.status == 200
    meta = stopped.body["meta"]
    assert meta["failure"] == "no answer" and meta["state"] == "incomplete"
    assert not meta["complete"]
    monkeypatch.setattr(panel.executor, "call", real_call)
    panel.executor.call("ABOR")  # the capture still ran on the instrument


# -- the ring ------------------------------------------------------------------------------


def ring_panel(tmp_path: Path) -> Any:
    return serving_panel(data_dir=tmp_path / "runs", sim_options={"ring_capacity": 4096})


def test_a_ring_capture_keeps_its_dump_crc_checked(tmp_path: Path) -> None:
    with ring_panel(tmp_path) as panel:
        meta = capture(panel, profile="TRACE")
        path = panel.recorder.data_dir / meta["id"]
        assert meta["ended_by"] == "ended_by_itself" and meta["complete"]
        trace = meta["trace"]
        block = (path / "trace.raw").read_bytes()
        assert check_dump(block) is True
        assert trace["file"] == "trace.raw" and trace["crc32"] == "checked"
        assert trace["count"] == trace["points"] == 4096 and trace["start"] == 0
        assert trace["bytes"] == len(block)
        assert trace["sha256"] == hashlib.sha256(block).hexdigest()
        dump = decode_dump(block, trace["dump_header"]["unit"])
        assert trace["first_t_us"] == dump.t_us(0) and trace["last_t_us"] == dump.t_us(4095)
        rows = [r for r in run_of(panel, meta).records() if not isinstance(r, Mark)]
        assert len(rows) == 4096 and dump.nanoamps is not None
        assert rows[10] == (dump.t_us(10), {"i_ua": dump.nanoamps[10]})


def test_a_ring_captures_marks_come_before_the_sample_they_mark(tmp_path: Path) -> None:
    big = {"ring_capacity": 1 << 16}
    with serving_panel(data_dir=tmp_path / "runs", sim_options=big) as panel:
        panel.sim.sleep = lambda seconds: time.sleep(0.001)  # time stands still unless moved
        assert panel.post("/api/capture/start", {"profile": "TRACE"}).status == 200
        panel.clock.now += 0.1  # about 357 samples, then the mark, then as many again
        assert panel.post("/api/capture/mark", {"label": "go"}).status == 200
        panel.clock.now += 0.1
        stopped = panel.post("/api/capture/stop")
        assert stopped.status == 200
        meta = stopped.body["meta"]
        assert meta["ended_by"] == "stop" and meta["complete"]
        records = list(run_of(panel, meta).records())
        (mark,) = [r for r in records if isinstance(r, Mark)]
        assert records.index(mark) == mark.index  # every sample of a lower index before it
        text = run_of(panel, meta).export("text")[0].read_text()
        assert f"METER_MARK mark=go n={mark.index} t={mark.t_us}" in text


def test_a_mark_past_the_dump_comes_after_its_samples() -> None:
    from kleidos_meter.protocol import Dump

    dump = Dump(True, 1, 0, 10, 0, 0, None, None, (), (Mark("late", 5, 50),), None, None, True)
    records = list(recorder_module._dump_records(dump))
    assert records == [(0, {}), Mark("late", 5, 50)]


def test_a_dump_that_fails_its_crc_is_fetched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    real = protocol.check_dump

    def once_bad(block: bytes) -> bool | None:
        calls.append(len(block))
        if len(calls) == 1:
            raise ValueError("CRC-32 mismatch")
        return real(block)

    monkeypatch.setattr(recorder_module, "check_dump", once_bad)
    with ring_panel(tmp_path) as panel:
        meta = capture(panel, profile="TRACE")
    assert len(calls) == 2 and meta["trace"]["file"] == "trace.raw" and meta["complete"]


def test_a_dump_that_fails_twice_is_kept_aside_and_the_run_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad(block: bytes) -> bool | None:
        raise ValueError("CRC-32 mismatch")

    monkeypatch.setattr(recorder_module, "check_dump", bad)
    with ring_panel(tmp_path) as panel:
        meta = capture(panel, profile="TRACE")
        path = panel.recorder.data_dir / meta["id"]
        assert meta["trace"] == {
            "file": None,
            "damaged": "trace.damaged",
            "error": "CRC-32 mismatch",
        }
        assert (path / "trace.damaged").is_file() and not (path / "trace.raw").exists()
        assert not meta["complete"]
        # Read back as a streamed run: its stream.txt holds no sample.
        assert list(run_of(panel, meta).records()) == []


def test_a_dump_with_no_block_leaves_no_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ring_panel(tmp_path) as panel:
        real_call = panel.executor.call

        def no_block(message: str, **kwargs: Any) -> Any:
            if "TRAC:DATA?" in message:
                return real_call("FETC:HEAD?", **kwargs)
            return real_call(message, **kwargs)

        monkeypatch.setattr(panel.executor, "call", no_block)
        meta = capture(panel, profile="TRACE")
    assert meta["trace"] is None and not meta["complete"]


# -- refusals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("order", "error"),
    [
        ({}, "bad_body"),
        ({"profile": "STATE", "extra": 1}, "bad_body"),
        ({"profile": "NOPE"}, "bad_parameter"),
        ({"profile": "STATE", "channels": ["AMPS"]}, "bad_parameter"),
        ({"profile": "STATE", "seconds": 0}, "bad_parameter"),
        ({"profile": "STATE", "seconds": True}, "bad_parameter"),
        ({"profile": "STATE", "seconds": "10"}, "bad_parameter"),
        ({"profile": "STATE", "seconds": 1e9}, "bad_parameter"),
        ({"profile": "STATE", "label": "é"}, "bad_parameter"),
        ({"profile": "STATE", "label": 7}, "bad_parameter"),
        ({"profile": "STATE", "label": "x" * 81}, "bad_parameter"),
    ],
)
def test_a_malformed_capture_is_refused_unsent(
    panel: Panel, order: dict[str, Any], error: str
) -> None:
    answer = panel.post("/api/capture/start", order)
    assert answer.status == 400 and answer.body["error"] == error
    assert panel.sent == [] and panel.recorder.state == "idle"
    assert not panel.recorder.data_dir.exists() or not list(panel.recorder.data_dir.iterdir())


def test_a_label_may_be_absent_or_null(panel: Panel) -> None:
    meta = capture(panel, profile="STATE", seconds=0.2, label=None)
    assert meta["label"] == ""


def test_a_capture_the_instrument_refuses_leaves_no_run(panel: Panel) -> None:
    answer = panel.post("/api/capture/start", {"profile": "TRACE", "channels": ["VOLT"]})
    assert answer.status in (400, 409) and answer.body["error"] == "refused"
    assert answer.body["errors"]
    assert panel.recorder.state == "idle"
    assert not panel.recorder.data_dir.exists() or not list(panel.recorder.data_dir.iterdir())


def test_an_initiate_the_instrument_refuses_removes_the_run(panel: Panel) -> None:
    assert panel.post("/api/settings", {"trigger_source": "LEV"}).status == 200
    answer = panel.post("/api/capture/start", {"profile": "STATE"})  # a level trigger: a ring
    assert answer.status == 409 and answer.body["error"] == "refused", answer.body
    assert panel.recorder.state == "idle"
    assert list(panel.recorder.data_dir.iterdir()) == []


def test_an_initiate_that_fails_on_the_line_removes_the_run(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_call = panel.executor.call

    def failing(message: str, **kwargs: Any) -> Any:
        if message.startswith("INIT"):
            raise MeterError("the line failed")
        return real_call(message, **kwargs)

    monkeypatch.setattr(panel.executor, "call", failing)
    with pytest.raises(MeterError):
        panel.recorder.start("STATE")
    assert panel.recorder.state == "idle" and list(panel.recorder.data_dir.iterdir()) == []


def test_an_initiate_with_no_answer_unit_is_refused(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_call = panel.executor.call

    def silent(message: str, **kwargs: Any) -> Any:
        if message.startswith("INIT"):
            return real_call("INIT", **kwargs)
        return real_call(message, **kwargs)

    monkeypatch.setattr(panel.executor, "call", silent)
    with pytest.raises(CaptureRefused) as refused:
        panel.recorder.start("STATE")
    assert refused.value.status == 502
    panel.executor.call("ABOR")


def test_a_second_capture_and_a_foreign_window_are_refused(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE"}).status == 200
    again = panel.post("/api/capture/start", {"profile": "STATE"})
    assert again.status == 409 and again.body["error"] == "capture_running"
    assert panel.post("/api/capture/stop").status == 200
    session = panel.api.session
    panel.executor.remove_listener(session._on_event)  # no poll closes the window serve "found"
    panel.executor.call("*OPC?", wait=WAIT)  # the thread is past any event it was handing out
    session.foreign_window = {"sink": "stream"}
    found = panel.post("/api/capture/start", {"profile": "STATE"})
    assert found.status == 409 and found.body["error"] == "window_open"


def test_the_capture_routes_are_held_while_another_controller_holds_the_line(
    panel: Panel,
) -> None:
    panel.api.hold("a QA run")
    for path in ("/api/capture/start", "/api/capture/stop", "/api/capture/mark"):
        assert panel.post(path, {"profile": "STATE"}).status == 423
    assert panel.get("/api/capture").status == 200 and panel.get("/api/runs").status == 200


def test_without_a_recorder_the_capture_and_run_routes_answer_503(panel: Panel) -> None:
    panel.api.recorder = None
    for answer in (
        panel.get("/api/capture"),
        panel.get("/api/runs"),
        panel.post("/api/capture/start", {"profile": "STATE"}),
    ):
        assert answer.status == 503 and answer.body["error"] == "unavailable"
    assert panel.get("/api/status").body["capture"] is None


# -- the data directory --------------------------------------------------------------------


def test_a_data_directory_inside_a_work_tree_is_refused(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(DataDirRefused, match="work tree"):
        check_data_dir(tmp_path / "repo" / "data" / "runs")
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / ".git").write_text("gitdir: elsewhere\n")  # a linked work tree
    with pytest.raises(DataDirRefused):
        check_data_dir(tmp_path / "linked")
    with pytest.raises(DataDirRefused):
        check_data_dir(REPOSITORY / "runs")
    assert work_tree_of(REPOSITORY / "host" / "x") == REPOSITORY
    assert work_tree_of(tmp_path / "free") is None
    assert check_data_dir(tmp_path / "free" / "runs") == (tmp_path / "free" / "runs").resolve()
    (tmp_path / "file").write_text("")
    with pytest.raises(DataDirRefused, match="not a directory"):
        check_data_dir(tmp_path / "file")


def test_serve_and_capture_refuse_a_data_directory_in_the_repository(
    capsys: pytest.CaptureFixture[str],
) -> None:
    inside = str(REPOSITORY / "host" / "runs")
    with pytest.raises(SystemExit) as served:
        serve.main(["--sim", "--data-dir", inside])
    assert served.value.code == 2
    with pytest.raises(SystemExit) as captured:
        capture_main(["--sim", "--profile", "STATE", "--seconds", "1", "--out", inside])
    assert captured.value.code == 2
    assert "work tree" in capsys.readouterr().err
    assert not (REPOSITORY / "host" / "runs").exists()


def test_the_recorder_refuses_a_work_tree_too(panel: Panel) -> None:
    with pytest.raises(DataDirRefused):
        recorder_module.Recorder(panel.api.session, REPOSITORY / "runs")


# -- reading a run back -------------------------------------------------------------------


def test_every_export_of_a_text_run_reads_back_as_its_records(panel: Panel) -> None:
    started = panel.post(
        "/api/capture/start",
        {"profile": "STATE", "channels": ["CURR", "VOLT", "POW", "TEMP"]},
    )
    assert started.status == 200
    until(lambda: panel.recorder.state_json()["samples"] > 100, "samples")
    assert panel.post("/api/capture/mark", {"label": "m1"}).status == 200
    until(lambda: panel.recorder.state_json()["samples"] > 1000, "more samples")
    meta = panel.post("/api/capture/stop").body["meta"]
    run = run_of(panel, meta)
    expected = list(run.records())
    assert any(isinstance(r, Mark) for r in expected)
    text = panel.get(f"/api/runs/{meta['id']}/export?format=text")
    assert text.status == 200 and text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.raw == (run.path / "stream.txt").read_bytes()  # its own format: the stream itself
    assert text.headers["content-disposition"] == f'attachment; filename="{meta["id"]}.txt"'
    assert int(text.headers["content-length"]) == len(text.raw)
    csv = panel.get(f"/api/runs/{meta['id']}/export?format=csv")
    assert csv.status == 200 and csv.headers["content-type"] == "text/csv; charset=utf-8"
    assert csv.raw.decode().startswith("S,")
    assert records_of(csv.raw.decode()) == expected == records_of(text.raw.decode())
    document = panel.get(f"/api/runs/{meta['id']}/export?format=json").body
    assert document["schema"] == EXPORT_SCHEMA and document["run"] == meta["id"]
    assert document["meta"] == meta
    assert document["units"] == {
        "t_us": "us",
        "i_na": "nA",
        "v_uv": "uV",
        "p_uw": "uW",
        "die_mc": "mdegC",
    }
    assert json_records(document) == [r for r in expected if not isinstance(r, Mark)]
    assert [m["label"] for m in document["marks"]] == ["m1"]
    assert (run.path / "export.csv").is_file() and (run.path / "export.json").is_file()
    again = panel.get(f"/api/runs/{meta['id']}/export?format=csv")
    assert again.raw == csv.raw  # made once, kept


def test_a_run_still_recording_is_not_exported_and_its_export_is_whole_after(
    panel: Panel,
) -> None:
    """An export made from a partial stream was kept and served for good (P3)."""
    started = panel.post("/api/capture/start", {"profile": "STATE"})
    assert started.status == 200
    until(lambda: panel.recorder.state_json()["samples"] > 100, "samples")
    run_id = panel.recorder.state_json()["run"]
    listed = next(r for r in panel.get("/api/runs").body["runs"] if r["id"] == run_id)
    assert listed["recording"] is True  # the page offers no export of it
    for kind in ("csv", "text", "json"):
        early = panel.get(f"/api/runs/{run_id}/export?format={kind}")
        assert early.status == 409 and early.body["error"] == "recording", early.body
    run = panel.recorder.run(run_id)
    assert not [p.name for p in run.path.iterdir() if p.name.startswith("export.")]
    meta = panel.post("/api/capture/stop").body["meta"]
    listed = next(r for r in panel.get("/api/runs").body["runs"] if r["id"] == run_id)
    assert listed["recording"] is False
    csv = panel.get(f"/api/runs/{run_id}/export?format=csv")
    assert csv.status == 200
    rows = [r for r in records_of(csv.raw.decode()) if not isinstance(r, Mark)]
    assert len(rows) == len([r for r in run_of(panel, meta).records() if not isinstance(r, Mark)])


def test_exports_made_at_once_each_write_their_own_file(panel: Panel) -> None:
    import threading

    meta = capture(panel, profile="SLEEP", seconds=2)
    run = run_of(panel, meta)
    results: list[bytes] = []

    def one() -> None:
        path, _, _ = panel.recorder.run(run.id).export("json")
        results.append(path.read_bytes())

    threads = [threading.Thread(target=one) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and len(set(results)) == 1
    json.loads(results[0])
    assert not [p.name for p in run.path.iterdir() if p.name.endswith(".tmp")]


def test_every_export_of_a_csv_run_reads_back_as_its_records(panel: Panel) -> None:
    assert panel.post("/api/scpi", "FORM CSV").status == 200
    meta = capture(panel, profile="SLEEP", seconds=2)
    assert meta["format"] == "CSV"
    run = run_of(panel, meta)
    expected = list(run.records())
    assert len(expected) == meta["stream"]["samples"] > 10
    csv_path, _, _ = run.export("csv")
    assert csv_path == run.path / "stream.txt"
    text_path, _, name = run.export("text")
    assert text_path.name == "export.txt" and name.endswith(".txt")
    assert records_of(text_path.read_text()) == expected == records_of(csv_path.read_text())
    assert json_records(json.loads(run.export("json")[0].read_text())) == expected


def test_every_export_of_a_ring_run_reads_back_as_its_dump(tmp_path: Path) -> None:
    with ring_panel(tmp_path) as panel:
        meta = capture(panel, profile="TRACE")
        run = run_of(panel, meta)
        expected = list(run.records())
        assert len(expected) == 4096
        for kind in ("text", "csv"):
            path, _, _ = run.export(kind)
            assert path.name.startswith("export.")
            assert records_of(path.read_text()) == expected
        document = json.loads(run.export("json")[0].read_text())
        assert list(document["columns"]) == ["t_us", "i_na"]
        assert json_records(document) == expected


def test_an_export_of_an_unknown_format_or_run_is_refused(panel: Panel) -> None:
    meta = capture(panel, profile="STATE", seconds=0.2)
    bad = panel.get(f"/api/runs/{meta['id']}/export?format=xml")
    assert bad.status == 400 and bad.body["error"] == "bad_format"
    for run in ("nope", "20261122T000000Z-STATE-abcdef", "..%2F..%2Fetc"):
        missing = panel.get(f"/api/runs/{run}")
        assert missing.status == 404 and missing.body["error"] == "no_run", run
    assert panel.get(f"/api/runs/{meta['id']}").body == meta


def test_a_numeric_export_is_written_in_thousandths() -> None:
    assert recorder_module._text_lines((5, {"i_ua": -1500, "die_c": 25500})) == [
        "METER t=5 ch=i_ua v=-1.500\n",
        "METER t=5 ch=die_c v=25.500\n",
    ]
    assert recorder_module._csv_lines((5, {"v_mv": 7})) == ["S,5,,0.007,,\n"]
    assert recorder_module._csv_lines(Mark("a", 1, 2)) == ["K,1,2,a\n"]


def test_the_decimation_keeps_every_extreme(panel: Panel) -> None:
    from kleidos_meter.sim import ScriptedWaveform

    panel.sim.waveform = ScriptedWaveform([(0.05, 0.01), (0.05, 0.2)])
    meta = capture(panel, profile="STATE", channels=["CURR", "VOLT"], seconds=1)
    rows = [r for r in run_of(panel, meta).records() if not isinstance(r, Mark)]
    answer = panel.get(f"/api/runs/{meta['id']}/decimated?column=i_na&buckets=10")
    assert answer.status == 200
    body = answer.body
    assert body["column"] == "i_na" and len(body["t_us"]) <= 10
    assert body["t0_us"] == rows[0][0] and body["t1_us"] == rows[-1][0]
    assert sum(body["n"]) == len(rows)
    assert min(body["min"]) == min(v["i_ua"] for _, v in rows)
    assert max(body["max"]) == max(v["i_ua"] for _, v in rows)
    width = body["bucket_us"]
    figures = zip(
        body["t_us"], body["min"], body["max"], body["n"], body["mean"], body["rms"], strict=True
    )
    for start, low, high, n, mean, rms in figures:
        inside = [v["i_ua"] for t, v in rows if start <= t < start + width]
        assert (low, high, n) == (min(inside), max(inside), len(inside))
        assert mean == pytest.approx(statistics.fmean(inside), rel=1e-12)
        assert rms == pytest.approx(math.sqrt(statistics.fmean(v * v for v in inside)), rel=1e-12)
    # Weighed by their counts, the buckets' means are the run's mean.
    weighed = sum(n * mean for n, mean in zip(body["n"], body["mean"], strict=True))
    assert weighed / sum(body["n"]) == pytest.approx(
        statistics.fmean(v["i_ua"] for _, v in rows), rel=1e-12
    )
    window = panel.get(
        f"/api/runs/{meta['id']}/decimated?column=v_uv&buckets=5"
        f"&t0_us={rows[10][0]}&t1_us={rows[20][0]}"
    ).body
    assert sum(window["n"]) == 11


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("column=amps", "bad_column"),
        ("buckets=0", "bad_buckets"),
        ("buckets=10001", "bad_buckets"),
        ("buckets=x", "bad_parameter"),
        ("t0_us=10&t1_us=5", "bad_window"),
    ],
)
def test_a_bad_decimation_is_refused(panel: Panel, query: str, error: str) -> None:
    meta = capture(panel, profile="STATE", seconds=0.2)
    answer = panel.get(f"/api/runs/{meta['id']}/decimated?{query}")
    assert answer.status == 400 and answer.body["error"] == error


def test_a_run_with_no_sample_decimates_to_nothing(panel: Panel) -> None:
    assert panel.post("/api/settings", {"capture": "STATistics"}).status == 200
    meta = capture(panel, profile="STATE", seconds=0.2)
    body = panel.get(f"/api/runs/{meta['id']}/decimated").body
    assert body["t_us"] == [] and body["bucket_us"] is None and body["t0_us"] is None
    assert body["mean"] == body["rms"] == []


def test_the_runs_are_listed_newest_first_and_a_broken_one_says_so(panel: Panel) -> None:
    first = capture(panel, profile="STATE", seconds=0.2)
    second = capture(panel, profile="SLEEP", seconds=0.5)
    broken = panel.recorder.data_dir / "20200101T000000Z-STATE-000000"
    broken.mkdir()
    (broken / "meta.json").write_text("{")
    listed = panel.get("/api/runs").body["runs"]
    ids = [run["id"] for run in listed]
    assert ids[-1] == broken.name and set(ids[:2]) == {first["id"], second["id"]}
    assert listed[-1]["state"] == "unreadable"
    summary = next(run for run in listed if run["id"] == second["id"])
    assert summary["profile"] == "SLEEP" and summary["complete"] is True
    assert summary["samples"] == second["statistics"]["fields"]["samples"]
    assert panel.get("/api/runs?limit=1").body["runs"][0]["id"] in ids[:2]
    assert panel.get("/api/runs?limit=0").status == 400
    assert panel.get(f"/api/runs/{broken.name}").status == 409
    (broken / "meta.json").write_text("[]")
    assert panel.get(f"/api/runs/{broken.name}").body["error"] == "unreadable"
    (broken / "meta.json").unlink()
    (broken / "meta.json").mkdir()  # a file that cannot be read names no absolute path
    refused = panel.get(f"/api/runs/{broken.name}").body
    assert refused["detail"].endswith(f": {broken.name}/meta.json")
    assert str(panel.recorder.data_dir) not in refused["detail"]


def test_no_data_directory_yet_lists_no_run(panel: Panel) -> None:
    assert panel.get("/api/runs").body == {"runs": []}


# -- what the disk and the bounds cost -------------------------------------------------------


def test_a_disk_that_fails_costs_records_counted_never_the_line(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = Path.open

    class Broken(io.BytesIO):
        def write(self, data: Any) -> int:
            raise OSError("disk full")

    def opener(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self.name == "stream.txt" and mode == "wb":
            return Broken()
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opener)
    meta = capture(panel, profile="STATE", seconds=0.5)
    assert meta["stream"]["error"].startswith("writing stream.txt failed")
    assert meta["stream"]["lost"] > 0 and not meta["complete"]
    assert int(meta["statistics"]["fields"]["dropped"]) == 0


def test_records_past_the_pending_bound_are_counted_lost(panel: Panel) -> None:
    panel.recorder.max_pending = 3
    panel.recorder.interval = 60.0  # the writer never runs: only the bound counts
    meta = capture(panel, profile="STATE", seconds=0.2)
    assert meta["stream"]["records"] == 3 and meta["stream"]["lost"] > 0
    assert not meta["complete"]


def test_a_meta_json_that_cannot_be_written_is_reported(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, value: dict[str, Any]) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(recorder_module, "_write_json", refuse)
    meta = capture(panel, profile="STATE", seconds=0.2)
    assert meta["failure"] == "writing meta.json failed: read-only"


def test_a_failure_names_the_runs_file_by_no_absolute_path(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure reaches the page (the Runs view, an ``error`` event): no local path."""
    published: list[tuple[str, dict[str, Any]]] = []

    def refuse(path: Path, value: dict[str, Any]) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(recorder_module, "_write_json", refuse)
    live = panel.recorder.live
    assert live is not None
    real = live.publish

    def publish(kind: str, body: dict[str, Any]) -> None:
        published.append((kind, body))
        real(kind, body)

    monkeypatch.setattr(live, "publish", publish)
    meta = capture(panel, profile="STATE", seconds=0.2)
    failure = meta["failure"]
    assert failure.startswith("writing meta.json failed: Permission denied: ")
    assert failure.endswith(f"{meta['id']}/meta.json")
    assert str(panel.recorder.data_dir) not in failure and ": /" not in failure
    assert ("error", {"kind": "recorder", "detail": failure}) in published
    assert str(panel.recorder.data_dir) not in json.dumps(panel.recorder.state_json())


def test_a_stream_that_cannot_be_written_names_no_absolute_path(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = Path.open

    class Full(io.BytesIO):
        def __init__(self, name: Path) -> None:
            super().__init__()
            self.where = str(name)

        def write(self, data: Any) -> int:
            raise OSError(28, "No space left on device", self.where)

    def opener(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self.name == "stream.txt" and mode == "wb":
            return Full(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opener)
    meta = capture(panel, profile="STATE", seconds=0.3)
    error = meta["stream"]["error"]
    assert error == f"writing stream.txt failed: No space left on device: {meta['id']}/stream.txt"


def test_a_closed_recorder_stops_its_capture_and_closes_once(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE"}).status == 200
    panel.recorder.close()
    meta = panel.recorder.last_meta
    assert meta is not None and meta["ended_by"] == "shutdown"
    panel.recorder.close()


def test_a_timer_whose_capture_was_stopped_first_does_nothing(panel: Panel) -> None:
    assert panel.post("/api/capture/start", {"profile": "STATE", "seconds": 3600}).status == 200
    assert panel.post("/api/capture/stop").status == 200
    assert panel.recorder.last_meta["ended_by"] == "stop"  # type: ignore[index]  # the run ended


def test_completeness_needs_statistics() -> None:
    assert recorder_module._complete({"statistics": None}) is False
    meta = {"statistics": {"fields": {"samples": "x"}}}
    assert recorder_module._complete(meta) is False


# -- serve and the capture command --------------------------------------------------------


def test_serve_stops_a_capture_still_running_and_finishes_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    stop = threading.Event()
    out = io.StringIO()
    clock = FakeClock()
    sims: list[SimulatedMeter] = []

    def sim_factory(*args: Any, **kwargs: Any) -> SimulatedMeter:
        sims.append(SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep))
        return sims[0]

    result: list[int] = []
    runs = tmp_path / "runs"
    monkeypatch.setattr("kleidos_meter.panel.serve.SimulatedMeter", sim_factory)
    try:
        thread = threading.Thread(
            target=lambda: result.append(
                serve.main(["--sim", "--http", "0", "--data-dir", str(runs)], stop=stop, out=out)
            )
        )
        thread.start()
        until(lambda: "panel API at" in out.getvalue(), "serve's start")
        url = out.getvalue().split("panel API at ")[1].split()[0]
        port = int(url.rsplit(":", 1)[1].split("/")[0])
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"profile": "STATE"})
        connection.request(
            "POST",
            "/api/capture/start",
            body,
            {"X-Kleidos-Meter": "1", "Content-Type": "application/json"},
        )
        assert connection.getresponse().status == 200
        connection.close()
        stop.set()
        thread.join(30)
    finally:
        monkeypatch.undo()
    assert result == [0]
    text = out.getvalue()
    assert f"recordings in {runs.resolve()}" in text
    assert "stopped the capture and finished run" in text
    (run,) = runs.iterdir()
    assert json.loads((run / "meta.json").read_text())["ended_by"] == "shutdown"


def sim_on(clock: FakeClock) -> Any:
    return lambda: SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)


def test_the_capture_command_records_one_run(tmp_path: Path) -> None:
    clock = FakeClock()
    out = io.StringIO()
    status = capture_main(
        [
            "--sim",
            "--profile",
            "state",
            "--seconds",
            "2",
            "--channels",
            "CURR,VOLT",
            "--label",
            "cli",
            "--out",
            str(tmp_path / "runs"),
        ],
        make_sim=sim_on(clock),
        clock=clock,
        out=out,
    )
    assert status == 0, out.getvalue()
    (run,) = (tmp_path / "runs").iterdir()
    meta = json.loads((run / "meta.json").read_text())
    assert meta["complete"] and meta["label"] == "cli" and meta["ended_by"] == "seconds"
    assert meta["host"]["tool"].startswith("kleidos-meter capture ")
    text = out.getvalue()
    assert f"capture: recording {run}" in text
    assert "wrote back profile" in text and "complete" in text.splitlines()[-1]


def test_the_capture_command_runs_a_ring_until_it_is_full(tmp_path: Path) -> None:
    clock = FakeClock()

    def small_ring() -> SimulatedMeter:
        return SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, ring_capacity=2048)

    out = io.StringIO()
    status = capture_main(
        ["--sim", "--profile", "TRACE", "--out", str(tmp_path / "runs"), "--channels", "DEF"],
        make_sim=small_ring,
        clock=clock,
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "trace=2048" in out.getvalue() and "ended by ended_by_itself" in out.getvalue()


def test_the_capture_command_stops_early_when_told(tmp_path: Path) -> None:
    import threading

    clock = FakeClock()
    stop = threading.Event()
    stop.set()
    out = io.StringIO()
    status = capture_main(
        ["--sim", "--profile", "STATE", "--seconds", "3600", "--out", str(tmp_path / "r")],
        make_sim=sim_on(clock),
        clock=clock,
        stop=stop,
        out=out,
    )
    assert status == 0 and "ended by stop" in out.getvalue()


@pytest.mark.parametrize(
    "argv",
    [
        ["--profile", "STATE", "--seconds", "1"],  # no port
        ["--sim", "--profile", "NOPE", "--seconds", "1"],
        ["--sim", "--profile", "STATE"],  # a stream needs --seconds
    ],
)
def test_the_capture_command_refuses_a_bad_command_line(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KLEIDOS_METER_PORT", raising=False)
    with pytest.raises(SystemExit) as stopped:
        capture_main(argv)
    assert stopped.value.code == 2


def test_the_capture_command_reports_a_refused_capture(tmp_path: Path) -> None:
    clock = FakeClock()
    err = io.StringIO()
    argv = ["--sim", "--profile", "TRACE", "--channels", "VOLT", "--out", str(tmp_path / "r")]
    assert capture_main(argv, make_sim=sim_on(clock), clock=clock, err=err) == 1
    assert "# the capture was refused" in err.getvalue() and "# -2" in err.getvalue()


def test_the_capture_command_on_a_busy_port_names_the_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def busy(port: str) -> Any:
        raise PortBusyError(f"{port} is held")

    err = io.StringIO()
    argv = ["--port", "/dev/cu.fake", "--profile", "STATE", "--seconds", "1"]
    assert capture_main(argv, open_transport=busy, err=err) == 1
    assert err.getvalue() == "# /dev/cu.fake is held\n"
    lockinfo.write_info("/dev/cu.fake", {"holder": "kleidos-meter serve"})
    err = io.StringIO()
    assert capture_main(argv, open_transport=busy, err=err) == 1
    assert err.getvalue().startswith("# the port is held by kleidos-meter serve (pid ")


def test_the_capture_command_on_a_port_leaves_a_note_while_it_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    clock = FakeClock()
    notes: list[dict[str, object] | None] = []
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    real_read = sim.read

    def reading(size: int) -> bytes:
        if not notes or notes[-1] is None:
            notes.append(lockinfo.read_holder("/dev/cu.fake"))
        return real_read(size)

    sim.read = reading  # type: ignore[method-assign]  # a planted read
    argv = ["--port", "/dev/cu.fake", "--profile", "STATE", "--seconds", "1"]
    argv += ["--out", str(tmp_path / "runs")]
    assert capture_main(argv, open_transport=lambda port: sim, clock=clock) == 0
    held = [note for note in notes if note is not None]
    assert held and held[0]["holder"] == "kleidos-meter capture"
    assert lockinfo.read_holder("/dev/cu.fake") is None  # removed on the way out


def test_the_capture_command_goes_on_when_its_note_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(port: str, info: dict[str, object]) -> Path:
        raise OSError("no runtime directory")

    monkeypatch.setattr(lockinfo, "write_info", refuse)
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    err = io.StringIO()
    argv = ["--port", "/dev/cu.fake", "--profile", "STATE", "--seconds", "1"]
    argv += ["--out", str(tmp_path / "runs")]
    assert capture_main(argv, open_transport=lambda port: sim, clock=clock, err=err) == 0
    assert "# could not leave a note" in err.getvalue()


def test_the_capture_command_on_a_silent_instrument_exits_1(tmp_path: Path) -> None:
    class Silent:
        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int) -> bytes:
            return b""

        def close(self) -> None:
            pass

    err = io.StringIO()
    argv = ["--port", "/dev/cu.fake", "--profile", "STATE", "--seconds", "1"]
    argv += ["--out", str(tmp_path / "r"), "--ready-timeout", "0.1"]
    assert capture_main(argv, open_transport=lambda port: Silent(), err=err) == 1
    assert err.getvalue().startswith("# the instrument is not answering")


def test_the_capture_command_reports_a_line_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_open(self: Any) -> None:
        raise MeterError("the identity was refused")

    monkeypatch.setattr(serve.Session, "open", broken_open)
    clock = FakeClock()
    err = io.StringIO()
    argv = ["--sim", "--profile", "STATE", "--seconds", "1", "--out", str(tmp_path / "r")]
    assert capture_main(argv, make_sim=sim_on(clock), clock=clock, err=err) == 1
    assert err.getvalue() == "# the identity was refused\n"


def test_an_incomplete_capture_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder_module, "_complete", lambda meta: False)
    clock = FakeClock()
    out = io.StringIO()
    argv = ["--sim", "--profile", "STATE", "--seconds", "0.5", "--out", str(tmp_path / "r")]
    assert capture_main(argv, make_sim=sim_on(clock), clock=clock, out=out) == 1
    assert out.getvalue().splitlines()[-1].endswith("INCOMPLETE")


def test_the_cli_dispatches_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    from kleidos_meter import capture_cli

    def dispatched(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(capture_cli, "main", dispatched)
    assert cli.main(["capture", "--sim", "--profile", "STATE"]) == 0
    assert seen == [["--sim", "--profile", "STATE"]]


def test_the_crc_of_a_kept_dump_is_its_own() -> None:
    block = b"METER_DUMP count=0 t0_us=1 period_us=1 fmt=raw20\n"
    end = f"METER_DUMP_END crc32={zlib.crc32(block):08x} bytes={len(block)}\n".encode()
    assert check_dump(block + end) is True


def test_the_capture_command_opens_a_real_port_through_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kleidos_meter import capture_cli

    opened: list[str] = []

    def transport(port: str) -> str:
        opened.append(port)
        return "port"

    monkeypatch.setattr(capture_cli, "SerialTransport", transport)
    opened_by: object = capture_cli._open("/dev/cu.fake-meter")
    assert opened_by == "port"
    assert opened == ["/dev/cu.fake-meter"]


def test_the_capture_command_on_the_real_clock_simulator(tmp_path: Path) -> None:
    import threading

    result: list[int] = []
    out = io.StringIO()
    argv = ["--sim", "--profile", "STATE", "--seconds", "0.3", "--out", str(tmp_path / "r")]
    thread = threading.Thread(target=lambda: result.append(capture_main(argv, out=out)))
    thread.start()  # off the main thread: no signal handler is installed
    thread.join(30)
    assert result == [0], out.getvalue()


def test_sigint_stops_the_capture_command_on_the_main_thread(tmp_path: Path) -> None:
    import os
    import signal
    import threading

    clock = FakeClock()
    out = io.StringIO()
    previous = signal.getsignal(signal.SIGINT)
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        argv = ["--sim", "--profile", "STATE", "--seconds", "3600", "--out", str(tmp_path / "r")]
        status = capture_main(argv, make_sim=sim_on(clock), clock=clock, out=out)
    finally:
        timer.cancel()
    assert status == 0 and "ended by stop" in out.getvalue()
    assert signal.getsignal(signal.SIGINT) is previous


def test_a_meta_json_the_capture_command_cannot_write_is_kept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, value: dict[str, Any]) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(recorder_module, "_write_json", refuse)
    clock = FakeClock()
    out = io.StringIO()
    argv = ["--sim", "--profile", "STATE", "--seconds", "0.3", "--out", str(tmp_path / "r")]
    assert capture_main(argv, make_sim=sim_on(clock), clock=clock, out=out) == 0
    assert "complete" in out.getvalue().splitlines()[-1]


def test_a_setting_the_instrument_refuses_before_the_capture_leaves_no_run(panel: Panel) -> None:
    assert panel.post("/api/scpi", "CAL:ZERO 30").status == 200  # a null window: R refuses
    answer = panel.post("/api/capture/start", {"profile": "SLEEP"})
    assert answer.status == 409 and answer.body["error"] == "refused"
    assert answer.body["errors"]  # the profile is refused while the null window is open
    assert not panel.recorder.data_dir.exists()
    panel.post("/api/abort")


def test_a_run_read_back_skips_what_is_not_a_record(tmp_path: Path) -> None:
    path = tmp_path / "20261122T000000Z-STATE-abcdef"
    path.mkdir()
    (path / "meta.json").write_text(json.dumps({"id": path.name, "format": "TEXT"}))
    (path / "stream.txt").write_text(
        "a line that is no record\n"
        "METER_MARK mark=a n=0 t=10\n"
        "METER t=10 ch=i_ua v=1.000\n"
        "METER t=10 ch=v_mv v=2.000\n"
        "METER_MARK mark=b n=1 t=11\n"
        "METER t=20 ch=i_ua v=3.000\n"
        "METER t=30 ch=v_mv v=4.000\n"
    )
    run = Run(path)
    assert list(run.records()) == [
        Mark("a", 0, 10),
        (10, {"i_ua": 1000, "v_mv": 2000}),
        Mark("b", 1, 11),
        (20, {"i_ua": 3000}),
        (30, {"v_mv": 4000}),
    ]
    body = run.decimate("i_na", 1, 0, 100)
    assert (body["min"], body["max"], body["n"]) == ([1000], [3000], [2])
    assert body["mean"] == [2000.0] and body["rms"] == [math.sqrt((1000**2 + 3000**2) / 2)]
    assert run.decimate("i_na", 2)["t0_us"] is None  # no first or last in this meta.json
    assert run.summary()["id"] == path.name


def test_malformed_records_are_no_records() -> None:
    for line in [
        "METER t=x ch=i_ua v=1.000",
        "METER t=1 ch=i_ua v=abc",
        "METER_MARK mark=a n=x t=1",
        "METER_MARK n=1 t=1",
        "S,1,2",
        "K,1,2",
    ]:
        assert parse_record(line) is None, line


def test_a_capture_its_seconds_end_at_once_still_names_its_run(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start`` answers the state as the capture began, whatever its timer did since.

    The timer runs inline here and stops the capture before ``start`` returns,
    the order a short ``--seconds`` can take on a busy host.
    """
    real_thread = threading.Thread

    class InlineTimer(real_thread):  # type: ignore[misc, valid-type]  # Thread, patched
        def start(self) -> None:
            if self.name == "kleidos-meter-timer":
                self.run()
            else:
                super().start()

    recorder = panel.recorder
    monkeypatch.setattr(threading, "Thread", InlineTimer)
    monkeypatch.setattr(recorder, "_timer", lambda capture: recorder.stop("seconds"))
    state = recorder.start("STATE", None, 5, "")
    assert state["state"] == "running" and state["run"] is not None
    assert recorder.state == "idle" and recorder.last_meta is not None
    assert recorder.last_meta["id"] == state["run"] and recorder.last_meta["ended_by"] == "seconds"
