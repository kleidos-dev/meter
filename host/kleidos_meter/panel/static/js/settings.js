// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Settings view: every setting the schema declares as the
 * instrument answers it, a control to write it, what the live pin map gives
 * each role (changed in the Pins view), and each range's shunt with the
 * registry's figure, its uncertainty and the verdict, and the override flow.
 *
 * Each setting is a `<div data-setting="name">` of the page, so a test
 * holds the page to `/api/schema`; the control inside it is built here from
 * the setting's kind. A setting is written alone (`POST /api/settings`), so
 * what the instrument refuses is shown beside it with its code. While the
 * setting's flags would refuse it (`refusedBy`) its control is disabled
 * and says why. The settings the instrument keeps for each profile are the
 * noise reduction's block (noise.js), not rows.
 *
 * An override needs its uncertainty: it is previewed first
 * (`GET /api/settings/shunt/preview`), written only from the preview after
 * a confirmation (`POST /api/settings/shunt` with `confirm: true`), and
 * while a range converts with one the view carries the 'not quotable'
 * banner until Revert to the registry (`POST /api/calibration/restore`).
 */

import { get, post } from "./api.js";
import { confirmAction } from "./dialogs.js";
import { fillPairs, need, shown, showRefusal } from "./dom.js";
import { NoiseReduction } from "./noise.js";
import { assignments, mapFrom } from "./pins-math.js";

/**
 * A setting as `/api/schema` declares it.
 *
 * @typedef {object} SettingInfo
 * @property {string} name Its key.
 * @property {string} label What the page calls it.
 * @property {string} flags Its setter's flag letters.
 * @property {string} kind `choice`, `integer`, `number`, `number_or_off`, `boolean` or `channels`.
 * @property {string[]} choices A choice's words.
 * @property {string} unit A number's unit.
 * @property {boolean} writable False for one the panel never writes.
 */

/**
 * The value a setting's control holds, as `POST /api/settings` takes it.
 *
 * @param {SettingInfo} setting The setting.
 * @param {HTMLElement} slot Its control's container.
 * @returns {{value: unknown} | {error: string}} The value, or why there is none.
 */
export function controlValue(setting, slot) {
    if (setting.kind === "channels") {
        const names = [...slot.querySelectorAll("input:checked")].map(
            (box) => /** @type {HTMLInputElement} */ (box).value,
        );
        return { value: names.length > 0 ? names : "DEFault" };
    }
    const control = slot.querySelector("select, input");
    if (!(control instanceof HTMLSelectElement || control instanceof HTMLInputElement)) {
        return { error: "no control" };
    }
    const text = control.value.trim();
    if (setting.kind === "boolean") {
        return { value: text === "ON" };
    }
    if (text === "") {
        return { error: "give a value" };
    }
    return { value: text };
}

/**
 * Build a setting's control from its kind.
 *
 * @param {SettingInfo} setting The setting.
 * @param {string} id The control's id, for its label.
 * @returns {HTMLElement} The control (a group of checkboxes for the channels).
 */
function control(setting, id) {
    if (setting.kind === "choice" || setting.kind === "boolean") {
        const select = document.createElement("select");
        select.id = id;
        const words = setting.kind === "boolean" ? ["ON", "OFF"] : setting.choices;
        for (const word of words) {
            const option = document.createElement("option");
            option.value = word;
            option.textContent = word;
            select.append(option);
        }
        return select;
    }
    if (setting.kind === "channels") {
        const box = document.createElement("div");
        box.className = "checks";
        box.id = id;
        for (const name of setting.choices) {
            const label = document.createElement("label");
            const input = document.createElement("input");
            input.type = "checkbox";
            input.value = name;
            label.append(input, ` ${name}`);
            box.append(label);
        }
        return box;
    }
    const input = document.createElement("input");
    input.id = id;
    input.type = "text";
    input.inputMode = setting.kind === "integer" ? "numeric" : "decimal";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = setting.kind === "number_or_off" ? `a number of ${setting.unit}, or OFF` : setting.unit;
    return input;
}

/**
 * Put the instrument's answer into a control, so Set writes it back unchanged.
 *
 * @param {SettingInfo} setting The setting.
 * @param {HTMLElement} slot Its control's container.
 * @param {string} answer The query's answer.
 */
function adopt(setting, slot, answer) {
    if (setting.kind === "channels") {
        const named = answer.replaceAll('"', "").split(",").map((item) => item.trim().toUpperCase());
        for (const box of slot.querySelectorAll("input")) {
            box.checked = named.some((item) => item !== "" && box.value.startsWith(item.slice(0, 3)));
        }
        return;
    }
    const element = slot.querySelector("select, input");
    if (element instanceof HTMLSelectElement) {
        const upper = answer.trim().toUpperCase();
        const word = setting.kind === "boolean"
            ? (upper === "1" || upper === "ON" ? "ON" : "OFF")
            : [...element.options].find((option) => {
                const value = option.value.toUpperCase();
                return value === upper || value.startsWith(upper);
            })?.value;
        if (word !== undefined) {
            element.value = word;
        }
    } else if (element instanceof HTMLInputElement) {
        element.value = answer.trim();
    }
}

export class SettingsView {
    /**
     * @param {HTMLElement} root The view's section.
     * @param {Record<string, any>} schema `/api/schema`.
     * @param {import("./session.js").Session} session The session's state.
     * @param {import("./status.js").StatusBar} status The status bar, told when the figures are not quotable.
     */
    constructor(root, schema, session, status) {
        this.root = root;
        this.session = session;
        this.status = status;
        /** @type {SettingInfo[]} */
        this.settings = (schema.settings ?? []).filter((/** @type {any} */ setting) => !setting.per_profile);
        this.noise = new NoiseReduction(need(root, "#noise", HTMLElement), schema, session);
        /** @type {Map<string, HTMLElement>} */
        this.rows = new Map();
        /** @type {string[]} Settings the schema names that the page has no row for. */
        this.missing = [...this.noise.missing];
        this.refusal = need(root, "#settings-refusal", HTMLElement);
        this.pinsList = need(root, "#settings-pins-list", HTMLElement);
        this.pinsRefusal = need(root, "#settings-pins-refusal", HTMLElement);
        this.banner = need(root, "#shunt-banner", HTMLElement);
        this.verdict = need(root, "#shunt-verdict", HTMLElement);
        this.shuntBody = need(root, "#shunt-body", HTMLElement);
        this.overrideForm = need(root, "#override-form", HTMLFormElement);
        this.writeButton = need(root, "#override-write", HTMLButtonElement);
        this.revertButton = need(root, "#override-revert", HTMLButtonElement);
        this.overrideRefusal = need(root, "#override-refusal", HTMLElement);
        this.previewList = need(root, "#override-preview-list", HTMLElement);
        /** @type {Record<string, unknown> | null} The override the last preview showed. */
        this.previewed = null;
        this.build();
        need(root, "#settings-refresh", HTMLButtonElement).addEventListener("click", () => void this.refresh());
        this.overrideForm.addEventListener("submit", (event) => {
            event.preventDefault();
            void this.preview();
        });
        this.overrideForm.addEventListener("input", () => this.forgetPreview());
        this.writeButton.addEventListener("click", () => void this.writeOverride());
        this.revertButton.addEventListener("click", () => void this.revert());
        session.addEventListener("change", () => this.lockControls());
    }

    /** Build each row's control and wire its Set button. */
    build() {
        for (const setting of this.settings) {
            const row = this.root.querySelector(`[data-setting="${setting.name}"]`);
            if (!(row instanceof HTMLElement)) {
                this.missing.push(setting.name);
                continue;
            }
            this.rows.set(setting.name, row);
            const slot = need(row, "[data-input]", HTMLElement);
            const id = `setting-${setting.name.replaceAll("_", "-")}`;
            if (setting.writable) {
                const built = control(setting, id);
                slot.append(built);
                const label = row.querySelector(".setting-label");
                if (label !== null && !(built instanceof HTMLDivElement)) {
                    built.setAttribute("aria-labelledby", label.id);
                } else if (label !== null) {
                    built.setAttribute("role", "group");
                    built.setAttribute("aria-labelledby", label.id);
                }
                need(row, '[data-action="set"]', HTMLButtonElement).addEventListener(
                    "click",
                    () => void this.write(setting, row),
                );
            }
        }
        this.lockControls();
    }

    /** Disable every control whose flags would be refused now, saying why. */
    lockControls() {
        for (const setting of this.settings) {
            const row = this.rows.get(setting.name);
            if (row === undefined || !setting.writable) {
                continue;
            }
            const why = this.session.refusedBy(setting.flags);
            for (const node of row.querySelectorAll("select, input, button")) {
                /** @type {HTMLButtonElement | HTMLInputElement | HTMLSelectElement} */ (node).disabled = why !== null;
            }
            need(row, "[data-reason]", HTMLElement).textContent = why === null ? "" : `Disabled: ${why}.`;
            row.dataset.refused = why === null ? "" : "1";
        }
        const busy = this.session.known.held !== null || this.session.known.sequence !== null;
        this.revertButton.disabled = busy;
        this.writeButton.disabled = busy || this.previewed === null;
    }

    /** The view is shown: read every setting, the pin map and the shunt. */
    shown() {
        void this.refresh();
    }

    /**
     * Messages that change settings were written: the noise reduction shown
     * is read again, or forgotten when a reset changed every one.
     *
     * @param {string[]} changed The settings, by name.
     * @param {boolean} shownNow Whether the view is shown; a hidden one reads nothing.
     */
    changed(changed, shownNow) {
        if (this.settings.every((setting) => changed.includes(setting.name))) {
            this.noise.forget();
        }
        if (shownNow) {
            void this.noise.refresh(false);
        }
    }

    /** What was read of the profiles' noise reduction may no longer hold. */
    forgetNoise() {
        this.noise.forget();
    }

    /** Read what the live pin map gives each role. */
    async refreshPins() {
        const answer = await get("/api/pins");
        this.status.setPins(answer);
        showRefusal(this.pinsRefusal, answer.ok ? null : answer);
        if (answer.ok) {
            fillPairs(this.pinsList, assignments(mapFrom(answer.body.live), answer.body.board));
        }
    }

    /** Read every setting, the pin map and the shunt again. */
    async refresh() {
        void this.refreshPins();
        void this.noise.refresh(true);
        showRefusal(this.refusal, null);
        const answer = await get("/api/settings");
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return;
        }
        this.showSettings(answer.body.settings ?? {}, true);
        this.showShunt(answer.body.shunt ?? null);
    }

    /**
     * Write the instrument's answers into the rows.
     *
     * @param {Record<string, string>} answers By setting name.
     * @param {boolean} adoptAll Whether to put them into the controls too.
     */
    showSettings(answers, adoptAll) {
        for (const setting of this.settings) {
            const row = this.rows.get(setting.name);
            const answer = answers[setting.name];
            if (row === undefined || answer === undefined) {
                continue;
            }
            const unit = setting.unit && /^[+-]?[\d.]/u.test(answer) ? ` ${setting.unit}` : "";
            need(row, "[data-current]", HTMLElement).textContent = `${answer}${unit}`;
            if (adoptAll && setting.writable) {
                adopt(setting, need(row, "[data-input]", HTMLElement), answer);
            }
        }
    }

    /**
     * Write one setting, then show what the instrument reads back.
     *
     * @param {SettingInfo} setting The setting.
     * @param {HTMLElement} row Its row.
     */
    async write(setting, row) {
        const where = need(row, "[data-refusal]", HTMLElement);
        showRefusal(where, null);
        const value = controlValue(setting, need(row, "[data-input]", HTMLElement));
        if ("error" in value) {
            showRefusal(where, value.error);
            return;
        }
        const answer = await post("/api/settings", { [setting.name]: value.value });
        const read = answer.body.settings;
        if (read && typeof read === "object") {
            this.showSettings(read, true);
        }
        if (!answer.ok) {
            showRefusal(where, answer);
        }
    }

    /**
     * Show each range's shunt, the verdict and, under an override, the banner.
     *
     * @param {Record<string, any> | null} shunt `GET /api/settings`'s `shunt`.
     */
    showShunt(shunt) {
        this.shuntBody.replaceChildren();
        if (shunt === null) {
            this.verdict.textContent = "This serve shows no shunt.";
            return;
        }
        const verdict = shunt.verdict ?? {};
        const problem = shunt.registry_problem ? ` (${shunt.registry_problem})` : "";
        this.verdict.textContent = `Registry verdict: ${shown(verdict.status)}${problem}. ${shunt.quotable ? "The figures are quotable." : "The figures are not quotable."}`;
        this.verdict.dataset.state = shunt.quotable ? "done" : "pending";
        this.banner.hidden = !shunt.banner;
        this.banner.textContent = shunt.banner ?? "";
        if (shunt.verdict) {
            this.status.setVerdict(shunt.verdict);
        }
        this.status.setQuotable(Boolean(shunt.quotable), shunt.banner ?? null);
        for (const range of shunt.ranges ?? []) {
            const row = document.createElement("tr");
            const head = document.createElement("th");
            head.scope = "row";
            head.textContent = String(range.range);
            const figures = range.uncertainty;
            const registry = range.registry;
            const cells = [
                `${shown(range.device_uohm)} µΩ`,
                range.source,
                figures === null ? "none stated" : `${figures.expanded_ppm} ppm, ±${figures.expanded_uohm} µΩ`,
                registry === null ? "none" : `${registry.value_uohm} µΩ, U ${registry.expanded_ppm} ppm (${registry.event}, ${registry.record})`,
                range.deviation_ppm === null ? "-" : `${range.deviation_ppm} ppm`,
            ];
            row.append(head);
            for (const text of cells) {
                const cell = document.createElement("td");
                cell.textContent = shown(text);
                row.append(cell);
            }
            row.dataset.source = String(range.source);
            this.shuntBody.append(row);
        }
    }

    /**
     * The override the form describes, as the preview and the write take it.
     *
     * @returns {Record<string, unknown> | string} The order, or why the form is not one.
     */
    overrideOrder() {
        const form = new FormData(this.overrideForm);
        const value = Number(form.get("uohm"));
        const amount = Number(form.get("amount"));
        if (!Number.isFinite(value) || value <= 0) {
            return "the value is a number of µΩ above 0";
        }
        if (!Number.isFinite(amount) || amount <= 0) {
            return "an override needs its uncertainty: a number above 0";
        }
        /** @type {Record<string, unknown>} */
        const order = { range: Number(form.get("range")), uohm: value, [String(form.get("kind"))]: amount };
        const note = String(form.get("note") ?? "").trim();
        if (note !== "") {
            order.note = note;
        }
        return order;
    }

    /** The form changed: the preview no longer describes it. */
    forgetPreview() {
        this.previewed = null;
        this.writeButton.disabled = true;
        this.previewList.hidden = true;
    }

    /** Preview the override: what it would write, against the registry's figure. */
    async preview() {
        showRefusal(this.overrideRefusal, null);
        const order = this.overrideOrder();
        if (typeof order === "string") {
            showRefusal(this.overrideRefusal, order);
            return;
        }
        const query = new URLSearchParams(Object.entries(order).map(([key, value]) => [key, String(value)]));
        const answer = await get(`/api/settings/shunt/preview?${query}`);
        if (!answer.ok) {
            showRefusal(this.overrideRefusal, answer);
            return;
        }
        const body = answer.body;
        const figures = body.uncertainty ?? {};
        fillPairs(this.previewList, [
            ["Writes", body.unit],
            ["Value", `${figures.value_uohm} µΩ`],
            ["Standard uncertainty", `${figures.u_ppm} ppm`],
            ["Expanded, k = 2", `${figures.expanded_ppm} ppm, ±${figures.expanded_uohm} µΩ`],
            ["Now", `${body.device_uohm} µΩ`],
            ["Against the registry", body.deviation_ppm === null ? "no registry figure" : `${body.deviation_ppm} ppm`],
            ["Normalized error", body.normalized_error === null ? "-" : String(body.normalized_error), body.normalized_error !== null && body.normalized_error < 1 ? "done" : "pending"],
            ["Quotable", "no: " + String(body.banner ?? ""), "pending"],
        ]);
        this.previewList.hidden = false;
        this.previewed = order;
        this.lockControls();
    }

    /** Write the previewed override, after its confirmation. */
    async writeOverride() {
        const order = this.previewed;
        if (order === null) {
            return;
        }
        const go = await confirmAction({
            title: "Write the override",
            lines: [
                `Range ${order.range} converts with ${order.uohm} µΩ from now on, kept on this host under the instrument's serial.`,
                "Every figure the instrument gives on that range is not quotable until the registry's copy is written back.",
            ],
            go: "Write it",
        });
        if (!go) {
            showRefusal(this.overrideRefusal, "not written: the override was not confirmed");
            return;
        }
        const answer = await post("/api/settings/shunt", { ...order, confirm: true });
        if (!answer.ok) {
            showRefusal(this.overrideRefusal, answer);
            return;
        }
        this.forgetPreview();
        this.showShunt(answer.body.shunt ?? null);
    }

    /** Write the registry's working copy back, which removes the host's overrides. */
    async revert() {
        showRefusal(this.overrideRefusal, null);
        const go = await confirmAction({
            title: "Revert to the registry",
            lines: [
                "The registry's working copy is written to the instrument where it differs, then verified.",
                "The overrides this host keeps for the instrument are removed once it holds the registry's copy.",
            ],
            go: "Revert",
        });
        if (!go) {
            return;
        }
        const answer = await post("/api/calibration/restore");
        const said = Array.isArray(answer.body.said) ? answer.body.said.join(" ") : "";
        if (answer.ok) {
            this.verdict.textContent = `After the revert: ${shown(answer.body.after?.status)}. ${said}`;
        } else if (answer.body.after) {
            showRefusal(
                this.overrideRefusal,
                `${answer.status} · the instrument holds ${shown(answer.body.after.status)} after the revert · ${said}`,
            );
        } else {
            showRefusal(this.overrideRefusal, answer);
        }
        await this.refresh();
    }
}
