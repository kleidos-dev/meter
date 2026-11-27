// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The panel's entry point: read the schema and the status, build the
 * status bar and every view, show the one the fragment names, and route the
 * live events to them.
 *
 * Nothing here is fetched from anywhere but the page's own origin, and
 * nothing grows with time: the samples live in the Live view's fixed ring,
 * the notices list keeps its last `NOTICES` entries, the console its last
 * fifty messages and the runs list its latest hundred. A view that is not
 * shown reads nothing from the instrument.
 */

import { get, openEvents, useMutationHeader } from "./api.js";
import { noiseOf } from "./chart-math.js";
import { ConsoleView } from "./console.js";
import { need } from "./dom.js";
import { HealthView } from "./health.js";
import { LiveView } from "./live.js";
import { PinsView } from "./pins.js";
import { RelaysView } from "./relays.js";
import { RunsView } from "./runs.js";
import { SequencesView } from "./sequences.js";
import { Session } from "./session.js";
import { SettingsView } from "./settings.js";
import { StatusBar } from "./status.js";
import { Views } from "./views.js";

/** The most notices the page keeps. */
const NOTICES = 20;

/** The device errors after which the relays moved by themselves: the hold, a reset. */
const RELAY_RELEASES = new Set([113, 114, 115, 116]);

/**
 * Add a line to the notices, forgetting the oldest beyond `NOTICES`.
 *
 * @param {HTMLElement} list The notices' list.
 * @param {string} text The line.
 */
function notice(list, text) {
    const item = document.createElement("li");
    const time = new Date().toLocaleTimeString("en-GB", { hour12: false });
    item.textContent = `${time} ${text}`;
    list.prepend(item);
    while (list.children.length > NOTICES) {
        list.lastElementChild?.remove();
    }
}

/**
 * Read `/api/status` into the status bar, the session and the Live view.
 *
 * @param {StatusBar} status The status bar.
 * @param {Session} session The session's state.
 * @param {LiveView | null} live The Live view, once built.
 */
async function refreshStatus(status, session, live) {
    const answer = await get("/api/status");
    if (!answer.ok) {
        status.setLink("down", false, `serve answered ${answer.status}`);
        return;
    }
    const body = answer.body;
    status.setInstrument(body.instrument);
    status.setPoll(body.status);
    status.setController({ holder: body.held ?? null });
    status.setLink(body.link?.up ? "up" : "down", Boolean(body.link?.up));
    session.update({
        instrument: body.instrument ?? null,
        status: body.status ?? null,
        held: body.held ?? null,
        capture: String(body.capture?.state ?? "idle"),
    });
    live?.capture(body.capture);
}

/** Build the page and keep it current. */
async function main() {
    const status = new StatusBar(document);
    const notices = need(document, "#notices", HTMLElement);
    const schemaAnswer = await get("/api/schema");
    if (!schemaAnswer.ok) {
        status.setLink("down", false, "serve gave no schema");
        notice(notices, `schema: ${schemaAnswer.status} ${schemaAnswer.body.detail ?? ""}`);
        return;
    }
    const schema = schemaAnswer.body;
    useMutationHeader(schema.mutation_header);
    const session = new Session();
    const views = new Views(document, "live");
    const live = new LiveView(need(document, "#live", HTMLElement), schema, status);
    const sequences = new SequencesView(need(document, "#sequences", HTMLElement), schema, session);
    const runs = new RunsView(need(document, "#runs", HTMLElement), async (meta) => {
        const why = await live.reopen(meta);
        if (why === null) {
            location.hash = "#live";
        }
        return why;
    });
    const readNoise = async () => {
        const answer = await get(`/api/query?q=${encodeURIComponent("SENS:PROF:REP?")}`);
        const fields = answer.ok ? answer.body.fields?.[0] : null;
        status.setNoise(noiseOf(fields), answer.ok);
    };
    const readPins = async () => status.setPins(await get("/api/pins"));
    const relays = new RelaysView(need(document, "#relays", HTMLElement), schema, session, () => void readPins());
    const pins = new PinsView(need(document, "#pins", HTMLElement), session, status);
    const settings = new SettingsView(need(document, "#settings", HTMLElement), schema, session, status);
    const health = new HealthView(need(document, "#health", HTMLElement));
    new ConsoleView(need(document, "#console", HTMLElement));
    for (const [name, missing] of [
        ["sequence", sequences.missing],
        ["relay", relays.missing],
        ["setting", settings.missing],
    ]) {
        for (const item of /** @type {string[]} */ (missing)) {
            notice(notices, `the page has no control for ${name} ${item}`);
        }
    }
    views.on("runs", { shown: () => void runs.refresh() });
    views.on("relays", { shown: () => relays.shown(), hidden: () => relays.hidden() });
    views.on("pins", { shown: () => pins.shown() });
    views.on("settings", { shown: () => settings.shown() });
    views.on("health", { shown: () => health.shown() });
    views.on("sequences", { shown: () => void sequences.refresh() });
    views.start();

    await refreshStatus(status, session, live);
    const calibrate = async () => status.setCalibration(await get("/api/calibration"));
    await calibrate();
    void readPins();
    void readNoise();
    void sequences.refresh();

    openEvents(
        {
            samples: (batch) => live.samples(batch),
            mark: (mark) => live.addMark(mark),
            ring: (ring) => live.ringFill(ring),
            lost: (lost) => {
                status.addLost(typeof lost.samples === "number" ? lost.samples : 0);
                notice(notices, `the page missed ${lost.events ?? "some"} events`);
                void readNoise();
            },
            sequence: (state) => {
                status.setController({ sequence: state.state === "idle" ? null : state.sequence });
                sequences.update(state);
                if (state.state === "idle") {
                    void readPins(); // a sequence's null path verifies the relays
                }
            },
            error: (error) => {
                const entries = Array.isArray(error.errors) ? error.errors : [];
                const detail = entries.length > 0
                    ? entries.map((/** @type {any} */ e) => `${e.code} ${e.text}`).join("; ")
                    : (error.detail ?? error.discarded ?? "");
                notice(notices, `${error.kind}: ${detail}`);
                if (entries.some((/** @type {any} */ e) => RELAY_RELEASES.has(e.code)) && views.isShown("relays")) {
                    void relays.refresh();
                }
            },
            status: (event) => {
                if (event.kind === "hello") {
                    status.setPoll(event.poll?.status);
                    session.update({ status: event.poll?.status ?? null });
                    live.capture(event.capture);
                } else if (event.kind === "poll") {
                    status.setPoll(event.status);
                    session.update({ status: event.status ?? null });
                } else if (event.kind === "capture") {
                    live.capture(event);
                    const state = String(event.state ?? "idle");
                    session.update({ capture: state });
                    if (state === "idle") {
                        runs.stale = true;
                        if (views.isShown("runs")) {
                            void runs.refresh();
                        }
                    }
                } else if (event.kind === "settings") {
                    const changed = Array.isArray(event.changed) ? event.changed : [];
                    if (changed.includes("profile") || changed.includes("average")) {
                        void readNoise();
                        settings.changed(changed, views.isShown("settings"));
                    }
                } else if (event.kind === "passthrough") {
                    notice(notices, `passthrough: ${event.state ?? event.event ?? "changed"}`);
                    if (event.state !== "refused") {
                        live.setLiveNoise(null); // the client's samples, taken as it chose
                        settings.forgetNoise();
                    }
                    void refreshStatus(status, session, live).then(readNoise);
                } else if (event.kind === "reboot") {
                    notice(notices, "the instrument rebooted");
                    settings.forgetNoise();
                    void refreshStatus(status, session, live).then(calibrate).then(readPins).then(readNoise);
                    if (views.isShown("pins")) {
                        void pins.refresh();
                    }
                } else if (event.kind === "link") {
                    status.setLink(event.up ? "up" : "down", Boolean(event.up));
                    notice(notices, `the line went ${event.up ? "up" : "down"}`);
                }
            },
        },
        (open) => {
            if (!open) {
                status.setLink("reconnecting", false, "to serve");
            } else {
                // What changed while the events were not read is asked for again.
                void refreshStatus(status, session, live).then(readNoise);
            }
        },
    );
}

void main();
