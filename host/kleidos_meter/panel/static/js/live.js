// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Live view: pick a profile and its channels, start, mark and stop
 * a capture serve records, watch it on the live chart, and read its
 * statistics once it ends.
 *
 * The samples come from the live events (`samples`, `mark`) into one
 * `SampleRing` of fixed capacity, so the page holds at most `RING_CAPACITY`
 * samples however long it stays open. A ring profile streams nothing while
 * it runs: its fill is shown from the `ring` events, and once it ends the
 * chart shows the run's trace from the server's count, mean, root mean
 * square, minimum and maximum per bucket (`/api/runs/<run>/decimated`).
 *
 * The INA228's averaging, when a capture was taken with it, is named on the
 * chart, in its readout and in its exports: it changed the samples, as the
 * display smoothing never does.
 *
 * The chart's display smoothing (chart.js, `setSmoothing()`) is chosen per
 * chart: the live chart and a run's history chart each keep their own, off
 * until a person picks one, remembered in this browser (`localStorage`, when
 * the browser allows it).
 *
 * "View as PNG" and "View as CSV" save what the chart shows (chart.js,
 * `exportView()`), made in the page and downloaded from a `data:` URL, so
 * nothing is asked of serve: of the live chart as `live`, of a run's history
 * chart as `run <id>` with the run's own instrument. A run's own exports, of
 * its recorded samples, are the Runs view's.
 */

import { get, post, refusalText } from "./api.js";
import { LiveChart } from "./chart.js";
import {
    CHANNELS,
    MarkList,
    SampleRing,
    TimeView,
    batchChangesView,
    bucketOf,
    SMOOTHING,
    channelOf,
    formatQuantity,
    mergeBuckets,
    noiseCaption,
    rowsMissed,
    viewFileName,
} from "./chart-math.js";

/** The most samples the page keeps: five minutes of `STATe` and then some. */
export const RING_CAPACITY = 1 << 18;

/** The most marks the page keeps. */
const MARK_CAPACITY = 1024;

/** The buckets a ring capture's trace is fetched in. */
const RING_BUCKETS = 4000;

/** The window presets, microseconds, and their labels. */
export const WINDOWS = Object.freeze([
    { us: 1e6, label: "1 s" },
    { us: 10e6, label: "10 s" },
    { us: 60e6, label: "1 min" },
    { us: 300e6, label: "5 min" },
]);

/** Where each chart's smoothing is remembered, by chart: `live` or `history`. */
export const SMOOTHING_KEY = "kleidos-meter.smoothing.";

/**
 * A chart's remembered smoothing, off when none is or the browser keeps nothing.
 *
 * @param {string} chart `live` or `history`.
 * @returns {number} The window, microseconds, one of `SMOOTHING`'s.
 */
export function rememberedSmoothing(chart) {
    let stored = null;
    try {
        stored = window.localStorage.getItem(SMOOTHING_KEY + chart);
    } catch {
        // a browser that keeps nothing for this page: the default
    }
    const us = Number(stored);
    return SMOOTHING.some((s) => s.us === us) ? us : 0;
}

/**
 * Remember a chart's smoothing, if the browser keeps anything for this page.
 *
 * @param {string} chart `live` or `history`.
 * @param {number} us The window, microseconds.
 */
function rememberSmoothing(chart, us) {
    try {
        window.localStorage.setItem(SMOOTHING_KEY + chart, String(us));
    } catch {
        // nothing kept: the choice holds until the page is left
    }
}

/**
 * Hand the browser a file to save, from a `data:` URL: no request leaves the page.
 *
 * @param {string} name The file's name.
 * @param {string} url Its content.
 */
function save(name, url) {
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
}

/**
 * The statistics shown after a capture: a `FETCh:STATistics?` key, its
 * label, and how its value is written.
 *
 * @type {readonly {key: string, label: string, unit?: string, scale?: number}[]}
 */
const STATISTICS = Object.freeze([
    { key: "samples", label: "Samples" },
    { key: "dropped", label: "Dropped" },
    { key: "read_err", label: "Read errors" },
    { key: "dur_us", label: "Duration", unit: "s", scale: 1e-6 },
    { key: "mean_na", label: "Mean current", unit: "A", scale: 1e-9 },
    { key: "sd_na", label: "Current SD", unit: "A", scale: 1e-9 },
    { key: "min_na", label: "Minimum current", unit: "A", scale: 1e-9 },
    { key: "max_na", label: "Maximum current", unit: "A", scale: 1e-9 },
    { key: "mean_charge_na", label: "Mean from charge", unit: "A", scale: 1e-9 },
    { key: "vmin_uv", label: "Minimum bus voltage", unit: "V", scale: 1e-6 },
    { key: "vmax_uv", label: "Maximum bus voltage", unit: "V", scale: 1e-6 },
    { key: "pmin_uw", label: "Minimum power", unit: "W", scale: 1e-6 },
    { key: "pmax_uw", label: "Maximum power", unit: "W", scale: 1e-6 },
    { key: "q", label: "Calibration" },
]);

/**
 * A profile as `/api/schema` declares it.
 *
 * @typedef {object} Profile
 * @property {string} name As the profile setting spells it (`STATe`).
 * @property {number} period_us Its sample period.
 * @property {boolean} ring Whether it fills the ring instead of streaming.
 * @property {string[]} channels What it streams by default (`CURR`, ...).
 * @property {string[]} columns The same as live-event columns.
 */

/**
 * Find an element the view needs.
 *
 * @template {Element} T
 * @param {ParentNode} root Where to look.
 * @param {string} selector What to look for.
 * @param {new () => T} kind What it must be.
 * @returns {T} The element.
 */
function need(root, selector, kind) {
    const found = root.querySelector(selector);
    if (!(found instanceof kind)) {
        throw new Error(`the page has no ${selector}`);
    }
    return found;
}

/**
 * One statistic written for a person.
 *
 * @param {string | undefined} text The report's value.
 * @param {{unit?: string, scale?: number}} spec How to write it.
 * @returns {string} The value.
 */
function statistic(text, spec) {
    if (text === undefined || text === "-" || text === "") {
        return "-";
    }
    if (spec.unit === undefined || spec.scale === undefined) {
        return text;
    }
    const value = Number(text);
    return Number.isFinite(value) ? formatQuantity(value * spec.scale, spec.unit, 5) : text;
}

export class LiveView {
    /**
     * @param {HTMLElement} root The view's section.
     * @param {Record<string, any>} schema `/api/schema`.
     * @param {import("./status.js").StatusBar} status The status bar, told what the page missed.
     */
    constructor(root, schema, status) {
        this.root = root;
        this.status = status;
        /** @type {Profile[]} */
        this.profiles = schema.profiles ?? [];
        /** @type {Array<Record<string, any>>} The routes, with what each takes. */
        this.routes = schema.routes ?? [];
        this.ring = new SampleRing(
            RING_CAPACITY,
            CHANNELS.map((c) => c.key),
            RING_BUCKETS,
        );
        this.marks = new MarkList(MARK_CAPACITY);
        this.view = new TimeView(WINDOWS[1].us);
        this.form = need(root, "#capture-form", HTMLFormElement);
        this.profile = need(root, "#capture-profile", HTMLSelectElement);
        this.channelBox = need(root, "#capture-channels", HTMLElement);
        this.seconds = need(root, "#capture-seconds", HTMLInputElement);
        this.label = need(root, "#capture-label", HTMLInputElement);
        this.startButton = need(root, "#capture-start", HTMLButtonElement);
        this.stopButton = need(root, "#capture-stop", HTMLButtonElement);
        this.markButton = need(root, "#capture-mark", HTMLButtonElement);
        this.markLabel = need(root, "#mark-label", HTMLInputElement);
        this.followButton = need(root, "#chart-follow", HTMLButtonElement);
        this.windows = need(root, "#chart-windows", HTMLElement);
        this.smoothing = need(root, "#chart-smoothing", HTMLElement);
        /** @type {"live" | "history"} Which chart the canvas shows: the live samples or a run's buckets. */
        this.source = "live";
        /** @type {import("./chart-math.js").Noise | null} The averaging of the live samples, null when unknown. */
        this.liveNoise = null;
        this.refusal = need(root, "#capture-refusal", HTMLElement);
        this.note = need(root, "#capture-note", HTMLElement);
        this.statsSection = need(root, "#statistics", HTMLElement);
        this.statsList = need(root, "#statistics-list", HTMLElement);
        this.statsTitle = need(root, "#statistics-run", HTMLElement);
        this.chart = new LiveChart({
            canvas: need(root, "#chart", HTMLCanvasElement),
            readout: need(root, "#chart-readout", HTMLElement),
            ring: this.ring,
            marks: this.marks,
            view: this.view,
        });
        /** @type {number | null} The `n0` the next batch should carry. */
        this.expected = null;
        /** @type {Set<string>} The columns the batches carried since the capture began. */
        this.seen = new Set();
        /** @type {string} The recorder's state: `idle`, `starting`, `running`, `stopping`. */
        this.state = "idle";
        /** @type {string | null} The run whose statistics are shown. */
        this.shownRun = null;
        /** @type {Record<string, any> | null} That run's `meta.json`. */
        this.shownMeta = null;
        this.exportPng = need(root, "#chart-export-png", HTMLButtonElement);
        this.exportCsv = need(root, "#chart-export-csv", HTMLButtonElement);
        this.build();
        this.wire();
        this.pickProfile();
        this.useSource("live");
    }

    /**
     * Show the smoothing and the averaging of the chart the canvas now holds.
     *
     * @param {"live" | "history"} source The live samples, or a run's buckets.
     */
    useSource(source) {
        this.source = source;
        const us = rememberedSmoothing(source);
        for (const radio of this.smoothing.querySelectorAll("input")) {
            radio.checked = Number(radio.value) === us;
        }
        this.chart.smoothingUs = us;
        this.chart.setNoise(source === "live" ? this.liveNoise : (this.shownMeta?.noise_reduction ?? null));
    }

    /**
     * The INA228's averaging of the samples the live events bring: the
     * capture's, or null when the page cannot know it (a window serve did not
     * open).
     *
     * @param {import("./chart-math.js").Noise | null | undefined} noise The averaging.
     */
    setLiveNoise(noise) {
        this.liveNoise = noise ?? null;
        if (this.source === "live") {
            this.chart.setNoise(this.liveNoise);
        }
    }

    /** Fill the controls the schema declares. */
    build() {
        this.bound(this.label, "/api/capture/start", "label");
        this.bound(this.markLabel, "/api/capture/mark", "label");
        for (const profile of this.profiles) {
            const option = document.createElement("option");
            option.value = profile.name;
            const sink = profile.ring ? "ring" : "stream";
            const period = formatQuantity(profile.period_us * 1e-6, "s", 4);
            option.textContent = `${profile.name} · ${period} · ${sink}`;
            option.dataset.ring = String(profile.ring);
            this.profile.append(option);
        }
        const stream = this.profiles.find((p) => !p.ring);
        if (stream !== undefined) {
            this.profile.value = stream.name;
        }
        for (const channel of CHANNELS) {
            const label = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.name = "channel";
            box.value = channel.name;
            label.append(box, ` ${channel.label}`);
            this.channelBox.append(label);
        }
        WINDOWS.forEach((preset, index) => {
            const label = document.createElement("label");
            const radio = document.createElement("input");
            radio.type = "radio";
            radio.name = "window";
            radio.value = String(preset.us);
            radio.checked = index === 1;
            label.append(radio, preset.label);
            this.windows.append(label);
        });
        for (const choice of SMOOTHING) {
            const label = document.createElement("label");
            const radio = document.createElement("input");
            radio.type = "radio";
            radio.name = "smoothing";
            radio.value = String(choice.us);
            label.append(radio, choice.label);
            this.smoothing.append(label);
        }
    }

    /** Listen to the controls. */
    wire() {
        this.profile.addEventListener("change", () => this.pickProfile());
        this.channelBox.addEventListener("change", () => {
            if (this.state === "idle" && this.ring.size === 0) {
                this.chart.setChannels(this.chosenColumns());
            }
        });
        this.form.addEventListener("submit", (event) => {
            event.preventDefault();
            void this.start();
        });
        this.stopButton.addEventListener("click", () => void this.stop());
        this.markButton.addEventListener("click", () => void this.mark());
        this.followButton.addEventListener("click", () => this.chart.followAgain());
        this.windows.addEventListener("change", (event) => {
            const radio = /** @type {HTMLInputElement} */ (event.target);
            this.view.setWindow(Number(radio.value), this.ring.last);
            this.chart.invalidate();
        });
        this.exportPng.addEventListener("click", () => this.exportView("png"));
        this.exportCsv.addEventListener("click", () => this.exportView("csv"));
        this.smoothing.addEventListener("change", (event) => {
            const us = Number(/** @type {HTMLInputElement} */ (event.target).value);
            rememberSmoothing(this.source, us);
            this.chart.setSmoothing(us);
        });
    }

    /**
     * What the chart shows now: the live samples, or the history of the run
     * shown, with the instrument that recorded it and the averaging it was
     * taken with.
     *
     * @returns {import("./chart-math.js").ViewSubject} The subject of an export.
     */
    subject() {
        const known = (/** @type {unknown} */ serial) =>
            typeof serial === "string" && serial !== "" && serial !== "-" ? serial : null;
        const live = known(this.status.items.get("serial")?.textContent);
        const meta = this.source === "history" ? this.shownMeta : null;
        const noise = this.chart.noise;
        if (meta !== null) {
            const recorded = known(meta.instrument?.info?.serial);
            return { source: `run ${meta.id}`, instrument: recorded ?? live ?? "unknown", noise };
        }
        return { source: "live", instrument: live ?? "unknown", noise };
    }

    /**
     * Save the view as the chart shows it.
     *
     * @param {"png" | "csv"} kind Which file.
     */
    exportView(kind) {
        const subject = this.subject();
        const files = this.chart.exportView(subject);
        const url = kind === "png" ? files.png : `data:text/csv;charset=utf-8,${encodeURIComponent(files.csv)}`;
        save(viewFileName(subject.source, kind), url);
    }

    /** @returns {Profile | undefined} The profile picked. */
    picked() {
        return this.profiles.find((p) => p.name === this.profile.value);
    }

    /** Set the channels to the picked profile's defaults; a ring profile takes none. */
    pickProfile() {
        const profile = this.picked();
        const ring = profile?.ring ?? false;
        for (const box of this.channelBoxes()) {
            box.checked = !ring && (profile?.channels ?? []).includes(box.value);
        }
        this.lockControls();
        this.note.textContent = ring
            ? "A ring profile streams nothing while it runs: the chart shows its trace once it ends."
            : "";
        if (this.state === "idle" && this.ring.size === 0) {
            this.chart.setChannels(this.chosenColumns());
        }
    }

    /** Enable the controls the recorder's state allows; a ring profile takes no channel. */
    lockControls() {
        const idle = this.state === "idle";
        const ring = this.picked()?.ring ?? false;
        for (const box of this.channelBoxes()) {
            box.disabled = ring || !idle;
        }
        this.profile.disabled = !idle;
        this.startButton.disabled = !idle;
        this.stopButton.disabled = this.state !== "running";
        this.markButton.disabled = this.state !== "running";
    }

    /** @returns {HTMLInputElement[]} The channel checkboxes. */
    channelBoxes() {
        return [...this.channelBox.querySelectorAll("input")];
    }

    /** @returns {string[]} The columns of the channels ticked. */
    chosenColumns() {
        return this.channelBoxes()
            .filter((box) => box.checked)
            .map((box) => channelOf(box.value)?.key ?? "")
            .filter((key) => key !== "");
    }

    /**
     * Show what serve or the instrument refused, beside the controls.
     *
     * @param {import("./api.js").Answer | null} answer A failed answer, null to clear it.
     */
    refuse(answer) {
        this.refusal.textContent = answer === null ? "" : refusalText(answer);
    }

    /** Start a capture with the controls' choices. */
    async start() {
        const profile = this.picked();
        if (profile === undefined) {
            return;
        }
        /** @type {Record<string, unknown>} */
        const order = { profile: profile.name };
        if (!profile.ring) {
            const names = this.channelBoxes()
                .filter((box) => box.checked)
                .map((box) => box.value);
            order.channels = names.length > 0 ? names : ["DEFault"];
        }
        const seconds = this.seconds.valueAsNumber;
        if (Number.isFinite(seconds)) {
            order.seconds = seconds;
        }
        if (this.label.value.trim() !== "") {
            order.label = this.label.value.trim();
        }
        this.refuse(null);
        this.startButton.disabled = true;
        const answer = await post("/api/capture/start", order);
        if (!answer.ok) {
            this.refuse(answer);
            this.startButton.disabled = this.state !== "idle";
            return;
        }
        this.capture(answer.body);
    }

    /** Stop the running capture; its statistics are shown once its run is finished. */
    async stop() {
        this.refuse(null);
        this.stopButton.disabled = true;
        const answer = await post("/api/capture/stop");
        if (!answer.ok) {
            this.refuse(answer);
            this.stopButton.disabled = this.state !== "running";
            return;
        }
        await this.showRun(answer.body.meta);
    }

    /**
     * Hold a text field to what its route takes, as `/api/schema` declares it:
     * its most characters and, where the route bounds them, the characters it
     * may hold, so that the page never sends what serve refuses.
     *
     * @param {HTMLInputElement} input The field.
     * @param {string} path The route's path.
     * @param {string} name The argument the field fills.
     */
    bound(input, path, name) {
        const route = this.routes.find((r) => r.path === path);
        const argument = (route?.takes ?? []).find((/** @type {any} */ a) => a.name === name);
        if (typeof argument?.max_length === "number") {
            input.maxLength = argument.max_length;
        }
        if (typeof argument?.pattern === "string") {
            input.pattern = argument.pattern;
        }
    }

    /** Mark the running capture. */
    async mark() {
        this.refuse(null);
        if (!this.markLabel.reportValidity()) {
            return;
        }
        const text = this.markLabel.value.trim();
        const answer = await post("/api/capture/mark", text === "" ? {} : { label: text });
        if (!answer.ok) {
            this.refuse(answer);
        }
    }

    /**
     * The recorder's state, from a start, a `capture` event or `/api/status`.
     *
     * @param {Record<string, any> | null | undefined} state The recorder's `state_json`.
     */
    capture(state) {
        if (!state) {
            return;
        }
        const before = this.state;
        this.state = String(state.state ?? "idle");
        const idle = this.state === "idle";
        this.lockControls();
        if (before === "idle" && !idle) {
            this.beginCapture(state);
        }
        if (!idle) {
            this.setLiveNoise(state.noise_reduction);
        }
        const last = state.last;
        if (idle && last && last.id !== this.shownRun && before !== "idle") {
            void get(`/api/runs/${encodeURIComponent(last.id)}`).then((answer) => {
                if (answer.ok) {
                    void this.showRun(answer.body);
                }
            });
        }
    }

    /**
     * A capture began: the statistics of the last one go, the lanes are its
     * channels. The ring itself starts again at the capture's first batch
     * (`n0` 0), which may arrive before this.
     *
     * @param {Record<string, any>} state The recorder's state.
     */
    beginCapture(state) {
        this.statsSection.hidden = true;
        if (this.source !== "live") {
            this.useSource("live");
        }
        const profile = this.profiles.find(
            (p) => p.name.toUpperCase().startsWith(String(state.profile ?? "").slice(0, 4)),
        );
        this.chart.emptyText = profile?.ring
            ? "Filling the ring: the trace is read at the end."
            : "Waiting for the first samples.";
        this.chart.setChannels(profile?.ring ? [] : this.chosenColumns());
        this.view.setWindow(this.view.windowUs, Number.NaN);
    }

    /** A new capture's first rows: the chart starts again from them. */
    restart() {
        this.ring.clear();
        this.marks.clear();
        this.seen.clear();
        this.expected = null;
        this.chart.resetOrigin();
    }

    /**
     * A `samples` event: its rows into the ring, its columns into the lanes.
     *
     * @param {{n0: number, count: number, t_us: number[], [column: string]: unknown}} batch The event.
     */
    samples(batch) {
        if (batch.n0 === 0 && this.expected !== null && this.expected > 0) {
            this.restart();
        }
        if (this.source !== "live") {
            this.useSource("live");
        }
        const missed = rowsMissed(this.expected, batch.n0);
        if (missed > 0) {
            this.status.addLost(missed);
        }
        this.expected = batch.n0 + batch.count;
        this.ring.pushBatch(batch);
        let grew = false;
        for (const channel of CHANNELS) {
            if (Array.isArray(batch[channel.key]) && !this.seen.has(channel.key)) {
                this.seen.add(channel.key);
                grew = true;
            }
        }
        if (grew) {
            this.chart.setChannels(CHANNELS.map((c) => c.key).filter((key) => this.seen.has(key)));
        }
        // Held on an earlier stretch, the chart is not drawn again for samples
        // that land past it: at the full rate every batch would redraw it.
        if (grew || batchChangesView(this.view, batch.t_us[0], this.ring.first, this.chart.window.us / 2)) {
            this.chart.invalidate();
        }
    }

    /**
     * A `mark` event.
     *
     * @param {{label: string, n: number, t_us: number}} mark The event.
     */
    addMark(mark) {
        this.marks.add(mark.t_us, mark.label || `mark ${mark.n}`);
        this.chart.invalidate();
    }

    /**
     * A `ring` event: a ring capture's fill, or a dump's progress.
     *
     * @param {Record<string, any>} ring The event.
     */
    ringFill(ring) {
        if (ring.samples !== undefined && ring.running) {
            this.note.textContent = `The ring holds ${ring.samples} samples.`;
        } else if (ring.dump) {
            this.note.textContent = `Reading the ring: ${ring.dump.received} of ${ring.dump.total} bytes.`;
        }
    }

    /**
     * Show a finished run's statistics and, for a ring capture, its trace.
     *
     * @param {Record<string, any>} meta The run's `meta.json`.
     */
    async showRun(meta) {
        this.shownRun = meta.id;
        this.shownMeta = meta;
        const fields = meta.statistics?.fields ?? {};
        this.statsList.replaceChildren();
        for (const spec of STATISTICS) {
            const term = document.createElement("dt");
            term.textContent = spec.label;
            const value = document.createElement("dd");
            value.textContent = statistic(fields[spec.key], spec);
            value.dataset.statistic = spec.key;
            const pair = document.createElement("div");
            pair.append(term, value);
            this.statsList.append(pair);
        }
        const ended = meta.ended_by ? `, ended by ${meta.ended_by}` : "";
        const averaged = noiseCaption(meta.noise_reduction);
        this.statsTitle.textContent = `${meta.id} (${meta.state ?? "?"}${ended})${averaged === "" ? "" : ` · ${averaged}`}`;
        this.statsSection.hidden = false;
        if (meta.trace) {
            await this.showTrace(meta.id);
        }
    }

    /**
     * A ring capture's trace, from the server's mean, minimum and maximum per bucket.
     *
     * @param {string} run The run.
     */
    async showTrace(run) {
        const count = await this.showHistory(run, ["i_na"]);
        if (count > 0) {
            this.note.textContent = `The ring's trace, as ${count} buckets of mean, minimum and maximum.`;
        }
    }

    /**
     * Open a recorded run in the chart: its statistics, and every channel it
     * holds from the server's count, mean, minimum and maximum per bucket, so
     * a run of any length costs the page the same.
     *
     * @param {Record<string, any>} meta The run's `meta.json`.
     * @returns {Promise<string | null>} Why it was not opened, or null.
     */
    async reopen(meta) {
        if (this.state !== "idle") {
            return "a capture is running: stop it before opening a run";
        }
        await this.showRun(meta);
        if (!meta.trace) {
            const count = await this.showHistory(
                meta.id,
                CHANNELS.map((c) => c.key),
            );
            if (count > 0) {
                this.note.textContent = `Run ${meta.id}, as ${count} buckets of mean, minimum and maximum per channel.`;
            }
        }
        return null;
    }

    /**
     * A run's columns from `/api/runs/<run>/decimated`, merged into the ring as weighted buckets.
     *
     * @param {string} run The run.
     * @param {string[]} columns The columns to ask for; those the run lacks come back empty.
     * @returns {Promise<number>} The buckets shown, 0 when none.
     */
    async showHistory(run, columns) {
        const base = `/api/runs/${encodeURIComponent(run)}/decimated?buckets=${RING_BUCKETS}&column=`;
        const answers = await Promise.all(columns.map((column) => get(base + column)));
        const failed = answers.find((answer) => !answer.ok);
        if (failed !== undefined) {
            this.refuse(failed);
            return 0;
        }
        /** @type {Parameters<typeof mergeBuckets>[0]} */
        const held = answers
            .map((answer) => /** @type {Parameters<typeof mergeBuckets>[0][number]} */ (/** @type {unknown} */ (answer.body)))
            .filter((body) => body.t_us.length > 0);
        if (held.length === 0) {
            this.note.textContent = "The run holds no sample.";
            return 0;
        }
        const merged = mergeBuckets(held);
        this.restart();
        this.useSource("history");
        for (let i = 0; i < merged.t.length; i += 1) {
            this.ring.pushBucket(merged.t[i], merged.end[i], (key) => bucketOf(merged, key, i));
        }
        this.chart.setChannels(held.map((body) => body.column));
        this.view.follow = false;
        this.view.t0 = this.ring.first;
        this.view.t1 = merged.end[merged.end.length - 1]; // the last bucket's end, so it shows whole
        this.chart.invalidate();
        return Math.max(...held.map((body) => body.t_us.length));
    }
}
