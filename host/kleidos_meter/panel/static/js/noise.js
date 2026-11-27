// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file Noise reduction in the INA228, the Settings view's block: the
 * averaging count and the shunt conversion time of each profile, off by
 * default, with the sample period they would give shown before anything is
 * written.
 *
 * It changes the measurement, unlike the charts' smoothing, so it is the
 * instrument's setting and serve puts it back on the way out. The instrument
 * keeps one for each profile and answers the live profile's alone: what the
 * page knows of another is what it read while that one was live, dated, or
 * nothing ("not read"). Set makes the profile live first, and turns an
 * averaging that is on off before it writes other figures, so that no figure
 * is judged beside the other's old one: what the instrument then refuses
 * leaves the profile off, with its reason beside the control.
 *
 * Each profile is a `<article data-noise="STATe">` of the page, so a test
 * holds the page to `/api/schema`; the choices inside are built here.
 */

import { get, post } from "./api.js";
import { averagedPeriod, formatDuration, noiseOf } from "./chart-math.js";
import { need, showRefusal } from "./dom.js";

/**
 * A profile as `/api/schema` declares it, what this block reads of it.
 *
 * @typedef {object} Profile
 * @property {string} name As the profile setting spells it (`STATe`).
 * @property {number} period_us Its own sample period.
 * @property {number} count Its own averaging count.
 * @property {number} conversion_us Its own shunt conversion time.
 * @property {number} other_us What the other inputs it converts add to a conversion.
 */

/**
 * One profile's card: its controls, and what the page last read of it.
 *
 * @typedef {object} Card
 * @property {Profile} profile The profile.
 * @property {HTMLElement} root Its element.
 * @property {HTMLSelectElement} state On or off.
 * @property {HTMLSelectElement} count The count.
 * @property {HTMLSelectElement} time The conversion time, seconds.
 * @property {HTMLButtonElement} button Set.
 * @property {HTMLElement} current What the page last read.
 * @property {HTMLElement} period What Set would give.
 * @property {HTMLElement} reason Why Set is disabled.
 * @property {HTMLElement} refusal What was refused.
 * @property {boolean} writing Whether its Set is being written: its choices wait for the answer.
 * @property {number} edits How many choices a person made in it: an answer asked for before one leaves it.
 * @property {{noise: import("./chart-math.js").Noise, at: string} | null} read The reading, dated.
 */

/** The per-profile settings, by what the card's control is for. */
export const FIELDS = Object.freeze({ state: "average", count: "average_count", time: "average_aperture" });

/** @returns {string} The time now, as a notice writes it. */
function now() {
    return new Date().toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * A conversion in a few words.
 *
 * @param {number} count The averaging count.
 * @param {number} conversionUs The shunt conversion time, microseconds.
 * @param {number} periodUs The sample's period, microseconds.
 * @returns {string} For example `AVG 16 × 1052 µs, a sample every 18432 µs (18.4 ms)`.
 */
export function conversionText(count, conversionUs, periodUs) {
    const micros = `${periodUs} µs`;
    const short = formatDuration(periodUs);
    return `AVG ${count} × ${conversionUs} µs, a sample every ${micros}${short === micros ? "" : ` (${short})`}`;
}

/**
 * Whether the instrument's answer names this profile: it spells them in capitals.
 *
 * @param {Profile} profile The profile.
 * @param {unknown} name The instrument's answer (`STATE`).
 * @returns {boolean} Whether they are the same.
 */
function same(profile, name) {
    return profile.name.toUpperCase() === String(name).toUpperCase();
}

export class NoiseReduction {
    /**
     * @param {HTMLElement} root The block's section.
     * @param {Record<string, any>} schema `/api/schema`.
     * @param {import("./session.js").Session} session The session's state.
     */
    constructor(root, schema, session) {
        this.session = session;
        /** @type {string[]} What the schema names that the page has no control for. */
        this.missing = [];
        /** @type {Card[]} */
        this.cards = [];
        /** @type {string} The live profile, as last read. */
        this.live = "";
        this.refusal = need(root, "#noise-refusal", HTMLElement);
        const named = (/** @type {string} */ name) =>
            (schema.settings ?? []).find((/** @type {any} */ setting) => setting.name === name);
        this.flags = String(named(FIELDS.state)?.flags ?? "");
        const counts = /** @type {string[]} */ (named(FIELDS.count)?.choices ?? []);
        const times = /** @type {string[]} */ (named(FIELDS.time)?.choices ?? []);
        for (const profile of /** @type {Profile[]} */ (schema.profiles ?? [])) {
            const card = root.querySelector(`[data-noise="${profile.name}"]`);
            if (!(card instanceof HTMLElement)) {
                this.missing.push(`${FIELDS.state} of ${profile.name}`);
                continue;
            }
            this.cards.push(this.build(card, profile, counts, times));
        }
        session.addEventListener("change", () => this.lock());
        this.lock();
    }

    /**
     * Fill one profile's controls, with its own conversion chosen and off.
     *
     * @param {HTMLElement} root The card.
     * @param {Profile} profile The profile.
     * @param {string[]} counts The counts the instrument takes.
     * @param {string[]} times The conversion times it takes, seconds.
     * @returns {Card} The card.
     */
    build(root, profile, counts, times) {
        const field = (/** @type {string} */ name) =>
            need(root, `[data-noise-setting="${name}"]`, HTMLSelectElement);
        const fill = (/** @type {HTMLSelectElement} */ select, /** @type {string[][]} */ choices) => {
            for (const [value, text] of choices) {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = text;
                select.append(option);
            }
        };
        /** @type {Card} */
        const card = {
            profile,
            root,
            state: field(FIELDS.state),
            count: field(FIELDS.count),
            time: field(FIELDS.time),
            button: need(root, '[data-action="set"]', HTMLButtonElement),
            current: need(root, "[data-current]", HTMLElement),
            period: need(root, "[data-period]", HTMLElement),
            reason: need(root, "[data-reason]", HTMLElement),
            refusal: need(root, "[data-refusal]", HTMLElement),
            writing: false,
            edits: 0,
            read: null,
        };
        fill(card.state, [["OFF", "Off"], ["ON", "On"]]);
        fill(card.count, counts.map((count) => [count, count]));
        fill(card.time, times.map((time) => [time, `${Math.round(Number(time) * 1e6)} µs`]));
        this.choose(card, profile.count, profile.conversion_us);
        for (const select of [card.state, card.count, card.time]) {
            select.addEventListener("change", () => {
                card.edits += 1;
                this.showPeriod(card);
            });
        }
        card.button.addEventListener("click", () => void this.write(card));
        this.showPeriod(card);
        this.showCurrent(card);
        return card;
    }

    /**
     * Choose a count and a conversion time in a card's controls.
     *
     * @param {Card} card The card.
     * @param {number} count The count.
     * @param {number} conversionUs The time, microseconds.
     */
    choose(card, count, conversionUs) {
        card.count.value = String(count);
        for (const option of card.time.options) {
            option.selected = Math.round(Number(option.value) * 1e6) === conversionUs;
        }
    }

    /**
     * Say what the card's choices give, before they are written.
     *
     * @param {Card} card The card.
     */
    showPeriod(card) {
        const { profile } = card;
        const on = card.state.value === "ON";
        card.count.disabled = card.state.disabled || !on;
        card.time.disabled = card.state.disabled || !on;
        if (!on) {
            card.period.textContent =
                `Off gives the profile's own: ${conversionText(profile.count, profile.conversion_us, profile.period_us)}.`;
            return;
        }
        const count = Number(card.count.value);
        const time = Math.round(Number(card.time.value) * 1e6);
        const period = averagedPeriod(profile, count, time);
        card.period.textContent =
            `On gives ${conversionText(count, time, period)}, instead of ${formatDuration(profile.period_us)}.`;
    }

    /**
     * Write what the page knows of a profile's averaging.
     *
     * @param {Card} card The card.
     */
    showCurrent(card) {
        const { read, profile, current } = card;
        const live = this.live !== "" && same(profile, this.live);
        if (read === null) {
            current.textContent = "Off by default; not read: only the live profile's can be.";
            delete current.dataset.state;
            return;
        }
        const on = read.noise.state === "on";
        const { count, conversion_us: conversion, period_us: period } = read.noise;
        const when = live ? "the live profile" : `read at ${read.at}, when it was the live profile`;
        current.textContent = `${on ? "On" : "Off"}: ${conversionText(count, conversion, period)} (${when}).`;
        if (on) {
            current.dataset.state = "pending";
        } else {
            delete current.dataset.state;
        }
    }

    /** Disable every card's controls while the instrument would refuse them, saying why, or it is written. */
    lock() {
        const why = this.session.refusedBy(this.flags);
        for (const card of this.cards) {
            card.state.disabled = why !== null || card.writing;
            card.button.disabled = why !== null || card.writing;
            card.reason.textContent = why === null ? "" : `Disabled: ${why}.`;
            this.showPeriod(card);
        }
    }

    /**
     * Forget what was read of the profiles: the instrument rebooted or was
     * reset, or another controller had the line.
     */
    forget() {
        for (const card of this.cards) {
            card.read = null;
            this.showCurrent(card);
        }
    }

    /**
     * Read the live profile's averaging.
     *
     * @param {boolean} adopt Whether to put it into its card's controls too,
     *     unless a person has chosen something in it since.
     * @returns {Promise<boolean>} Whether it was read.
     */
    async refresh(adopt) {
        const edits = this.cards.map((card) => card.edits);
        const answer = await get(`/api/query?q=${encodeURIComponent("SENS:PROF:REP?")}`);
        showRefusal(this.refusal, answer.ok ? null : answer);
        if (!answer.ok) {
            return false;
        }
        const fields = answer.body.fields?.[0] ?? {};
        const noise = noiseOf(fields);
        if (noise === null) {
            showRefusal(this.refusal, "this image has no noise reduction: it comes with 0.18.0");
        }
        this.live = String(fields.profile ?? "");
        for (const [index, card] of this.cards.entries()) {
            if (noise !== null && same(card.profile, this.live)) {
                card.read = { noise, at: now() };
                if (adopt && card.edits === edits[index]) {
                    card.state.value = noise.state === "on" ? "ON" : "OFF";
                    this.choose(card, noise.count, noise.conversion_us);
                    this.showPeriod(card);
                }
            }
            this.showCurrent(card);
        }
        return true;
    }

    /**
     * Write a profile's choices and show what the instrument then answers.
     * Its controls wait meanwhile, so that the answer never replaces a choice
     * made after Set.
     *
     * @param {Card} card The card.
     */
    async write(card) {
        showRefusal(card.refusal, null);
        card.writing = true;
        this.lock();
        try {
            await this.send(card);
        } finally {
            card.writing = false;
            this.lock();
        }
    }

    /**
     * The changes of `write()`, in order: the first one refused ends them.
     *
     * @param {Card} card The card.
     */
    async send(card) {
        const on = card.state.value === "ON";
        const wanted = { average_count: card.count.value, average_aperture: card.time.value, average: true };
        if (!(await this.refresh(false))) {
            return;
        }
        const order = async (/** @type {Record<string, unknown>} */ change) => {
            const answer = await post("/api/settings", change);
            if (!answer.ok) {
                showRefusal(card.refusal, answer);
            }
            return answer.ok;
        };
        let taken = true;
        if (!same(card.profile, this.live)) {
            taken = (await order({ profile: card.profile.name })) && (await this.refresh(false));
        }
        if (taken && card.read?.noise.state === "on") {
            taken = await order({ average: false });
        }
        if (taken && on) {
            await order(wanted);
        }
        await this.refresh(true);
    }
}
