// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Sequences view: one button per measurement sequence the schema
 * declares, its parameters within their bounds, and the run's progress, its
 * question in a `<dialog>`, its abort, its steps, its cleanup and its results.
 *
 * Each sequence is an `<article data-sequence="NAME">` of the page, its
 * parameters `<input data-param="name">` in it, so a test holds the page to
 * `/api/schema`; this module fills in their titles, bounds and defaults. The
 * state comes from `POST /api/sequences/run`, `GET /api/sequences` and
 * every `sequence` event of the live events, the same object each time.
 */

import { get, post } from "./api.js";
import { askPrompt, dismissPrompt, promptShown } from "./dialogs.js";
import { fillPairs, need, shown, showRefusal } from "./dom.js";

/**
 * A sequence as `/api/schema` declares it.
 *
 * @typedef {object} SequenceInfo
 * @property {string} name Its key.
 * @property {string} title Its button's label.
 * @property {string} summary What it does.
 * @property {{name: string, label: string, unit: string, default: number, low: number, high: number}[]} params
 *     What a person may set.
 */

/** How a sequence may end, and the status color each takes. */
const OUTCOMES = Object.freeze({
    done: "done",
    failed: "pending",
    aborted: "pending",
    declined: "pending",
});

/**
 * How far a sequence has gone, as a fraction of its steps.
 *
 * @param {Record<string, any>} state The sequence state.
 * @returns {number} 0 to 1.
 */
export function progressOf(state) {
    const total = Number(state.total ?? 0);
    const steps = Array.isArray(state.steps) ? state.steps : [];
    if (state.state === "idle" && state.outcome === "done") {
        return 1;
    }
    if (total <= 0) {
        return 0;
    }
    const finished = steps.filter((entry) => entry.state !== "running").length;
    return Math.min(1, finished / total);
}

export class SequencesView {
    /**
     * @param {HTMLElement} root The view's section.
     * @param {Record<string, any>} schema `/api/schema`.
     * @param {import("./session.js").Session} session The session's state.
     */
    constructor(root, schema, session) {
        this.root = root;
        this.session = session;
        /** @type {SequenceInfo[]} */
        this.sequences = schema.sequences ?? [];
        /** @type {Map<string, HTMLElement>} */
        this.cards = new Map();
        this.stateText = need(root, "#sequence-state", HTMLElement);
        this.bar = need(root, "#sequence-bar", HTMLProgressElement);
        this.abortButton = need(root, "#sequence-abort", HTMLButtonElement);
        this.refusal = need(root, "#sequence-refusal", HTMLElement);
        this.steps = need(root, "#sequence-steps", HTMLElement);
        this.cleanup = need(root, "#sequence-cleanup", HTMLElement);
        this.results = need(root, "#sequence-results", HTMLElement);
        /** @type {Record<string, any>} The last state shown. */
        this.state = { state: "idle", sequence: null };
        /** @type {string[]} Sequences the schema names that the page has no button for. */
        this.missing = [];
        this.build();
        this.abortButton.addEventListener("click", () => void this.abort());
        session.addEventListener("change", () => this.lockButtons());
    }

    /** Fill each sequence's card from the schema and wire its button. */
    build() {
        for (const sequence of this.sequences) {
            const card = this.root.querySelector(`[data-sequence="${sequence.name}"]`);
            if (!(card instanceof HTMLElement)) {
                this.missing.push(sequence.name);
                continue;
            }
            this.cards.set(sequence.name, card);
            const title = card.querySelector("h3");
            if (title !== null) {
                title.textContent = sequence.title;
            }
            for (const param of sequence.params) {
                const input = card.querySelector(`[data-param="${param.name}"]`);
                if (input instanceof HTMLInputElement) {
                    input.min = String(param.low);
                    input.max = String(param.high);
                    input.value = String(param.default);
                }
            }
            const button = need(card, '[data-action="run"]', HTMLButtonElement);
            button.addEventListener("click", () => void this.run(sequence, card));
        }
    }

    /** Enable the Run buttons only while no sequence runs and nobody else holds the line. */
    lockButtons() {
        const known = this.session.known;
        const busy = this.state.state !== "idle" || known.held !== null || known.capture !== "idle";
        for (const card of this.cards.values()) {
            need(card, '[data-action="run"]', HTMLButtonElement).disabled = busy;
        }
        this.abortButton.disabled = this.state.state === "idle";
    }

    /**
     * Start one sequence with the parameters its card holds.
     *
     * @param {SequenceInfo} sequence The sequence.
     * @param {HTMLElement} card Its card.
     */
    async run(sequence, card) {
        const where = need(card, "[data-refusal]", HTMLElement);
        showRefusal(where, null);
        /** @type {Record<string, number>} */
        const params = {};
        for (const param of sequence.params) {
            const input = need(card, `[data-param="${param.name}"]`, HTMLInputElement);
            const value = input.valueAsNumber;
            if (!Number.isInteger(value) || value < param.low || value > param.high) {
                showRefusal(where, `${param.label} is a whole number of ${param.unit}, ${param.low} to ${param.high}`);
                input.focus();
                return;
            }
            params[param.name] = value;
        }
        const answer = await post("/api/sequences/run", { name: sequence.name, params });
        if (!answer.ok) {
            showRefusal(where, answer);
            return;
        }
        this.update(answer.body);
    }

    /** Abort the sequence that runs; its cleanup runs. */
    async abort() {
        showRefusal(this.refusal, null);
        const answer = await post("/api/sequences/abort");
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return;
        }
        this.update(answer.body);
    }

    /** Read the state again (when the view is shown, or the events came back). */
    async refresh() {
        const answer = await get("/api/sequences");
        if (answer.ok) {
            this.update(answer.body);
        }
    }

    /**
     * A new state: the progress, the log, the results, and the question if one waits.
     *
     * @param {Record<string, any>} state `GET /api/sequences`, or a `sequence` event.
     */
    update(state) {
        this.state = state;
        this.session.update({ sequence: state.state === "idle" ? null : state.sequence });
        this.lockButtons();
        if (state.sequence === null || state.sequence === undefined) {
            return;
        }
        for (const [name, card] of this.cards) {
            card.dataset.state = name === state.sequence && state.state !== "idle" ? "running" : "";
        }
        this.bar.value = progressOf(state);
        const total = Number(state.total ?? 0);
        const begun = Array.isArray(state.steps) ? state.steps.length : 0;
        const elapsed = typeof state.elapsed_s === "number" ? `${state.elapsed_s.toFixed(1)} s` : "";
        if (state.state === "idle") {
            const outcome = String(state.outcome ?? "ended");
            const detail = state.detail ? `: ${state.detail}` : "";
            this.stateText.textContent = `${state.sequence} ${outcome}${detail} (${elapsed})`;
            this.stateText.dataset.state = OUTCOMES[/** @type {keyof typeof OUTCOMES} */ (outcome)] ?? "pending";
        } else {
            const where = state.state === "cleanup" ? "cleaning up" : `step ${begun} of ${total}`;
            this.stateText.textContent = `${state.sequence} ${state.state}: ${where} (${elapsed})`;
            delete this.stateText.dataset.state;
        }
        this.fillLog(this.steps, state.steps);
        this.fillLog(this.cleanup, state.cleanup);
        const results = Object.entries(state.results ?? {}).map(
            ([key, value]) => /** @type {[string, unknown]} */ ([key, summarize(value)]),
        );
        if (state.run && !("run" in (state.results ?? {}))) {
            results.push(["run", state.run]);
        }
        fillPairs(this.results, results);
        this.handlePrompt(state);
    }

    /**
     * Write a log of steps.
     *
     * @param {HTMLElement} list The `<ol>`.
     * @param {unknown} entries The state's `steps` or `cleanup`.
     */
    fillLog(list, entries) {
        list.replaceChildren();
        for (const entry of Array.isArray(entries) ? entries : []) {
            const item = document.createElement("li");
            const errors = Array.isArray(entry.errors) && entry.errors.length > 0
                ? ` (${entry.errors.map((/** @type {any} */ e) => `${e.code} ${e.text}`).join("; ")})`
                : "";
            const detail = entry.detail ? `: ${String(entry.detail).replace(PART_FIELD, "")}` : "";
            item.textContent = `${entry.step} · ${entry.state}${detail}${errors}`;
            if (entry.state === "ok") {
                item.dataset.state = "done";
            } else if (entry.state !== "running") {
                item.dataset.state = "pending";
            }
            list.append(item);
        }
    }

    /**
     * Open the question's dialog when one waits, close it when it no longer does.
     *
     * @param {Record<string, any>} state The sequence state.
     */
    handlePrompt(state) {
        const prompt = state.state === "prompt" ? state.prompt : null;
        const open = promptShown();
        if (prompt === null || prompt === undefined) {
            if (open !== null) {
                dismissPrompt(null);
            }
            return;
        }
        if (open === prompt.id) {
            return;
        }
        if (open !== null) {
            dismissPrompt(null);
        }
        void askPrompt(prompt).then(async (choice) => {
            if (choice === null) {
                return;
            }
            const answer = await post("/api/sequences/answer", { id: prompt.id, answer: choice });
            if (!answer.ok) {
                showRefusal(this.refusal, answer);
            } else {
                this.update(answer.body);
            }
        });
    }
}

/** The report's field the page leaves out: it names the part fitted as Relay 2, and the page names no part. */
const PART_FIELD = /(?:^| )bypass_part=\S*/gu;

/**
 * A kept answer in a line: a report's pairs, or the answer as it came.
 *
 * @param {unknown} value A result.
 * @returns {string} The line.
 */
function summarize(value) {
    const line = value !== null && typeof value === "object" && !Array.isArray(value)
        ? Object.entries(value)
            .map(([key, item]) => `${key}=${shown(item)}`)
            .join(" ")
        : shown(value);
    return line.replace(PART_FIELD, "");
}
