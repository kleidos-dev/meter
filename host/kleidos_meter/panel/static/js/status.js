// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The status bar: what the instrument is and what the session is
 * doing, always in view. Each item is a cell of the page's header with two
 * lines: its value, a `<span data-status="...">`, and below it a qualifier,
 * a `<span data-qualifier="...">` (an image's `elf=`, a capture's samples,
 * "not quotable", the INA228's averaging while it is on), written here from `/api/status`, the status polls of the
 * live events, the recorder's capture events and `/api/calibration`. Both
 * lines are always there and one line high, so what the bar says never
 * changes the header's height; a value cut by its column is whole in its
 * title.
 *
 * A status carries its word; the `data-state` color (`done`, `pending`)
 * only repeats it.
 */

import { noiseText } from "./chart-math.js";

/**
 * The items, by their `data-status` name.
 *
 * @typedef {"serial" | "image" | "capture" | "loss" | "controller" | "calibration" | "pins" | "link"} Item
 */

export class StatusBar {
    /** @param {ParentNode} root Where the `data-status` items are. */
    constructor(root) {
        /** @type {Map<string, HTMLElement>} */
        this.items = new Map();
        for (const node of root.querySelectorAll("[data-status]")) {
            const element = /** @type {HTMLElement} */ (node);
            this.items.set(element.dataset.status ?? "", element);
        }
        /** @type {Map<string, HTMLElement>} Each item's qualifier line. */
        this.qualifiers = new Map();
        for (const node of root.querySelectorAll("[data-qualifier]")) {
            const element = /** @type {HTMLElement} */ (node);
            this.qualifiers.set(element.dataset.qualifier ?? "", element);
        }
        /** The instrument's own count of samples it could not write, from the polls. */
        this.dropped = 0;
        /** Samples the page missed: `lost` events and gaps between batches. */
        this.lost = 0;
        /** @type {string | null} Who holds the line besides the panel. */
        this.holder = null;
        /** @type {string | null} The sequence running, if any. */
        this.sequence = null;
    }

    /**
     * Write one item's value.
     *
     * @param {Item} name The item.
     * @param {string} text Its text.
     * @param {"done" | "pending" | null} [state] Its status color, none by default.
     */
    set(name, text, state = null) {
        const element = this.items.get(name);
        write(element, text, state);
        if (element !== undefined) {
            element.title = text;
        }
    }

    /**
     * Write one item's qualifier line; an empty one keeps its line.
     *
     * @param {Item} name The item.
     * @param {string} text Its text, "" for none.
     * @param {"done" | "pending" | null} [state] Its status color, none by default.
     */
    qualify(name, text, state = null) {
        write(this.qualifiers.get(name), text, state);
    }

    /**
     * The instrument, from `/api/status`'s `instrument`.
     *
     * @param {Record<string, any> | null | undefined} instrument Its identity.
     */
    setInstrument(instrument) {
        if (!instrument) {
            return;
        }
        this.set("serial", String(instrument.serial ?? "unknown"));
        this.set("image", `${instrument.image ?? "?"} ${instrument.fw ?? "?"}`);
        this.qualify("image", instrument.elf ? `elf ${instrument.elf}` : "");
    }

    /**
     * The capture as a status poll reports it.
     *
     * @param {Record<string, any> | null | undefined} status `SYSTem:STATus?` as JSON.
     */
    setPoll(status) {
        if (!status) {
            return;
        }
        if (typeof status.dropped === "number") {
            this.dropped = status.dropped;
        }
        const running = status.running === 1;
        if (running) {
            this.set("capture", `${status.sink} ${status.profile}`, "done");
            this.qualify("capture", `${status.samples} samples`);
        } else {
            this.set("capture", "idle");
            this.qualify("capture", `profile ${status.profile}`);
        }
        this.showLoss();
    }

    /**
     * Count samples the page missed.
     *
     * @param {number} rows How many more.
     */
    addLost(rows) {
        this.lost += rows;
        this.showLoss();
    }

    /** Write the dropped and lost counts. */
    showLoss() {
        const text = `dropped ${this.dropped} · lost ${this.lost}`;
        this.set("loss", text, this.dropped > 0 || this.lost > 0 ? "pending" : null);
    }

    /**
     * Who controls the instrument: the panel, a passthrough client or a sequence.
     *
     * @param {{holder?: string | null, sequence?: string | null}} change What changed.
     */
    setController(change) {
        if ("holder" in change) {
            this.holder = change.holder ?? null;
        }
        if ("sequence" in change) {
            this.sequence = change.sequence ?? null;
        }
        if (this.holder !== null) {
            this.set("controller", `held by ${this.holder}`, "pending");
            this.qualify("controller", "the panel watches");
        } else if (this.sequence !== null) {
            this.set("controller", `sequence ${this.sequence}`, "pending");
            this.qualify("controller", "running");
        } else {
            this.set("controller", "this panel");
            this.qualify("controller", "");
        }
    }

    /**
     * The registry's verdict on the instrument's calibration.
     *
     * @param {import("./api.js").Answer} answer `/api/calibration`'s answer.
     */
    setCalibration(answer) {
        if (!answer.ok) {
            this.set("calibration", `not known (${answer.status})`, "pending");
            return;
        }
        this.setVerdict(answer.body.verdict ?? {});
    }

    /**
     * The registry's verdict as `/api/calibration` or `/api/settings` (its
     * `shunt.verdict`) last read it, so the bar never keeps the one read at
     * load once Settings has read another.
     *
     * @param {Record<string, any>} verdict The verdict: its `status` and `ok`.
     */
    setVerdict(verdict) {
        this.set("calibration", String(verdict.status ?? "unknown"), verdict.ok ? "done" : "pending");
        this.qualify("calibration", "");
    }

    /**
     * Whether the instrument's figures may be quoted: not while a range
     * converts with an override, nor when the registry's verdict is not
     * `applied`. The calibration item says so on its qualifier line, under
     * the verdict, and the verdict turns `pending`.
     *
     * @param {boolean} quotable Whether they may.
     * @param {string | null} banner The 'not quotable' banner, when an override is the reason.
     */
    setQuotable(quotable, banner) {
        const element = this.items.get("calibration");
        const qualifier = this.qualifiers.get("calibration");
        if (element === undefined || qualifier === undefined) {
            return;
        }
        if (quotable) {
            this.qualify("calibration", "");
        } else {
            element.dataset.state = "pending";
            this.qualify("calibration", "not quotable", "pending");
        }
        qualifier.title = banner ?? "";
    }

    /**
     * The pin map, from `GET /api/pins`'s answer.
     *
     * @param {import("./api.js").Answer} answer The answer.
     */
    setPins(answer) {
        if (!answer.ok) {
            this.set("pins", `not known (${answer.status})`, "pending");
            this.qualify("pins", "");
            return;
        }
        this.setPinMap(answer.body.live ?? {});
    }

    /**
     * The live pin map: where it came from (`default`, `stored`, or
     * `fallback` when the stored bus did not answer at boot) and, on the
     * qualifier line, whether its relays passed a null-path check.
     *
     * @param {Record<string, any>} live `SYSTem:PINS?`, as JSON.
     */
    setPinMap(live) {
        const source = String(live.src ?? "unknown");
        this.set("pins", source, source === "fallback" ? "pending" : "done");
        if (live.verified === 1) {
            this.qualify("pins", "verified");
        } else {
            this.qualify("pins", "unverified", "pending");
        }
    }

    /**
     * The live profile's averaging in the INA228: the Samples item's
     * qualifier says it while it is on, since it changes every sample, and
     * that the page does not know when it could not be read.
     *
     * @param {import("./chart-math.js").Noise | null} noise The averaging, null for an image that has none.
     * @param {boolean} [read] Whether it could be read.
     */
    setNoise(noise, read = true) {
        const on = noise !== null && noise.state === "on";
        const text = on ? noiseText(noise) : (read ? "" : "averaging not known");
        this.qualify("loss", text, text === "" ? null : "pending");
        const qualifier = this.qualifiers.get("loss");
        if (qualifier !== undefined) {
            qualifier.title = on
                ? `Noise reduction in the INA228 is on, on the live profile: ${text}. It changes the measurement.`
                : text;
        }
    }

    /**
     * The line to the instrument and the stream from serve.
     *
     * @param {string} text What to say, a word: `up`, `down`, `reconnecting`.
     * @param {boolean} up Whether all is well.
     * @param {string} [detail] Why, on the qualifier line; none by default.
     */
    setLink(text, up, detail = "") {
        this.set("link", text, up ? "done" : "pending");
        this.qualify("link", detail);
    }
}

/**
 * Write a line of the bar and its status color.
 *
 * @param {HTMLElement | undefined} element The line, if the page has it.
 * @param {string} text Its text.
 * @param {"done" | "pending" | null} state Its status color, or none.
 */
function write(element, text, state) {
    if (element === undefined) {
        return;
    }
    element.textContent = text;
    if (state === null) {
        delete element.dataset.state;
    } else {
        element.dataset.state = state;
    }
}
