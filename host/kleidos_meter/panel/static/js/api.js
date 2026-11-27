// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The panel's HTTP API from the page: JSON in and out, the header
 * every change carries, and the live events.
 *
 * Every request goes to the page's own origin. A change (a POST) carries the
 * header `/api/schema` names (`X-Kleidos-Meter: 1`), which serve requires of
 * every change; a GET never changes the instrument.
 */

/**
 * What the API answered.
 *
 * @typedef {object} Answer
 * @property {boolean} ok Whether the status was 2xx.
 * @property {number} status The HTTP status, 0 when the request did not reach serve.
 * @property {Record<string, any>} body The JSON body (`{error, detail}` on a failure).
 */

/** The header every change carries, until the schema names it. */
let mutationHeader = { name: "X-Kleidos-Meter", value: "1" };

/**
 * Take the change header the schema names.
 *
 * @param {{name: string, value: string}} header The schema's `mutation_header`.
 */
export function useMutationHeader(header) {
    mutationHeader = header;
}

/**
 * One request, answered as JSON; a failure to reach serve is an answer too.
 *
 * @param {string} method `GET` or `POST`.
 * @param {string} path The path, below the page's origin.
 * @param {unknown} [body] A POST's body, sent as JSON unless *text* is given.
 * @param {string} [text] A POST's body sent as plain text instead (`/api/scpi`'s program messages).
 * @returns {Promise<Answer>} The answer.
 */
export async function request(method, path, body, text) {
    /** @type {Record<string, string>} */
    const headers = { Accept: "application/json" };
    /** @type {RequestInit} */
    const init = { method, headers, cache: "no-store" };
    if (method !== "GET") {
        headers[mutationHeader.name] = mutationHeader.value;
        if (text === undefined) {
            headers["Content-Type"] = "application/json";
            init.body = JSON.stringify(body ?? {});
        } else {
            headers["Content-Type"] = "text/plain; charset=us-ascii";
            init.body = text;
        }
    }
    try {
        const response = await fetch(path, init);
        const text = await response.text();
        let parsed;
        try {
            parsed = text ? JSON.parse(text) : {};
        } catch {
            parsed = { error: "not_json", detail: text.slice(0, 200) };
        }
        return { ok: response.ok, status: response.status, body: parsed };
    } catch (error) {
        return {
            ok: false,
            status: 0,
            body: { error: "unreachable", detail: `serve did not answer: ${String(error)}` },
        };
    }
}

/**
 * A GET.
 *
 * @param {string} path The path.
 * @returns {Promise<Answer>} The answer.
 */
export function get(path) {
    return request("GET", path);
}

/**
 * A POST with a JSON body.
 *
 * @param {string} path The path.
 * @param {unknown} [body] The body.
 * @returns {Promise<Answer>} The answer.
 */
export function post(path, body) {
    return request("POST", path, body);
}

/**
 * Program messages for `POST /api/scpi`, one per line, as plain text.
 *
 * @param {string} messages The messages.
 * @returns {Promise<Answer>} The answer: `replies`, each message's units and errors.
 */
export function postScpi(messages) {
    return request("POST", "/api/scpi", undefined, messages);
}

/**
 * What a refusal says, as one line: its status, the instrument's error codes
 * when it queued any, and serve's detail.
 *
 * @param {Answer} answer A failed answer.
 * @returns {string} For example `409 · -221 Settings conflict · the instrument refused`.
 */
export function refusalText(answer) {
    const body = answer.body ?? {};
    const parts = [answer.status === 0 ? "no answer" : String(answer.status)];
    const errors = Array.isArray(body.errors) ? body.errors : [];
    for (const entry of errors) {
        parts.push(`${entry.code} ${entry.text ?? ""}`.trim());
    }
    if (body.detail) {
        parts.push(String(body.detail));
    } else if (body.error) {
        parts.push(String(body.error));
    }
    return parts.join(" · ");
}

/**
 * The handlers of the live events, by kind.
 *
 * @typedef {Partial<Record<"samples" | "mark" | "status" | "error" | "sequence" | "ring" | "lost", (data: any) => void>>} EventHandlers
 */

/** The first wait before the page opens the events again after a refusal, milliseconds. */
export const EVENTS_RETRY_FIRST_MS = 1000;
/** The longest such wait: each refusal in a row doubles it, up to this, milliseconds. */
export const EVENTS_RETRY_MOST_MS = 15000;

/**
 * Open the live events (`/api/events`) and keep them open.
 *
 * After a dropped connection the browser reconnects by itself and resumes
 * after the last event it took. After an answer that is not a stream (serve's
 * 503 when it already serves as many streams as it may, or while it closes)
 * the browser gives the stream up for good, so the page opens a new one after
 * a wait that starts at {@link EVENTS_RETRY_FIRST_MS} and doubles with each
 * refusal in a row, up to {@link EVENTS_RETRY_MOST_MS}.
 *
 * @param {EventHandlers} handlers What to do with each kind.
 * @param {(open: boolean) => void} onLink Told when the stream opens or drops.
 * @returns {{ close: () => void }} What stops it, and any retry it waits for.
 */
export function openEvents(handlers, onLink) {
    let wait = EVENTS_RETRY_FIRST_MS;
    /** @type {EventSource | null} */
    let current = null;
    /** @type {ReturnType<typeof setTimeout> | null} */
    let pending = null;
    let closed = false;
    const open = () => {
        pending = null;
        const source = new EventSource("/api/events");
        current = source;
        for (const [kind, handle] of Object.entries(handlers)) {
            source.addEventListener(kind, (event) => {
                // Serve's `error` events share their name with the stream's own
                // error, which carries no data (the connection dropped or ended).
                if (event instanceof MessageEvent && typeof event.data === "string") {
                    handle(JSON.parse(event.data));
                }
            });
        }
        source.addEventListener("open", () => {
            wait = EVENTS_RETRY_FIRST_MS;
            onLink(true);
        });
        source.addEventListener("error", () => {
            onLink(false);
            if (source.readyState === EventSource.CLOSED && !closed) {
                source.close();
                pending = setTimeout(open, wait);
                wait = Math.min(2 * wait, EVENTS_RETRY_MOST_MS);
            }
        });
    };
    open();
    return {
        close: () => {
            closed = true;
            if (pending !== null) {
                clearTimeout(pending);
            }
            current?.close();
        },
    };
}
