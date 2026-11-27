// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The page's two modal dialogs, both native `<dialog>` elements
 * opened with `showModal()`: the confirmation that names what an order
 * moves before it goes out, and a running sequence's question.
 *
 * The browser gives a modal its focus handling, its inert page and its
 * `Esc`; the safe answer has `autofocus`, and a dialog dismissed without a
 * choice (`returnValue` empty) is the safe answer too.
 */

import { need } from "./dom.js";

/**
 * Ask a person to confirm.
 *
 * @param {{title: string, lines: readonly string[], go: string}} question
 *     The title, each line of what will happen, and the label of the button that goes ahead.
 * @returns {Promise<boolean>} Whether they went ahead.
 */
export function confirmAction(question) {
    const dialog = need(document, "#confirm-dialog", HTMLDialogElement);
    need(dialog, "#confirm-title", HTMLElement).textContent = question.title;
    const list = need(dialog, "#confirm-lines", HTMLElement);
    list.replaceChildren(
        ...question.lines.map((line) => {
            const item = document.createElement("li");
            item.textContent = line;
            return item;
        }),
    );
    need(dialog, "#confirm-go", HTMLButtonElement).textContent = question.go;
    dialog.returnValue = "";
    return new Promise((resolve) => {
        dialog.addEventListener(
            "close",
            () => {
                const went = dialog.returnValue === "confirm";
                dialog.returnValue = "";
                resolve(went);
            },
            { once: true },
        );
        dialog.showModal();
    });
}

/** The answer a sequence's question takes when it is dismissed without one. */
export const SAFE_ANSWER = "cancel";

/**
 * How the question on show ends from the page: called by `dismissPrompt` or by
 * the next `askPrompt`, it detaches that question's `close` listener and
 * settles its promise with null, at once.
 *
 * `dialog.close()` only queues its `close` event, so a question dismissed and
 * another shown in the same moment would otherwise see that event land on the
 * new question's listener, read as a choice-less close: the safe answer, sent
 * for a question nobody saw.
 *
 * @type {(() => void) | null}
 */
let settleShown = null;

/**
 * Ask a sequence's question.
 *
 * @param {{id: number, text: string, choices: string[]}} prompt The question, as the sequence state names it.
 * @returns {Promise<string | null>} The choice, the safe one if offered when
 *     dismissed, or null when the dialog was closed from the page (`dismissPrompt`).
 */
export function askPrompt(prompt) {
    const dialog = need(document, "#prompt-dialog", HTMLDialogElement);
    need(dialog, "#prompt-text", HTMLElement).textContent = prompt.text;
    const form = need(dialog, "#prompt-choices", HTMLFormElement);
    form.replaceChildren(
        ...prompt.choices.map((choice) => {
            const button = document.createElement("button");
            button.type = "submit";
            button.value = choice;
            button.className = choice === SAFE_ANSWER ? "btn" : "btn btn-primary";
            button.textContent = choice;
            button.autofocus = choice === SAFE_ANSWER;
            return button;
        }),
    );
    settleShown?.();
    dialog.dataset.prompt = String(prompt.id);
    dialog.returnValue = "";
    return new Promise((resolve) => {
        /** @param {string | null} answer */
        const settle = (answer) => {
            dialog.removeEventListener("close", onClose);
            settleShown = null;
            resolve(answer);
        };
        const onClose = () => {
            if (dialog.open) {
                return; // an earlier close, queued before this question was shown
            }
            const chosen = dialog.returnValue;
            dialog.returnValue = "";
            delete dialog.dataset.prompt;
            if (chosen !== "") {
                settle(chosen);
            } else {
                settle(prompt.choices.includes(SAFE_ANSWER) ? SAFE_ANSWER : null);
            }
        };
        settleShown = () => settle(null);
        dialog.addEventListener("close", onClose);
        dialog.showModal();
    });
}

/**
 * Close the question's dialog without an answer: the sequence no longer asks it.
 *
 * @param {number | null} id The question still open, or null for any.
 */
export function dismissPrompt(id) {
    const dialog = need(document, "#prompt-dialog", HTMLDialogElement);
    if (dialog.open && (id === null || dialog.dataset.prompt === String(id))) {
        delete dialog.dataset.prompt;
        dialog.close();
        settleShown?.();
    }
}

/**
 * The question the dialog is showing, by id, or null when it is closed.
 *
 * @returns {number | null} The id.
 */
export function promptShown() {
    const dialog = need(document, "#prompt-dialog", HTMLDialogElement);
    return dialog.open ? Number(dialog.dataset.prompt) : null;
}
