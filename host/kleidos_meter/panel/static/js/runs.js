// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Runs view: every capture serve recorded (`GET /api/runs`),
 * newest first, each with a button that opens it in the Live chart from the
 * server's decimated history, what the INA228 averaged of it (marked when
 * noise reduction was on, which changes the samples) and its three exports as plain links (a GET,
 * which changes nothing), downloaded as the run's own files.
 *
 * The list holds at most `LIMIT` runs, so the page does not grow with the
 * data directory.
 */

import { get } from "./api.js";
import { noiseText } from "./chart-math.js";
import { need, shown, showRefusal } from "./dom.js";

/** The most runs the list shows. */
export const LIMIT = 100;

/** The exports a run offers, as `/api/runs/<run>/export?format=` names them. */
export const EXPORTS = Object.freeze(["text", "csv", "json"]);

export class RunsView {
    /**
     * @param {HTMLElement} root The view's section.
     * @param {(meta: Record<string, any>) => Promise<string | null>} open
     *     Open a run's `meta.json` in the Live view; why it could not, or null.
     */
    constructor(root, open) {
        this.root = root;
        this.open = open;
        this.body = need(root, "#runs-body", HTMLElement);
        this.caption = need(root, "#runs-caption", HTMLElement);
        this.refusal = need(root, "#runs-refusal", HTMLElement);
        need(root, "#runs-refresh", HTMLButtonElement).addEventListener("click", () => void this.refresh());
        /** Whether the list may be out of date (a capture ended while the view was hidden). */
        this.stale = true;
    }

    /** Read the runs again and write the table. */
    async refresh() {
        showRefusal(this.refusal, null);
        const answer = await get(`/api/runs?limit=${LIMIT}`);
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return;
        }
        this.stale = false;
        const runs = Array.isArray(answer.body.runs) ? answer.body.runs : [];
        this.body.replaceChildren(...runs.map((run) => this.row(run)));
        this.caption.textContent =
            runs.length === 0
                ? "No run recorded yet: a capture started from the panel is recorded."
                : `${runs.length} ${runs.length === 1 ? "run" : "runs"}, newest first${runs.length === LIMIT ? ` (the latest ${LIMIT})` : ""}.`;
    }

    /**
     * One run's row.
     *
     * @param {Record<string, any>} run Its line of `/api/runs`.
     * @returns {HTMLTableRowElement} The row.
     */
    row(run) {
        const row = document.createElement("tr");
        row.dataset.run = String(run.id);
        const head = document.createElement("th");
        head.scope = "row";
        head.textContent = shown(run.id);
        row.append(head);
        const noise = run.noise_reduction ?? null;
        row.dataset.averaging = noise === null ? "unknown" : noise.state;
        const samples = run.samples ?? run.stream_samples;
        const state = run.complete === false ? `${run.state} (incomplete)` : run.state;
        const averaged = noise?.state === "on" ? noiseText(noise) : noise?.state;
        for (const value of [run.label, run.profile, averaged, state, samples, run.dropped, run.marks,
            run.ended_by ?? run.detail]) {
            const cell = document.createElement("td");
            cell.textContent = shown(value);
            if (value === averaged && noise?.state === "on") {
                cell.dataset.state = "pending";
            }
            row.append(cell);
        }
        const actions = document.createElement("td");
        actions.className = "row-actions";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn";
        button.textContent = "Open";
        button.disabled = run.state === "unreadable";
        button.addEventListener("click", () => void this.reopen(String(run.id), button));
        actions.append(button);
        // A run still recording has no export yet: serve refuses it (409).
        for (const kind of run.recording ? [] : EXPORTS) {
            const link = document.createElement("a");
            link.href = `/api/runs/${encodeURIComponent(String(run.id))}/export?format=${kind}`;
            link.download = "";
            link.textContent = kind.toUpperCase();
            link.className = "export";
            actions.append(link);
        }
        row.append(actions);
        return row;
    }

    /**
     * Open one run in the Live chart.
     *
     * @param {string} id The run.
     * @param {HTMLButtonElement} button The button that asked, disabled meanwhile.
     */
    async reopen(id, button) {
        showRefusal(this.refusal, null);
        button.disabled = true;
        try {
            const answer = await get(`/api/runs/${encodeURIComponent(id)}`);
            if (!answer.ok) {
                showRefusal(this.refusal, answer);
                return;
            }
            const why = await this.open(answer.body);
            if (why !== null) {
                showRefusal(this.refusal, why);
            }
        } finally {
            button.disabled = false;
        }
    }
}
