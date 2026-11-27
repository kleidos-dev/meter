// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file What every view needs to know of the session, kept in one place:
 * the last status poll, the instrument's identity, who holds the line, the
 * sequence that runs and the recorder's state. Views listen for `change`
 * and decide from it what their controls may do now.
 *
 * `refusedBy` is the rule the Settings view disables a control by: the
 * flag letters of `docs/scpi.md` (`R`, `C`, `D`), read against the last
 * poll, before the instrument has to refuse anything.
 */

/**
 * What the session knows.
 *
 * @typedef {object} Known
 * @property {Record<string, any> | null} status The last `SYSTem:STATus?` poll, as JSON.
 * @property {Record<string, any> | null} instrument `/api/status`'s `instrument`.
 * @property {string | null} held Who holds the line besides the panel.
 * @property {string | null} sequence The sequence that runs, by name.
 * @property {string} capture The recorder's state: `idle`, `starting`, `running`, `stopping`.
 */

/**
 * Why a command of these flags would be refused now, or null when it would not.
 *
 * The order is the one a person can act on: the line held by another
 * controller first, a sequence that runs, then the instrument's own flags.
 *
 * @param {string} flags The command's flag letters (`DRC`).
 * @param {Known} known What the session knows.
 * @returns {string | null} The reason, naming its flag, or null.
 */
export function refusedBy(flags, known) {
    if (known.held !== null) {
        return `the line is held by ${known.held}: the panel watches`;
    }
    if (known.sequence !== null) {
        return `sequence ${known.sequence} runs: wait for it or abort it`;
    }
    const chip = known.instrument?.chip;
    if (flags.includes("D") && typeof chip === "string" && chip !== "ina228") {
        return "D: needs the INA228, and the instrument has none up";
    }
    const status = known.status;
    if (status === null) {
        return null;
    }
    const sink = String(status.sink ?? "idle");
    if (flags.includes("R") && status.running === 1) {
        return sink === "null"
            ? "R: refused while the null window runs"
            : "R: refused while a capture runs";
    }
    if (flags.includes("C") && sink !== "idle") {
        return "C: refused until the window that ended by itself is closed (Abort)";
    }
    return null;
}

/** The session's state, and an event each time it changes. */
export class Session extends EventTarget {
    constructor() {
        super();
        /** @type {Known} */
        this.known = { status: null, instrument: null, held: null, sequence: null, capture: "idle" };
    }

    /**
     * Take what changed and tell the views.
     *
     * @param {Partial<Known>} change The keys that changed.
     */
    update(change) {
        let changed = false;
        for (const [key, value] of Object.entries(change)) {
            const name = /** @type {keyof Known} */ (key);
            if (value !== undefined && this.known[name] !== value) {
                /** @type {Record<string, unknown>} */ (this.known)[name] = value;
                changed = true;
            }
        }
        if (changed) {
            this.dispatchEvent(new Event("change"));
        }
    }

    /**
     * Why a command of these flags would be refused now.
     *
     * @param {string} flags The flag letters.
     * @returns {string | null} The reason, or null.
     */
    refusedBy(flags) {
        return refusedBy(flags, this.known);
    }
}
