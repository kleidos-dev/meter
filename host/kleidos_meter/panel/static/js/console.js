// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The raw SCPI console: program messages, one per line, sent through
 * serve's queue (`POST /api/scpi`), each written to the transcript with its
 * answers and the errors the instrument queued for it, codes first.
 *
 * The transcript keeps its last `TRANSCRIPT` messages, so a console left
 * open does not grow the page.
 */

import { postScpi } from "./api.js";
import { need, replyErrors, showRefusal } from "./dom.js";

/** The most messages the transcript keeps. */
export const TRANSCRIPT = 50;

export class ConsoleView {
    /** @param {HTMLElement} root The view's section. */
    constructor(root) {
        this.form = need(root, "#console-form", HTMLFormElement);
        this.input = need(root, "#console-input", HTMLTextAreaElement);
        this.sendButton = need(root, "#console-send", HTMLButtonElement);
        this.refusal = need(root, "#console-refusal", HTMLElement);
        this.log = need(root, "#console-log", HTMLElement);
        this.form.addEventListener("submit", (event) => {
            event.preventDefault();
            void this.send();
        });
        this.input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                void this.send();
            }
        });
        need(root, "#console-clear", HTMLButtonElement).addEventListener("click", () => this.log.replaceChildren());
    }

    /** Send the messages typed, one per line. */
    async send() {
        const text = this.input.value;
        if (text.trim() === "") {
            showRefusal(this.refusal, "type a program message");
            return;
        }
        showRefusal(this.refusal, null);
        this.sendButton.disabled = true;
        try {
            const answer = await postScpi(text);
            if (!answer.ok) {
                showRefusal(this.refusal, answer);
                return;
            }
            for (const reply of answer.body.replies ?? []) {
                this.write(reply);
            }
            const failed = answer.body.failed;
            if (failed) {
                // The lines before it ran: show them, the failure and what was never sent.
                const unsent = answer.body.unsent ?? [];
                this.write({ message: failed.message, units: [], errors: [], elapsed_s: 0 }, `${failed.error}: ${failed.detail}`);
                showRefusal(
                    this.refusal,
                    `${failed.error} at "${failed.message}"; ${unsent.length} line${unsent.length === 1 ? "" : "s"} not sent`,
                );
            }
        } finally {
            this.sendButton.disabled = false;
        }
    }

    /**
     * One message's reply, newest first, forgetting the oldest beyond `TRANSCRIPT`.
     *
     * @param {{message: string, units: string[], errors: {code: number, text: string}[], elapsed_s: number}} reply
     *     As `POST /api/scpi` gives it.
     * @param {string | null} [failure] Why the message has no answer, for one that failed.
     */
    write(reply, failure = null) {
        const item = document.createElement("li");
        const sent = document.createElement("code");
        sent.className = "sent";
        sent.textContent = `> ${reply.message}`;
        item.append(sent);
        for (const unit of reply.units ?? []) {
            const line = document.createElement("samp");
            line.textContent = unit;
            item.append(line);
        }
        const errors = failure ?? replyErrors(reply);
        const verdict = document.createElement("span");
        verdict.className = "verdict";
        verdict.textContent = errors === null ? `no error · ${reply.elapsed_s} s` : errors;
        verdict.dataset.state = errors === null ? "done" : "pending";
        item.append(verdict);
        this.log.prepend(item);
        while (this.log.children.length > TRANSCRIPT) {
            this.log.lastElementChild?.remove();
        }
    }
}
