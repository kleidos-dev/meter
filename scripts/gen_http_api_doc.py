#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write docs/http-api.md, the panel's HTTP API, from the schema the panel serves.

    python3 scripts/gen_http_api_doc.py            # write the page
    python3 scripts/gen_http_api_doc.py --check    # fail when the page is not what it would write

The routes, the settings, the profiles, the relay orders and the sequences are
``kleidos_meter.panel.schema``'s (``GET /api/schema`` serves the same), and the
limits and the event kinds the constants of the modules that hold them, so
the page says nothing of its own but the conventions every route shares: what a
report becomes in JSON, what each status means and what the guards refuse. The
schema is held to the firmware's command table by ``host/tests/test_panel_schema.py``,
its routes to what they answer by ``host/tests/test_panel_contract.py``, and
this page to the schema by ``host/tests/test_panel_doc.py``.

No third-party module: the panel is the host package's, imported from the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "host")]

from doc_page import anchor, code, header, run, table  # noqa: E402
from kleidos_meter.panel.executor import MAX_MESSAGE  # noqa: E402
from kleidos_meter.panel.guards import MUTATION_HEADER, MUTATION_VALUE  # noqa: E402
from kleidos_meter.panel.http import MAX_BODY, MAX_REQUESTS  # noqa: E402
from kleidos_meter.panel.live import EVENT_KINDS  # noqa: E402
from kleidos_meter.panel.schema import SCHEMA, Route, Schema, Setting  # noqa: E402
from kleidos_meter.panel.serve import DEFAULT_BIND, DEFAULT_HTTP  # noqa: E402

#: The page.
PAGE = ROOT / "docs" / "http-api.md"

#: What each status says, as the panel answers it.
STATUSES: tuple[tuple[str, str], ...] = (
    ("200", "done; the body is what the route answers"),
    (
        "400",
        "the request is malformed, or refused before anything was sent (a value of the wrong"
        " form, a message the instrument would discard, a GET that would act); or the instrument"
        " queued a command error (`-1xx`)",
    ),
    (
        "403",
        f"a change without `{MUTATION_HEADER}: {MUTATION_VALUE}` (`header_missing`), or from a"
        " page of another origin (`foreign_origin`)",
    ),
    ("404", "no such route, relay order, sequence or run"),
    ("405", "the route does not take the method; a GET never changes the instrument"),
    (
        "409",
        "the instrument refused it (an execution or device error: `-2xx`, `-3xx`, `100` and up,"
        " in `errors`), or serve did: a capture or a sequence runs, a window serve did not open"
        " is open, a pin map is being applied, a relay is not at rest, there is nothing to stop",
    ),
    ("411, 413", f"the body has no `Content-Length`, or is longer than {MAX_BODY} bytes"),
    ("421", "the `Host` header does not name the loopback"),
    ("423", "another controller holds the line: `holder` names it"),
    ("500", "a bug of serve's own, in its log"),
    ("502", "the instrument answered something other than what was asked"),
    (
        "503",
        f"{MAX_REQUESTS} requests run already, the queue of messages is full, the line is down"
        " (`Retry-After` says when to come back) or serve is stopping",
    ),
    ("504", "the answer did not come in time"),
)

#: What each event kind carries.
EVENTS: dict[str, str] = {
    "samples": "every 100 ms, the samples since the last batch as columns of integers: `n0` (the"
    " first row's index in the capture), `count`, `t_us`, and `i_na`, `v_uv`, `p_uw`, `die_mc`"
    " of the channels it holds, `null` in a row that lacks one",
    "mark": "a mark record in its place among the batches: `label`, `n`, `t_us`",
    "status": "`kind`: `hello` (the first event of a stream), `poll` (the last status poll),"
    " `link`, `reboot`, `capture`, `passthrough`, `settings` (`changed`: the settings a message"
    " that was written changes, whoever sent it)",
    "error": "`kind`: `device` (errors nobody asked for), `overflow` (a line too long to hold),"
    " `recorder` (a run's files could not be written)",
    "sequence": "the sequence's state, as `GET /api/sequences` answers it, at each change",
    "ring": "a ring capture's fill (`samples`, `running`) and its dump's progress (`dump`)",
    "lost": "to one stream only: the `events` it missed, and the `samples` among them, `null`"
    " when unknown",
}

#: Where each kind of argument goes.
WHERE = {"path": "path", "query": "query", "json": "JSON body", "text": "text body"}


def title_of(route: Route) -> str:
    """A route's heading and how every page names it: ``GET /api/schema``."""
    return f"{route.method} {route.path}"


def form_of(setting: Setting) -> str:
    """What a setting's value is."""
    if setting.kind == "choice":
        return "one of " + code(setting.choices)
    if setting.kind == "channels":
        return f"a list of {code(setting.choices)}, or `DEFault`"
    words = {
        "integer": "an integer",
        "number": "a number",
        "number_or_off": "a number, or `OFF`",
        "boolean": "`true` or `false`",
    }
    unit = f", {setting.unit}" if setting.unit else ""
    only = f": one of {code(setting.choices)}" if setting.choices else ""
    return words[setting.kind] + unit + only


def route_lines(route: Route) -> list[str]:
    """One route's section."""
    lines = [f"### `{title_of(route)}`", "", route.summary[0].upper() + route.summary[1:] + "."]
    if route.takes:
        rows = [
            [
                f"`{arg.name}`",
                WHERE[arg.where],
                "yes" if arg.required else "no",
                arg.takes,
            ]
            for arg in route.takes
        ]
        lines += ["", *table(["Takes", "In the", "Required", "What it is"], rows)]
    lines += ["", f"Answers {route.answers}."]
    if route.uses:
        lines += ["", f"Sends {code(route.uses)}."]
    return [*lines, ""]


def page(schema: Schema = SCHEMA) -> str:
    """The page's text.

    :param schema: The declarations it is written from.
    """
    base = f"http://{DEFAULT_BIND}:{DEFAULT_HTTP}"
    lines = [
        *header("gen_http_api_doc.py", "kleidos_meter.panel.schema"),
        "# The panel's HTTP API",
        "",
        "What `kleidos-meter serve` answers, route by route. This page is written by"
        " `scripts/gen_http_api_doc.py` from the schema the panel itself serves at"
        " `GET /api/schema`; a test fails when the two differ. How to start the panel, use it"
        " and stop it, with worked examples, is in [panel.md](panel.md).",
        "",
        "## Conventions",
        "",
        f"The address is `{base}/` unless serve was told another (`--bind`, `--http`). It is"
        " unauthenticated and listens on the loopback only. Every answer is one JSON object but"
        " the event stream and an export.",
        "",
        f"- **A change is a POST with `{MUTATION_HEADER}: {MUTATION_VALUE}`.** A GET never"
        " changes the instrument.",
        "- **The `Host` must name the loopback** (`localhost`, `127.0.0.1` or `[::1]`), and no"
        " answer approves another origin.",
        "- **A report is an object of its own keys.** A canonical integer is a JSON integer in"
        " the unit its key names (`i_na`, `vbus_uv`, `hold_s`), `-` is `null`, anything else is"
        " the instrument's text, and so are an integer beyond 2^53 and the keys "
        + code(sorted(schema.text_keys))
        + ".",
        "- **`errors`** are what the instrument queued for that message, oldest first, each"
        " `{code, text}`. What it queues on its own (a hold's release, `113` to `115`, relays a"
        " reset released, `116`, and a message an overrun discarded, `-363`) is never a"
        " reply's: whichever message drains it, it goes to `/api/events` as an `error` of"
        " kind `device`, and to the running capture's `meta.json`.",
        f"- **A message** is at most {MAX_MESSAGE} bytes with the `;:SYST:ERR:ALL?` serve adds"
        " to it, printable ASCII, every quote it opens closed (the error query after an"
        " open one would be quoted and get no answer).",
        "- **While a sequence runs** every change answers 409 (`sequence_running`) but"
        " `POST /api/abort`, `POST /api/sequences/answer` and `POST /api/sequences/abort`.",
        "- **While a pin map is applied and checked** every change answers 409"
        " (`pins_changing`) but `POST /api/abort`.",
        "- **While another controller holds the line** every route that talks to the instrument"
        " answers 423; the ones that say so below do not.",
        "",
        "A refusal's body holds `error`, a short word, and `detail`, a sentence.",
        "",
        *table(["Status", "Says"], [[status, says] for status, says in STATUSES]),
        "",
        "## Routes",
        "",
        *table(
            ["Route", "Changes", "Talks to the instrument", "Does"],
            [
                [
                    f"[`{title_of(route)}`](#{anchor(title_of(route))})",
                    "yes" if route.mutates else "no",
                    "yes" if route.sends else "no",
                    route.summary,
                ]
                for route in schema.routes
            ],
        ),
        "",
    ]
    for route in schema.routes:
        lines += route_lines(route)
        if route.path == "/api/events":
            lines += [
                *table(["Event", "Carries"], [[f"`{kind}`", EVENTS[kind]] for kind in EVENT_KINDS]),
                "",
                "An event's id is `<epoch>-<seq>-<rows>`. A browser that reconnects with"
                " `Last-Event-ID` goes on right after it, or is told with `lost` what it missed.",
                "",
            ]
    lines += [
        "## Settings",
        "",
        "What `GET /api/settings` reads and `POST /api/settings` writes, in the order they are"
        " written. A value is checked for its form, never for its range: that is the"
        " instrument's to refuse. The flags are the instrument's"
        " ([scpi.md](scpi.md)): `D` needs the INA228, `R` is refused while a capture or a null"
        " window runs, `C` also until one that ended by itself is closed.",
        "",
        *table(
            ["Setting", "Value", "Written with", "Flags", "What it is"],
            [
                [
                    f"`{setting.name}`",
                    form_of(setting) if setting.writable else "read only",
                    f"`{setting.pattern}`",
                    f"`{setting.flags}`",
                    setting.summary,
                ]
                for setting in schema.settings
            ],
        ),
        "",
        "## Profiles",
        "",
        *table(
            ["Profile", "A sample every", "Goes to", "Channels by default"],
            [
                [
                    f"`{profile.name}`",
                    f"{profile.period_us} µs",
                    "the ring" if profile.ring else "the stream",
                    code(profile.channels),
                ]
                for profile in schema.profiles
            ],
        ),
        "",
        "## Relay orders",
        "",
        "The `{relay}` of `POST /api/relays/{relay}`. A flag goes out only when the request sets"
        " it.",
        "",
        *table(
            ["Order", "Takes", "Flags", "Sends", "What it does"],
            [
                [
                    f"`{relay.name}`",
                    f"`state`: {code(relay.choices)}" if relay.choices else "`ms`",
                    code(relay.options),
                    f"`{relay.pattern}`",
                    f"{relay.label}: {relay.summary}",
                ]
                for relay in schema.relays
            ],
        ),
        "",
        "## Sequences",
        "",
        "The `name` and `params` of `POST /api/sequences/run`. What each one moves is in"
        " [panel.md](panel.md#what-each-sequence-moves).",
        "",
        *table(
            ["Sequence", "Parameters", "What it does", "Sends"],
            [
                [
                    f"`{sequence.name}`",
                    "; ".join(
                        f"`{param.name}`: {param.low} to {param.high} {param.unit}"
                        f" ({param.default})"
                        for param in sequence.params
                    )
                    or "–",
                    sequence.summary,
                    code(sequence.uses),
                ]
                for sequence in schema.sequences
            ],
        ),
        "",
        "## What a GET never asks",
        "",
        "`GET /api/query` takes only queries that read without acting. It refuses a command, a"
        " query the `R` flag marks, a header the instrument does not have, and the queries whose"
        " reading clears what they read: "
        + code(sorted(schema.clearing_queries))
        + ". `POST /api/scpi` takes them all but a ring dump, which the recorder fetches to a"
        " file.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Write the page, or check it.

    :returns: 0, or 1 when ``--check`` finds the page is not the generator's.
    """
    return run(argv, page, PAGE, __doc__.splitlines()[0], "the schema")


if __name__ == "__main__":
    sys.exit(main())
