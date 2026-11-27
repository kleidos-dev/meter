// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Pins view: the board drawn from `GET /api/pins`, a draft of the
 * pin map made on it, and the orders that send it.
 *
 * Every terminal of both rails is a button of the drawing, marked by its
 * state's color, shape and word. The drawing is one tab stop: the arrows move
 * between the pins (up and down a rail, left and right across), Home and End
 * go to a rail's ends, Enter or Space opens the pin's dialog, and Escape
 * closes it. What the pin is, and why, is written beside the drawing while it
 * has the pointer or the focus.
 *
 * The draft is the page's own until Apply: it is judged here by the
 * firmware's rules (`pins-math.js`), its conflicts are drawn on the pins and
 * named, and what differs from the live map is listed. Apply stages it
 * (`POST /api/pins/stage`) and applies it (`POST /api/pins/apply`), each after
 * the dialog that says what happens; the instrument proves it and keeps it, or
 * puts the last good map back, and the answer is shown. While the
 * instrument's table is another than the package's the view only shows.
 */

import { get, post } from "./api.js";
import { confirmAction } from "./dialogs.js";
import { fillPairs, need, showRefusal } from "./dom.js";
import {
    addressText,
    differences,
    mapFrom,
    pinState,
    problemsOf,
    problemText,
    resultText,
    roleProblem,
    roleReason,
    rolesAt,
    stageOrder,
    withUse,
} from "./pins-math.js";

/** @typedef {import("./pins-math.js").Board} Board */
/** @typedef {import("./pins-math.js").PinMap} PinMap */
/** @typedef {import("./pins-math.js").Row} Row */
/** @typedef {import("./pins-math.js").RoleKey} RoleKey */

/** The drawing's size and a row's, in the units of its `viewBox`: CSS pixels, never scaled. */
const WIDTH = 860;
const ROW_FIRST = 62;
const ROW_PITCH = 30;
const ROW_HEIGHT = 26;

/** Where each column of a rail is, x: the row's box, the word, the mark, the terminal, the GPIO, its number. */
const COLUMNS = Object.freeze({
    left: { box: 4, word: 198, wordAnchor: "end", mark: 216, terminal: 234, gpio: 280, gpioAnchor: "start", number: 352 },
    right: { box: 522, word: 662, wordAnchor: "start", mark: 644, terminal: 588, gpio: 580, gpioAnchor: "end", number: 508 },
});

/** The width of a row's box and of a terminal. */
const BOX_WIDTH = 334;
const TERMINAL_WIDTH = 38;

/** Each state's mark, drawn around (0, 0): a shape of its own, not a color only. */
const MARKS = Object.freeze({
    free: "M 5 0 A 5 5 0 1 1 -5 0 A 5 5 0 1 1 5 0 Z",
    used: "M -5 -5 H 5 V 5 H -5 Z",
    caution: "M 0 -6 L 6 5 H -6 Z",
    reserved: "M -5 -5 L 5 5 M 5 -5 L -5 5",
    conflict: "M 0 -8 L 8 0 L 0 8 L -8 0 Z M 0 -4 V 1 M 0 3 V 4",
});

/**
 * An SVG element with its attributes and children.
 *
 * @param {string} tag The element.
 * @param {Record<string, string | number>} attributes Its attributes.
 * @param {...(Element | string)} children What is in it.
 * @returns {SVGElement} The element.
 */
function svg(tag, attributes, ...children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) {
        node.setAttribute(name, String(value));
    }
    node.append(...children);
    return node;
}

/**
 * A `<select>` of words, one chosen, in a labeled field.
 *
 * @param {string} id Its id.
 * @param {string} label Its label.
 * @param {readonly (readonly [string, string])[]} choices Each value and its text.
 * @param {string} chosen The value chosen.
 * @returns {{field: HTMLElement, control: HTMLSelectElement}} The field and its control.
 */
function selectField(id, label, choices, chosen) {
    const control = document.createElement("select");
    for (const [value, text] of choices) {
        control.append(new Option(text, value, false, value === chosen));
    }
    return { field: labeled(id, label, control), control };
}

/**
 * A control in a labeled field.
 *
 * @param {string} id The control's id.
 * @param {string} text The label.
 * @param {HTMLElement} control The control.
 * @returns {HTMLElement} The field.
 */
function labeled(id, text, control) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = text;
    control.id = id;
    field.append(label, control);
    return field;
}

export class PinsView {
    /**
     * @param {HTMLElement} root The view's section.
     * @param {import("./session.js").Session} session The session's state.
     * @param {import("./status.js").StatusBar} status The status bar, told each map read.
     */
    constructor(root, session, status) {
        this.root = root;
        this.session = session;
        this.status = status;
        this.svg = need(root, "#board", SVGSVGElement);
        this.about = need(root, "#pin-about", HTMLElement);
        this.notice = need(root, "#pins-notice", HTMLElement);
        this.problemList = need(root, "#pins-problems", HTMLElement);
        this.diffList = need(root, "#pins-diff", HTMLElement);
        this.reason = need(root, "#pins-reason", HTMLElement);
        this.refusal = need(root, "#pins-refusal", HTMLElement);
        this.resultLine = need(root, "#pins-result", HTMLElement);
        this.resultList = need(root, "#pins-result-list", HTMLElement);
        this.applyButton = need(root, "#pins-apply", HTMLButtonElement);
        this.defaultsButton = need(root, "#pins-defaults", HTMLButtonElement);
        this.discardButton = need(root, "#pins-discard", HTMLButtonElement);
        this.dialog = need(root.ownerDocument, "#pin-dialog", HTMLDialogElement);
        this.form = need(this.dialog, "#pin-form", HTMLFormElement);
        this.uses = need(this.dialog, "#pin-uses", HTMLElement);
        this.config = need(this.dialog, "#pin-config", HTMLElement);
        this.dialogProblem = need(this.dialog, "#pin-problem", HTMLElement);
        this.keepButton = need(this.dialog, "#pin-keep", HTMLButtonElement);
        /** @type {Board | null} */
        this.board = null;
        /** @type {PinMap | null} The instrument's map. */
        this.live = null;
        /** @type {PinMap | null} The map on the page. */
        this.draft = null;
        /** Whether a person changed the draft since it was last the instrument's. */
        this.edited = false;
        /** Whether the instrument's table is the package's. */
        this.matches = true;
        /** Whether an order of this view is being answered. */
        this.working = false;
        /** @type {Map<Row, SVGElement>} Each terminal's element. */
        this.pins = new Map();
        /** @type {Row | null} The terminal that has the drawing's tab stop. */
        this.current = null;
        /** @type {Row | null} The terminal whose dialog is open. */
        this.edits = null;
        /** @type {PinMap | null} The draft as the dialog would leave it. */
        this.candidate = null;
        need(root, "#pins-refresh", HTMLButtonElement).addEventListener("click", () => void this.refresh());
        this.applyButton.addEventListener("click", () => void this.apply());
        this.defaultsButton.addEventListener("click", () => void this.defaults());
        this.discardButton.addEventListener("click", () => void this.discard());
        this.svg.addEventListener("keydown", (event) => this.key(event));
        this.svg.addEventListener("pointerleave", () => {
            if (this.current !== null) {
                this.describe(this.current);
            }
        });
        this.form.addEventListener("input", () => this.judgeDialog());
        this.form.addEventListener("submit", (event) => {
            if (event.submitter === this.keepButton) {
                this.keep();
            }
        });
        this.dialog.addEventListener("close", () => {
            if (this.dialog.open) {
                return; // an earlier close, queued before this pin was opened
            }
            this.edits = null;
            this.candidate = null;
        });
        session.addEventListener("change", () => this.lockControls());
    }

    /** The view is shown: read the map. */
    shown() {
        void this.refresh();
    }

    /** Read the board and the maps, keeping a draft a person has changed. */
    async refresh() {
        const answer = await get("/api/pins");
        this.status.setPins(answer);
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return;
        }
        showRefusal(this.refusal, null);
        const body = answer.body;
        if (this.board === null) {
            this.build(body.board);
        }
        this.matches = Boolean(body.table?.matches);
        this.notice.hidden = this.matches;
        this.notice.textContent = this.matches
            ? ""
            : `Read only: the instrument's pin table is ${body.table?.instrument} and this package's ${body.table?.package}, so the board shown is not the one it judges by. Defaults still works.`;
        this.take(body.live, body.pending);
        this.showResult(body.result);
    }

    /**
     * Take the maps the instrument gave; the draft follows the staged one unless a person changed it.
     *
     * @param {Record<string, unknown>} live `SYSTem:PINS?`, as JSON.
     * @param {Record<string, unknown>} pending `SYSTem:PINS:PENDing?`, as JSON.
     */
    take(live, pending) {
        this.live = mapFrom(live);
        if (!this.edited || this.draft === null) {
            this.draft = mapFrom(pending);
        }
        this.draw();
    }

    /**
     * Draw the board once: the carrier, the module, its two USB-C ports and every terminal.
     *
     * @param {Board} board The table.
     */
    build(board) {
        this.board = board;
        const rails = /** @type {const} */ (["left", "right"]);
        const rows = rails.map((rail) =>
            board.rows.filter((row) => row.rail === rail).sort((a, b) => a.terminal - b.terminal));
        const count = Math.max(...rows.map((rail) => rail.length));
        const bottom = ROW_FIRST + count * ROW_PITCH;
        const height = bottom + 70;
        this.svg.setAttribute("viewBox", `0 0 ${WIDTH} ${height}`);
        this.svg.setAttribute("width", String(WIDTH));
        this.svg.setAttribute("height", String(height));
        const usb = (/** @type {number} */ x) => [
            svg("rect", { class: "usb", x, y: bottom + 10, width: 62, height: 34, rx: 8 }),
            svg("text", { class: "small", x: x + 31, y: bottom + 31, "text-anchor": "middle" }, "USB-C"),
        ];
        this.svg.replaceChildren(
            svg("title", { id: "board-title" }, board.board.name),
            svg(
                "g",
                { "aria-hidden": "true" },
                svg("rect", { class: "carrier", x: 226, y: 40, width: 408, height: height - 44, rx: 10 }),
                svg("rect", { class: "module", x: 344, y: 22, width: 172, height: bottom + 8, rx: 8 }),
                svg("rect", { class: "shield", x: 366, y: 74, width: 128, height: 170, rx: 4 }),
                svg("text", { x: 430, y: 56, "text-anchor": "middle", class: "small" }, "antenna"),
                svg("text", { x: 430, y: 164, "text-anchor": "middle" }, "ESP32-S3"),
                ...usb(360),
                ...usb(438),
                svg("text", { x: 8, y: 30, class: "small" }, "Left rail"),
                svg("text", { x: WIDTH - 8, y: 30, class: "small", "text-anchor": "end" }, "Right rail"),
            ),
            ...rows.flat().map((row) => this.buildPin(row)),
        );
        this.current = rows[0][0];
    }

    /**
     * Draw one terminal.
     *
     * @param {Row} row The terminal.
     * @returns {SVGElement} Its group.
     */
    buildPin(row) {
        const at = COLUMNS[/** @type {"left" | "right"} */ (row.rail)];
        const y = ROW_FIRST + (row.terminal - 1) * ROW_PITCH;
        const middle = y + ROW_HEIGHT / 2;
        const text = (/** @type {string} */ name, /** @type {number} */ x, /** @type {string} */ anchor, said = "") =>
            svg("text", { class: name, x, y: middle + 5, "text-anchor": anchor }, said);
        const pin = svg(
            "g",
            {
                class: "pin",
                role: "button",
                "aria-haspopup": "dialog",
                tabindex: -1,
                "data-rail": row.rail,
                "data-terminal": row.terminal,
                ...(row.gpio === null ? {} : { "data-gpio": row.gpio }),
            },
            svg("rect", { class: "row", x: at.box, y, width: BOX_WIDTH, height: ROW_HEIGHT, rx: 6 }),
            text("word", at.word, at.wordAnchor),
            svg("path", { class: "mark", transform: `translate(${at.mark} ${middle})` }),
            svg("rect", { class: "terminal", x: at.terminal, y: y + 2, width: TERMINAL_WIDTH, height: ROW_HEIGHT - 4, rx: 3 }),
            text("silk", at.terminal + TERMINAL_WIDTH / 2, "middle", row.silk),
            text("gpio", at.gpio, at.gpioAnchor, row.gpio === null ? "" : `GPIO${row.gpio}`),
            text("number small", at.number, at.gpioAnchor, String(row.terminal)),
        );
        pin.addEventListener("click", () => this.open(row));
        pin.addEventListener("pointerenter", () => this.describe(row));
        pin.addEventListener("focus", () => {
            this.point(row);
            this.describe(row);
        });
        this.pins.set(row, pin);
        return pin;
    }

    /**
     * Give a terminal the drawing's one tab stop.
     *
     * @param {Row} row The terminal.
     */
    point(row) {
        this.current = row;
        for (const [other, pin] of this.pins) {
            pin.setAttribute("tabindex", other === row ? "0" : "-1");
            pin.toggleAttribute("aria-describedby", false);
        }
        this.pins.get(row)?.setAttribute("aria-describedby", this.about.id);
    }

    /**
     * The terminal a key moves to.
     *
     * @param {Row} from Where the focus is.
     * @param {string} key The key.
     * @returns {Row | null} The terminal, `null` for a key that moves nowhere.
     */
    moved(from, key) {
        const rail = [...this.pins.keys()].filter((row) => row.rail === from.rail);
        const other = [...this.pins.keys()].filter((row) => row.rail !== from.rail);
        const index = from.terminal - 1;
        switch (key) {
            case "ArrowUp":
                return rail[Math.max(0, index - 1)];
            case "ArrowDown":
                return rail[Math.min(rail.length - 1, index + 1)];
            case "ArrowLeft":
                return from.rail === "right" ? other[index] : from;
            case "ArrowRight":
                return from.rail === "left" ? other[index] : from;
            case "Home":
                return rail[0];
            case "End":
                return rail[rail.length - 1];
            default:
                return null;
        }
    }

    /**
     * A key on the drawing: move, or open the pin.
     *
     * @param {KeyboardEvent} event The key.
     */
    key(event) {
        const row = this.current;
        if (row === null || event.altKey || event.ctrlKey || event.metaKey) {
            return;
        }
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            this.open(row);
            return;
        }
        const next = this.moved(row, event.key);
        if (next !== null) {
            event.preventDefault();
            this.pins.get(next)?.focus();
        }
    }

    /**
     * Say what a terminal is, and why, beside the drawing.
     *
     * @param {Row} row The terminal.
     */
    describe(row) {
        const { board, live, draft } = this;
        if (board === null || live === null || draft === null) {
            return;
        }
        const state = pinState(row, draft, live, board);
        const group = board.groups[row.group];
        const heading = document.createElement("h3");
        heading.textContent = `${row.rail} ${row.terminal}, ${row.gpio === null ? row.silk : `GPIO${row.gpio}`}: ${state.word}`;
        const lines = [
            row.hover,
            ...(row.group === "none" ? [] : [`${group.sentence} (${group.source})`]),
            ...(state.state === "used" ? [`In this map: ${state.what}.`] : []),
            ...state.conflicts.map((text) => `Conflict: ${text}`),
        ];
        this.about.replaceChildren(
            heading,
            ...lines.map((line) => {
                const paragraph = document.createElement("p");
                paragraph.className = line.startsWith("Conflict: ") ? "refusal" : "hint";
                paragraph.textContent = line;
                return paragraph;
            }),
        );
    }

    /** Draw the draft: each pin's state, the conflicts, what differs, and what may be pressed. */
    draw() {
        const { board, live, draft } = this;
        if (board === null || live === null || draft === null) {
            return;
        }
        for (const [row, pin] of this.pins) {
            const state = pinState(row, draft, live, board);
            const conflict = state.conflicts.length > 0;
            pin.dataset.state = state.state;
            pin.dataset.conflict = String(conflict);
            pin.dataset.changed = String(state.changed);
            pin.setAttribute("aria-label", state.name);
            pin.setAttribute("tabindex", row === this.current ? "0" : "-1");
            need(pin, ".mark", SVGElement).setAttribute("d", MARKS[conflict ? "conflict" : state.state]);
            const word = state.what === "" ? state.word : `${state.what} · ${short(state.word)}`;
            need(pin, ".word", SVGElement).textContent = conflict ? `conflict · ${state.what || state.word}` : word;
        }
        const problems = problemsOf(draft, board).map((problem) => problemText(problem, draft, board));
        this.problemList.replaceChildren(...items(problems));
        this.diffList.replaceChildren(
            ...items(differences(live, draft, board).map(({ label, from, to }) => `${label}: ${from} → ${to}`)),
        );
        if (this.current !== null) {
            this.describe(this.current);
        }
        this.lockControls();
    }

    /**
     * Why nothing may be changed now, or null.
     *
     * @returns {string | null} The reason.
     */
    busy() {
        if (this.working) {
            return "an order is being answered";
        }
        const refused = this.session.refusedBy("RC");
        if (refused !== null) {
            return refused;
        }
        return this.session.known.capture === "idle" ? null : "a capture runs: stop it first";
    }

    /** Enable what may be pressed, and say why Apply may not. */
    lockControls() {
        const { board, live, draft } = this;
        const busy = this.busy();
        let why = busy;
        if (board === null || live === null || draft === null) {
            why = "the map is not read yet";
        } else if (!this.matches) {
            why = "the instrument's pin table is another";
        } else if (problemsOf(draft, board).length > 0) {
            why ??= "the map breaks a rule: see the conflicts";
        } else if (differences(live, draft, board).length === 0) {
            why ??= "nothing differs from the live map";
        }
        this.applyButton.disabled = why !== null;
        this.reason.textContent = why === null ? "Ready to apply: nothing has been sent yet." : `Apply is disabled: ${why}.`;
        this.defaultsButton.disabled = live === null || busy !== null;
        this.discardButton.disabled = this.working || live === null || draft === null || board === null
            || differences(live, draft, board).length === 0;
    }

    // -- a pin's dialog ---------------------------------------------------------------

    /**
     * Open a pin's dialog on the draft.
     *
     * @param {Row} row The terminal.
     */
    open(row) {
        const { board, draft } = this;
        if (board === null || draft === null || this.working) {
            return;
        }
        this.point(row);
        this.edits = row;
        this.candidate = draft;
        need(this.dialog, "#pin-title", HTMLElement).textContent = row.gpio === null
            ? `${row.rail} ${row.terminal}, ${row.silk}`
            : `${row.rail} ${row.terminal}, GPIO${row.gpio}`;
        const group = board.groups[row.group];
        need(this.dialog, "#pin-what", HTMLElement).textContent = row.group === "none"
            ? row.hover
            : `${row.hover} ${group.sentence}`;
        this.uses.replaceChildren(need(this.uses, "legend", HTMLElement), ...this.useChoices(row));
        const chosen = this.uses.querySelector("input:checked") ?? this.uses.querySelector("input:not(:disabled)");
        if (chosen instanceof HTMLInputElement) {
            chosen.autofocus = true;
        }
        this.config.replaceChildren();
        this.judgeDialog(true);
        this.dialog.showModal();
    }

    /**
     * A radio button per use of a pin, the ones it cannot take disabled with their reason.
     *
     * @param {Row} row The terminal.
     * @returns {HTMLElement[]} The choices.
     */
    useChoices(row) {
        const board = /** @type {Board} */ (this.board);
        const draft = /** @type {PinMap} */ (this.draft);
        const here = rolesAt(draft, row.gpio, board);
        const choice = (/** @type {string} */ value, /** @type {string} */ text, /** @type {string | null} */ why) => {
            const label = document.createElement("label");
            const input = document.createElement("input");
            input.type = "radio";
            input.name = "use";
            input.value = value;
            input.checked = value === (here.length === 1 ? here[0].key : here.length === 0 ? "" : null);
            input.disabled = why !== null;
            label.append(input, why === null ? ` ${text}` : ` ${text}: ${why}`);
            return label;
        };
        const roles = board.roles.map((role) => {
            if (row.gpio === null) {
                return choice(role.key, role.label, "not a GPIO");
            }
            const problem = roleProblem(role, row.gpio, board);
            const why = problem === null ? null : roleReason(role, row.gpio, problem, board);
            const now = draft[role.key];
            const where = now === null ? ", no pin now" : now === row.gpio ? "" : `, GPIO${now} now`;
            return choice(role.key, why === null ? `${role.label}${where}` : role.label, why);
        });
        return [...roles, choice("", "unused", null)];
    }

    /**
     * The use chosen in the dialog: a role, `null` for unused, `undefined` for none chosen.
     *
     * @returns {RoleKey | null | undefined} The use.
     */
    chosenUse() {
        const input = this.uses.querySelector("input:checked");
        if (!(input instanceof HTMLInputElement)) {
            return undefined;
        }
        return input.value === "" ? null : /** @type {RoleKey} */ (input.value);
    }

    /**
     * Something in the dialog changed: show the configuration of the use
     * chosen, make the draft it would leave, and say what breaks.
     *
     * @param {boolean} [opening] Whether the dialog is being opened.
     */
    judgeDialog(opening = false) {
        const row = this.edits;
        const board = this.board;
        const draft = this.draft;
        if (row === null || board === null || draft === null) {
            return;
        }
        const use = this.chosenUse();
        if (opening || this.config.dataset.use !== String(use)) {
            this.config.dataset.use = String(use);
            this.config.replaceChildren(...(use === undefined || use === null ? [] : this.configFields(use, draft)));
        }
        let next = draft;
        if (use !== undefined && row.gpio !== null) {
            next = withUse(draft, row.gpio, use, board);
            if (use !== null) {
                next = { ...next, ...this.configOf(use) };
            }
        }
        this.candidate = next;
        const problems = problemsOf(next, board).map((problem) => problemText(problem, next, board));
        const blocked = row.gpio === null || row.class === "reserved";
        this.dialogProblem.textContent = !this.matches
            ? "Read only: the instrument's pin table is another."
            : blocked && rolesAt(draft, row.gpio, board).length === 0
                ? "This pin cannot be assigned."
                : problems.join(" ");
        this.keepButton.disabled = !this.matches || differences(draft, next, board).length === 0;
    }

    /**
     * The configuration fields of a use.
     *
     * @param {RoleKey} use The role.
     * @param {PinMap} map The map whose values they start at.
     * @returns {HTMLElement[]} The fields.
     */
    configFields(use, map) {
        if (use === "relay1" || use === "relay2") {
            return [
                selectField("pin-option", "Active level", [["low", "low"], ["high", "high"]], map[`${use}_active`]).field,
            ];
        }
        const fields = [
            selectField("pin-option", "Internal pull", [["up", "pull-up"], ["off", "off"]], map[`pull_${use}`]).field,
        ];
        if (use === "alert") {
            return fields;
        }
        const bus = /** @type {Board} */ (this.board).bus;
        const clock = document.createElement("input");
        clock.type = "number";
        clock.min = String(bus.clock_min_hz);
        clock.max = String(bus.clock_max_hz);
        clock.step = "1";
        clock.value = Number.isNaN(map.scl_hz) ? "" : String(map.scl_hz);
        /** @type {[string, string][]} */
        const addresses = [["auto", "auto (scan)"]];
        for (let address = bus.address_first; address <= bus.address_last; address += 1) {
            addresses.push([String(address), addressText(address)]);
        }
        return [
            ...fields,
            labeled("pin-clock", "I2C clock at boot, Hz", clock),
            selectField("pin-address", "INA228 address", addresses, String(map.addr)).field,
        ];
    }

    /**
     * What the configuration fields of a use hold.
     *
     * @param {RoleKey} use The role.
     * @returns {Partial<PinMap>} The fields of the map.
     */
    configOf(use) {
        const option = need(this.config, "#pin-option", HTMLSelectElement).value;
        if (use === "relay1" || use === "relay2") {
            return { [`${use}_active`]: option };
        }
        /** @type {Record<string, unknown>} */
        const fields = { [`pull_${use}`]: option };
        if (use !== "alert") {
            const address = need(this.config, "#pin-address", HTMLSelectElement).value;
            fields.scl_hz = need(this.config, "#pin-clock", HTMLInputElement).valueAsNumber;
            fields.addr = address === "auto" ? "auto" : Number(address);
        }
        return fields;
    }

    /** Keep was pressed: the draft is what the dialog holds. */
    keep() {
        if (this.candidate !== null) {
            this.draft = this.candidate;
            this.edited = true;
            this.draw();
        }
    }

    // -- the orders --------------------------------------------------------------------

    /** Forget the draft, on the page and, if one is staged, on the instrument. */
    async discard() {
        showRefusal(this.refusal, null);
        this.edited = false;
        this.draft = this.live;
        this.draw();
        await this.work(async () => {
            const answer = await post("/api/pins/stage", { clear: true });
            if (!answer.ok) {
                showRefusal(this.refusal, answer);
            }
        });
    }

    /** Stage the draft and apply it, after its confirmation. */
    async apply() {
        const { board, live, draft } = this;
        if (board === null || live === null || draft === null) {
            return;
        }
        const changes = differences(live, draft, board);
        const relays = changes.some(({ key }) => key.startsWith("relay"));
        const go = await confirmAction({
            title: "Apply the pin map",
            lines: [
                ...changes.map(({ label, from, to }) => `${label}: ${from} → ${to}`),
                "The wires must already be where the map says.",
                "The instrument proves the INA228 on the new map. A bus that does not answer reverts by itself: the last good map comes back, and the panel says why.",
                ...(relays
                    ? ["A relay's pin or level changes: the null-path check runs after it. Relay 2 closes and Relay 1 opens for a moment, and both go back.",
                          "Under 2 mA the instrument cannot see a contact move: a Relay 2 not wired to its new pin leaves the DUT without its battery for that moment.",
                          "A wrong active level acts at rest: Relay 1 then cuts the DUT's battery, Relay 2 bypasses the shunt, now and after every reboot. From 2 mA the instrument sees it and puts the last good map back; under 2 mA it cannot, and Defaults is the way back."]
                    : []),
            ],
            go: "Apply",
        });
        if (!go) {
            showRefusal(this.refusal, "not sent: the change was not confirmed");
            return;
        }
        showRefusal(this.refusal, null);
        await this.work(async () => {
            const staged = await post("/api/pins/stage", stageOrder(live, draft, board));
            if (!staged.ok) {
                showRefusal(this.refusal, staged);
                return;
            }
            this.answered(await post("/api/pins/apply"));
        });
    }

    /** Put the rig's own map back, after its confirmation. */
    async defaults() {
        const go = await confirmAction({
            title: "Back to the default pins",
            lines: [
                "The rig's own pin map is made the live one and saved. It needs no INA228: the order works while none answers.",
                "If a relay's pin or level changes, the null-path check runs after it.",
            ],
            go: "Use the defaults",
        });
        if (!go) {
            showRefusal(this.refusal, "not sent: the defaults were not confirmed");
            return;
        }
        showRefusal(this.refusal, null);
        await this.work(async () => this.answered(await post("/api/pins/defaults")));
    }

    /**
     * Send orders with everything locked, then unlock.
     *
     * @param {() => Promise<void>} orders What to send.
     */
    async work(orders) {
        this.working = true;
        this.root.setAttribute("aria-busy", "true");
        this.lockControls();
        try {
            await orders();
        } finally {
            this.working = false;
            this.root.removeAttribute("aria-busy");
            this.lockControls();
        }
    }

    /**
     * An order was answered: show its result and take the maps it left.
     *
     * @param {import("./api.js").Answer} answer The answer.
     */
    answered(answer) {
        const body = answer.body;
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
        }
        if (!body.live || !body.pending) {
            return; // refused before the instrument was asked
        }
        if (answer.ok) {
            this.edited = false;
        }
        this.status.setPinMap(body.live);
        this.take(body.live, body.pending);
        this.showResult(body.result, body.check ?? null);
    }

    /**
     * Show the last change's result, and the check that followed it.
     *
     * @param {Record<string, any>} result `SYSTem:PINS:RESult?`, as JSON.
     * @param {Record<string, any> | null} [check] The null-path check, if one ran now.
     */
    showResult(result, check = null) {
        const bad = result.result === "reverted" || result.result === "refused";
        let text = resultText(result);
        if (check !== null) {
            const errors = Array.isArray(check.errors) ? check.errors.map((/** @type {any} */ e) => `${e.code} ${e.text}`) : [];
            text += check.passed
                ? " The null-path check passed: the relays are verified."
                : ` The null-path check failed, the relays are unverified: ${[...errors, check.detail].filter(Boolean).join("; ")}.`;
        }
        this.resultLine.textContent = text;
        this.resultLine.dataset.state = bad || (check !== null && !check.passed) ? "pending" : "done";
        fillPairs(this.resultList, [
            ["Result", result.result],
            ["Reason", result.reason],
            ["Manufacturer ID", result.mfg],
            ["Device ID", result.die],
            ["Self-test", result.tst],
            ["Saved", result.saved === 1 ? "yes" : "no"],
        ]);
    }
}

/**
 * A state's word beside a role's name: what the role already says is left out.
 *
 * @param {string} word The state's word.
 * @returns {string} The short word.
 */
function short(word) {
    return word.replace(" by the board", "").replace("usable with ", "");
}

/**
 * A list item per line.
 *
 * @param {readonly string[]} lines The lines.
 * @returns {HTMLLIElement[]} The items.
 */
function items(lines) {
    return lines.map((line) => {
        const item = document.createElement("li");
        item.textContent = line;
        return item;
    });
}
