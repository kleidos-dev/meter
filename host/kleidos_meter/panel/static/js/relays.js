// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Relays view: the measuring circuit drawn from `ROUTe:STATe?`
 * (the cell, the shunt, Relay 1 on the DUT's battery lead and Relay 2 across
 * the shunt, each with the pin that drives it and its active level, the wires
 * that carry the DUT's current marked), the hold's countdown, and one control
 * per relay order the schema declares.
 *
 * An order goes out with only the flags a person ticked, never one the
 * panel adds (`POST /api/relays/<name>`). Before an order with `FORCe` or
 * `LIVE` a dialog names what moves and what the path becomes, and the
 * order goes out only on its confirmation. Relays 3 and 4 are not wired on the
 * rig: their controls are shown, and disabled. What the instrument refuses
 * is shown beside the order that caused it, its code first.
 */

import { get, post } from "./api.js";
import { confirmAction } from "./dialogs.js";
import { fillPairs, need, shown, showRefusal } from "./dom.js";

/** Seconds between two reads of the circuit while the view is shown and a hold runs. */
const HOLD_READ_S = 10;

/**
 * The two contacts and the path they make (README, "The relays").
 *
 * @typedef {object} Circuit
 * @property {boolean} k1 Whether Relay 1, the lead, is closed (the DUT fed through the shunt).
 * @property {boolean} k2 Whether Relay 2, the bypass, is closed (the DUT fed around the shunt).
 */

/**
 * The path two contacts make, as `path=` names it.
 *
 * @param {Circuit} circuit The contacts.
 * @returns {"shunt" | "bypass" | "both" | "none"} The path.
 */
export function pathOf(circuit) {
    if (circuit.k1) {
        return circuit.k2 ? "both" : "shunt";
    }
    return circuit.k2 ? "bypass" : "none";
}

/** What each path means for the DUT and the shunt. */
export const PATHS = Object.freeze({
    shunt: "the DUT is fed through the shunt: the measuring circuit",
    bypass: "the DUT runs on the bypass and the shunt carries nothing: the null",
    both: "the shunt carries a fraction of the DUT's current: readings are q=invalid",
    none: "the DUT has no battery",
});

/**
 * The contacts as `ROUTe:STATe?` reports them.
 *
 * @param {Record<string, any>} state The report, as JSON.
 * @returns {Circuit | null} The contacts, or null when the report has none.
 */
export function circuitOf(state) {
    const k1 = String(state.k1 ?? "");
    const k2 = String(state.k2 ?? "");
    if (!k1.includes(":") || !k2.includes(":")) {
        return null;
    }
    return { k1: k1.endsWith(":closed"), k2: k2.endsWith(":closed") };
}

/**
 * What an order would move, and the path it leaves.
 *
 * @param {string} relay The order's name in the schema (`lead`, `bypass`, ...).
 * @param {string} choice Its first parameter's short form (`OPEN`, `ON`, ...), or `""` for a cycle.
 * @param {Circuit} before The contacts now.
 * @param {number} [ms] A cycle's opening, milliseconds.
 * @returns {{after: Circuit, lines: string[]}} The contacts after, and one line per move.
 */
export function predict(relay, choice, before, ms = 0) {
    const after = { ...before };
    /** @type {string[]} */
    const lines = [];
    const word = choice.toUpperCase();
    if (relay === "lead") {
        after.k1 = word.startsWith("CLOS");
        lines.push(after.k1 ? "Relay 1 closes: the DUT's battery lead is made through the shunt." : "Relay 1 opens: the DUT's battery lead is cut.");
    } else if (relay === "lead_cycle") {
        lines.push(`Relay 1 opens for ${ms} ms and recloses: the DUT loses its battery for that time and boots cold.`);
    } else if (relay === "bypass") {
        after.k2 = word === "ON";
        lines.push(after.k2 ? "Relay 2 closes: the DUT's current goes around the shunt." : "Relay 2 opens: the shunt carries the DUT's whole current again.");
    } else if (relay === "nullpath") {
        if (word === "ON") {
            after.k1 = false;
            after.k2 = true;
            lines.push("Relay 2 closes, then Relay 1 opens, each contact waited out: the DUT keeps a supply throughout.");
        } else {
            after.k1 = true;
            after.k2 = false;
            lines.push("Relay 1 closes, then Relay 2 opens, make-before-break: the DUT goes back on the shunt.");
        }
    } else {
        lines.push(`${relay} is not wired on the rig: the instrument refuses the order.`);
    }
    return { after, lines };
}

/**
 * The SVG segments that carry the DUT's current on a path.
 *
 * @param {string} path `path=`.
 * @returns {Set<string>} The `data-segment` names.
 */
export function liveSegments(path) {
    switch (path) {
        case "shunt":
            return new Set(["cell", "shunt", "k1", "dut"]);
        case "bypass":
            return new Set(["cell", "k2", "dut"]);
        case "both":
            return new Set(["cell", "shunt", "k1", "k2", "dut"]);
        default:
            return new Set();
    }
}

/**
 * The pin that drives a relay and the level that energizes it, as
 * `ROUTe:STATe?` reports them.
 *
 * @param {Record<string, any>} state The report, as JSON.
 * @param {1 | 2} relay Which.
 * @returns {string} For example `GPIO7, active low`.
 */
export function relayPin(state, relay) {
    const gpio = state[`relay${relay}_gpio`];
    if (typeof gpio !== "number") {
        return "no pin";
    }
    return `GPIO${gpio}, active ${shown(state[`relay${relay}_active`])}`;
}

/**
 * A relay order as `/api/schema` declares it.
 *
 * @typedef {object} RelayInfo
 * @property {string} name Its key.
 * @property {string} label What the page calls it.
 * @property {string[]} choices The words its first parameter takes; empty for a duration.
 * @property {string[]} options The flags it takes.
 * @property {boolean} wired False for a relay the rig does not wire.
 */

export class RelaysView {
    /**
     * @param {HTMLElement} root The view's section.
     * @param {Record<string, any>} schema `/api/schema`.
     * @param {import("./session.js").Session} session The session's state.
     * @param {() => void} [onOrder] Told when an order was answered: a null path that passes verifies the pin map's relays.
     */
    constructor(root, schema, session, onOrder = () => {}) {
        this.root = root;
        this.session = session;
        this.onOrder = onOrder;
        /** @type {RelayInfo[]} */
        this.relays = schema.relays ?? [];
        this.svg = need(root, "#circuit", SVGSVGElement);
        this.text = need(root, "#circuit-text", HTMLElement);
        this.holdText = need(root, "#hold-text", HTMLElement);
        this.stateList = need(root, "#relay-state", HTMLElement);
        this.refusal = need(root, "#relays-refusal", HTMLElement);
        /** @type {Circuit | null} */
        this.circuit = null;
        /** @type {string} */
        this.path = "";
        /** @type {number | null} The hold's deadline on the page's clock, ms. */
        this.holdUntil = null;
        /** @type {number | null} */
        this.ticker = null;
        /** @type {number} When the circuit was last read, on the page's clock. */
        this.readAt = 0;
        /** @type {string[]} Relays the schema names that the page has no control for. */
        this.missing = [];
        need(root, "#relays-refresh", HTMLButtonElement).addEventListener("click", () => void this.refresh());
        this.build();
        session.addEventListener("change", () => this.lockControls());
    }

    /** Wire each order's controls; disable the relays the rig does not wire. */
    build() {
        for (const relay of this.relays) {
            const card = this.root.querySelector(`[data-relay="${relay.name}"]`);
            if (!(card instanceof HTMLElement)) {
                this.missing.push(relay.name);
                continue;
            }
            card.dataset.wired = String(relay.wired);
            for (const node of card.querySelectorAll("[data-choice]")) {
                const button = /** @type {HTMLButtonElement} */ (node);
                button.addEventListener("click", () => void this.order(relay, card, button.dataset.choice ?? ""));
            }
        }
        this.lockControls();
    }

    /** Enable the orders the session allows: none while another controller holds the line. */
    lockControls() {
        const known = this.session.known;
        const why = known.held !== null ? `the line is held by ${known.held}` : known.sequence !== null ? `sequence ${known.sequence} runs` : null;
        for (const relay of this.relays) {
            const card = this.root.querySelector(`[data-relay="${relay.name}"]`);
            if (!(card instanceof HTMLElement)) {
                continue;
            }
            for (const node of card.querySelectorAll("button, input")) {
                /** @type {HTMLButtonElement | HTMLInputElement} */ (node).disabled = !relay.wired || why !== null;
            }
        }
    }

    /** The view is shown: read the circuit, and count the hold down. */
    shown() {
        void this.refresh();
        this.ticker = window.setInterval(() => this.tick(), 1000);
    }

    /** The view is hidden: no more reads. */
    hidden() {
        if (this.ticker !== null) {
            window.clearInterval(this.ticker);
            this.ticker = null;
        }
    }

    /** One second passed: the countdown, and a read now and then while a hold runs. */
    tick() {
        this.showHold();
        if (this.holdUntil !== null && performance.now() - this.readAt > HOLD_READ_S * 1000) {
            void this.refresh();
        }
    }

    /** Read `ROUTe:STATe?` and `ROUTe:RESult?` and draw them. */
    async refresh() {
        const answer = await get("/api/relays");
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return;
        }
        showRefusal(this.refusal, null);
        this.draw(answer.body.state, answer.body.result);
    }

    /**
     * Draw the circuit, the hold and the last order.
     *
     * @param {Record<string, any>} state `ROUTe:STATe?`, as JSON.
     * @param {Record<string, any> | null} result `ROUTe:RESult?`, as JSON.
     */
    draw(state, result) {
        const pin1 = relayPin(state, 1);
        const pin2 = relayPin(state, 2);
        need(this.svg, "#k1-pin", SVGTextElement).textContent = pin1;
        need(this.svg, "#k2-pin", SVGTextElement).textContent = pin2;
        this.readAt = performance.now();
        const circuit = circuitOf(state);
        this.circuit = circuit;
        this.path = String(state.path ?? (circuit === null ? "" : pathOf(circuit)));
        if (circuit !== null) {
            this.blade("#k1-blade", circuit.k1, 340, 50, 400);
            this.blade("#k2-blade", circuit.k2, 300, 140, 360);
        }
        const live = liveSegments(this.path);
        for (const node of this.svg.querySelectorAll("[data-segment]")) {
            const segment = /** @type {SVGElement} */ (node);
            segment.classList.toggle("live", live.has(segment.dataset.segment ?? ""));
        }
        const meaning = PATHS[/** @type {keyof typeof PATHS} */ (this.path)] ?? "unknown";
        const k1 = circuit === null ? "?" : circuit.k1 ? "closed" : "open";
        const k2 = circuit === null ? "?" : circuit.k2 ? "closed" : "open";
        this.text.textContent = `path=${this.path}: Relay 1 (${pin1}) ${k1}, Relay 2 (${pin2}) ${k2}; ${meaning}. Check: ${shown(state.check)}, pads: ${shown(state.pads)}.`;
        this.text.dataset.state = this.path === "shunt" ? "done" : "pending";
        const hold = typeof state.hold_s === "number" ? state.hold_s : null;
        this.holdUntil = hold === null ? null : this.readAt + hold * 1000;
        this.showHold();
        fillPairs(this.stateList, [
            ["Relay 1", state.k1],
            ["Relay 2", state.k2],
            ["Relay 3", state.k3],
            ["Relay 4", state.k4],
            ["Hold releases", state.hold_released],
            ["Last order moved", result?.moved],
            ["Its settle", result?.settle_us === undefined || result?.settle_us === null ? null : `${result.settle_us} µs`],
            ["q", result?.q],
        ]);
    }

    /**
     * Put a contact's blade closed or lifted.
     *
     * @param {string} selector The blade's `<line>`.
     * @param {boolean} closed Whether the contact is closed.
     * @param {number} x The pivot's x.
     * @param {number} y The pivot's and the contact's y.
     * @param {number} contact The contact's x.
     */
    blade(selector, closed, x, y, contact) {
        const line = this.svg.querySelector(selector);
        if (line === null) {
            return;
        }
        line.setAttribute("x2", String(closed ? contact : contact - 8));
        line.setAttribute("y2", String(closed ? y : y - 24));
        line.classList.toggle("closed", closed);
    }

    /** Write the hold's countdown. */
    showHold() {
        if (this.holdUntil === null) {
            this.holdText.textContent = this.path === "" ? "" : "No hold: the relays rest on the measuring circuit.";
            delete this.holdText.dataset.state;
            return;
        }
        const left = Math.max(0, Math.round((this.holdUntil - performance.now()) / 1000));
        this.holdText.textContent = `The hold puts every coil back to rest in ${left} s, make-before-break, unless a relay order arms it again.`;
        this.holdText.dataset.state = "pending";
    }

    /**
     * Send one order, after its confirmation when it carries `FORCe` or `LIVE`.
     *
     * @param {RelayInfo} relay The order.
     * @param {HTMLElement} card Its controls.
     * @param {string} choice Its first parameter, `""` for a cycle.
     */
    async order(relay, card, choice) {
        const where = need(card, "[data-refusal]", HTMLElement);
        showRefusal(where, null);
        const flag = (/** @type {string} */ name) => {
            const box = card.querySelector(`[data-flag="${name}"]`);
            return box instanceof HTMLInputElement && box.checked;
        };
        /** @type {Record<string, unknown>} */
        const body = { force: flag("force"), live: flag("live") };
        let ms = 0;
        if (relay.choices.length === 0) {
            ms = need(card, "[data-ms]", HTMLInputElement).valueAsNumber;
            if (!Number.isInteger(ms) || ms < 1 || ms > 60000) {
                showRefusal(where, "the opening is a whole number of ms, 1 to 60000");
                return;
            }
            body.ms = ms;
        } else {
            body.state = choice;
        }
        if (body.force || body.live) {
            const before = this.circuit ?? { k1: true, k2: false };
            const { after, lines } = predict(relay.name, choice, before, ms);
            const flags = [];
            if (body.force) {
                flags.push("FORCe: the interlock that refuses this order is overridden.");
            }
            if (body.live) {
                flags.push("LIVE: the move happens under the running capture and is marked in it.");
            }
            const next = relay.name === "lead_cycle" ? "shunt (after the reclose)" : pathOf(after);
            const go = await confirmAction({
                title: `${relay.label}: what moves`,
                lines: [...lines, `path=${this.path || pathOf(before)} becomes path=${next}: ${PATHS[pathOf(after)]}.`, ...flags],
                go: "Send the order",
            });
            if (!go) {
                showRefusal(where, "not sent: the order was not confirmed");
                return;
            }
        }
        const answer = await post(`/api/relays/${encodeURIComponent(relay.name)}`, body);
        if (answer.body.state && typeof answer.body.state === "object") {
            this.draw(answer.body.state, answer.body.result ?? null);
        }
        this.onOrder();
        if (!answer.ok) {
            showRefusal(where, answer);
        }
    }
}
