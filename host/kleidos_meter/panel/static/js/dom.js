// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The few DOM helpers every view shares: find an element the page
 * must have, write a list of terms and values, and show a refusal beside
 * the control that caused it.
 */

import { refusalText } from "./api.js";

/**
 * Find an element the view needs.
 *
 * @template {Element} T
 * @param {ParentNode} root Where to look.
 * @param {string} selector What to look for.
 * @param {new () => T} kind What it must be.
 * @returns {T} The element.
 */
export function need(root, selector, kind) {
    const found = root.querySelector(selector);
    if (!(found instanceof kind)) {
        throw new Error(`the page has no ${selector}`);
    }
    return found;
}

/**
 * A value as a person reads it: `-` for none, the text otherwise.
 *
 * @param {unknown} value A report's value as JSON.
 * @returns {string} The text.
 */
export function shown(value) {
    if (value === null || value === undefined || value === "") {
        return "-";
    }
    if (Array.isArray(value)) {
        return value.map(shown).join(", ");
    }
    return String(value);
}

/**
 * Replace a `<dl>`'s pairs: one `<div><dt><dd>` per entry.
 *
 * @param {HTMLElement} list The `<dl>`.
 * @param {readonly (readonly [string, unknown, ("done" | "pending" | null)?])[]} entries
 *     Each term, its value and, if it has one, the status its color repeats.
 */
export function fillPairs(list, entries) {
    list.replaceChildren();
    for (const [term, value, state] of entries) {
        const pair = document.createElement("div");
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = shown(value);
        if (state) {
            dd.dataset.state = state;
        }
        pair.append(dt, dd);
        list.append(pair);
    }
}

/**
 * Show what serve or the instrument refused in *where*, the element beside
 * the control that sent it; an empty answer clears it.
 *
 * @param {HTMLElement} where The refusal's element.
 * @param {import("./api.js").Answer | string | null} answer A failed answer, a line, or null.
 */
export function showRefusal(where, answer) {
    if (answer === null) {
        where.textContent = "";
    } else if (typeof answer === "string") {
        where.textContent = answer;
    } else {
        where.textContent = refusalText(answer);
    }
}

/**
 * The errors a reply of `POST /api/scpi` carried, as one line, or null for none.
 *
 * @param {{errors?: {code: number, text: string}[]}} reply One message's reply.
 * @returns {string | null} For example `-221 Settings conflict;window open`.
 */
export function replyErrors(reply) {
    const errors = Array.isArray(reply.errors) ? reply.errors : [];
    if (errors.length === 0) {
        return null;
    }
    return errors.map((entry) => `${entry.code} ${entry.text}`.trim()).join(" · ");
}
