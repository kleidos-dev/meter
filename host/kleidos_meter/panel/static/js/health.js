// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The Calibration and health view: what the instrument is and which
 * build it runs against the local `firmware.elf`, why it last reset, its
 * stacks, its heap counts, its watchdog and its calibration
 * (`GET /api/health`), and on request a self-test with its bits decoded and
 * a scan of the bus (`POST /api/scpi`: both act on the device, so a GET
 * never sends them).
 *
 * The heap counts are `allocs_since_main` and `frees_since_main` when the
 * image reports them, from the first line of `app_main` on, and
 * `allocs_after_init` and `frees_after_init` on an image that predates them.
 * A healthy instrument reports 0 for both.
 */

import { get, postScpi } from "./api.js";
import { fillPairs, need, replyErrors, shown, showRefusal } from "./dom.js";

/** A stack that kept fewer free bytes than this is short (`*TST?` bit 256). */
export const STACK_FLOOR = 512;

/**
 * `*TST?`'s bits, as `docs/scpi.md` lists them.
 *
 * @type {readonly {bit: number, part: string}[]}
 */
export const SELF_TEST_BITS = Object.freeze([
    { bit: 1, part: "the INA228 did not come up" },
    { bit: 2, part: "the sampling engine" },
    { bit: 4, part: "the relay inputs" },
    { bit: 8, part: "the INA228's identity or SHUNT_CAL reads back otherwise than at bring-up" },
    { bit: 16, part: "the INA228's trim memory (MEMSTAT clear)" },
    { bit: 32, part: "the ALE wire (STATE's pacing probe counted other than 7 to 9 edges)" },
    { bit: 64, part: "a relay pad does not read back its level" },
    { bit: 128, part: "a stored calibration is not given back" },
    { bit: 256, part: "a task's stack kept fewer than 512 bytes free" },
]);

/**
 * What a `*TST?` answer says, part by part.
 *
 * @param {number} value The answer: 0 when every part passes.
 * @returns {{passed: boolean, failed: string[], unknown: number}} The verdict,
 *     each failed part, and any bit the list does not know.
 */
export function decodeSelfTest(value) {
    const failed = SELF_TEST_BITS.filter((entry) => (value & entry.bit) !== 0).map(
        (entry) => `${entry.bit}: ${entry.part}`,
    );
    const known = SELF_TEST_BITS.reduce((sum, entry) => sum | entry.bit, 0);
    return { passed: value === 0, failed, unknown: value & ~known };
}

/**
 * The heap counts to show: the window since `app_main` when the image has it.
 *
 * @param {Record<string, any>} heap `DIAGnostic:HEAP?`, as JSON.
 * @returns {{allocs: unknown, frees: unknown, window: string}} The two counts and the window they cover.
 */
export function heapCounts(heap) {
    if ("allocs_since_main" in heap) {
        return { allocs: heap.allocs_since_main, frees: heap.frees_since_main, window: "since app_main" };
    }
    return { allocs: heap.allocs_after_init, frees: heap.frees_after_init, window: "after init" };
}

/**
 * A status color: done when *ok*, pending otherwise, none when unknown.
 *
 * @param {boolean | null} ok The verdict.
 * @returns {"done" | "pending" | null} The color.
 */
function verdict(ok) {
    return ok === null ? null : ok ? "done" : "pending";
}

export class HealthView {
    /** @param {HTMLElement} root The view's section. */
    constructor(root) {
        this.root = root;
        this.refusal = need(root, "#health-refusal", HTMLElement);
        this.elf = need(root, "#health-elf", HTMLElement);
        this.lists = {
            identity: need(root, "#health-identity", HTMLElement),
            reset: need(root, "#health-reset", HTMLElement),
            stacks: need(root, "#health-stacks", HTMLElement),
            heap: need(root, "#health-heap", HTMLElement),
            watchdog: need(root, "#health-watchdog", HTMLElement),
            calibration: need(root, "#health-calibration", HTMLElement),
        };
        this.selfTestResult = need(root, "#health-selftest-result", HTMLElement);
        this.selfTestBits = need(root, "#health-selftest-bits", HTMLElement);
        this.scanResult = need(root, "#health-scan-result", HTMLElement);
        this.selfTestButton = need(root, "#health-selftest", HTMLButtonElement);
        this.scanButton = need(root, "#health-scan", HTMLButtonElement);
        need(root, "#health-refresh", HTMLButtonElement).addEventListener("click", () => void this.refresh());
        this.selfTestButton.addEventListener("click", () => void this.selfTest());
        this.scanButton.addEventListener("click", () => void this.scan());
    }

    /** The view is shown: read the reports. */
    shown() {
        void this.refresh();
    }

    /** Read `GET /api/health` and write every group. */
    async refresh() {
        showRefusal(this.refusal, null);
        const answer = await get("/api/health");
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return;
        }
        this.show(answer.body);
    }

    /**
     * Write the reports.
     *
     * @param {Record<string, any>} body `GET /api/health`.
     */
    show(body) {
        const info = body.info ?? {};
        const health = body.health ?? {};
        const heap = body.heap ?? {};
        const watchdog = body.watchdog ?? {};
        const cal = body.calibration ?? {};
        const local = body.local_elf ?? {};
        this.elf.textContent = local.detail ?? "";
        this.elf.dataset.state = local.matches === true ? "done" : "pending";
        fillPairs(this.lists.identity, [
            ["Serial", info.serial],
            ["Model", info.model],
            ["Firmware", info.fw],
            ["Image", info.image],
            ["ELF (instrument)", info.elf, verdict(local.matches ?? null)],
            ["ELF (local build)", local.elf, verdict(local.matches ?? null)],
            ["Chip", info.chip, verdict(info.chip === undefined ? null : info.chip === "ina228")],
        ]);
        const uptime = typeof health.uptime_us === "number" ? `${(health.uptime_us / 1e6).toFixed(1)} s` : health.uptime_us;
        const clean = ["poweron", "software", "external", "usb"].includes(String(health.reset));
        fillPairs(this.lists.reset, [
            ["Reset reason", health.reset, verdict(health.reset === undefined ? null : clean)],
            ["Uptime", uptime],
            ["Coils the reset released", health.reset_released, verdict(health.reset_released === null ? true : health.reset_released === undefined ? null : false)],
            ["Hold releases", health.hold_released],
            ["Errors queued", health.errors],
        ]);
        fillPairs(this.lists.stacks, ["stack_console", "stack_sampler"].map((key) => {
            const bytes = health[key];
            const ok = typeof bytes === "number" ? bytes >= STACK_FLOOR : null;
            return /** @type {[string, unknown, ("done" | "pending" | null)]} */ ([
                key === "stack_console" ? "Console, fewest free" : "Sampler, fewest free",
                typeof bytes === "number" ? `${bytes} B` : bytes,
                verdict(ok),
            ]);
        }));
        const counts = heapCounts(heap);
        fillPairs(this.lists.heap, [
            [`Allocations ${counts.window}`, counts.allocs, verdict(typeof counts.allocs === "number" ? counts.allocs === 0 : null)],
            [`Frees ${counts.window}`, counts.frees, verdict(typeof counts.frees === "number" ? counts.frees === 0 : null)],
            ["Before app_main", "pre_main_allocs" in heap ? `${shown(heap.pre_main_allocs)} allocations, ${shown(heap.pre_main_frees)} frees` : "not reported by this image"],
            ["Internal RAM free", typeof heap.free_internal === "number" ? `${heap.free_internal} B` : heap.free_internal],
            ["Its lowest", typeof heap.min_free_internal === "number" ? `${heap.min_free_internal} B` : heap.min_free_internal],
            ["Largest block", typeof heap.largest_internal === "number" ? `${heap.largest_internal} B` : heap.largest_internal],
        ]);
        fillPairs(this.lists.watchdog, [
            ["Console checked in", watchdog.console, verdict(watchdog.console === undefined ? null : watchdog.console === 1)],
            ["Sampler checked in", watchdog.sampler, verdict(watchdog.sampler === undefined ? null : watchdog.sampler === 1)],
            ["Timeout", typeof watchdog.timeout_s === "number" ? `${watchdog.timeout_s} s` : watchdog.timeout_s],
            ["A starved task resets", watchdog.panic],
            ["Stalls since boot", watchdog.stalls, verdict(typeof watchdog.stalls === "number" ? watchdog.stalls === 0 : null)],
        ]);
        fillPairs(this.lists.calibration, [
            ["Tag", cal.cal_tag],
            ["Date", cal.cal_date],
            ["Shunt measured", cal.shunt_measured, verdict(cal.shunt_measured === undefined ? null : cal.shunt_measured === 1)],
            ["Range 0 shunt", cal.shunt_uohm_r0 === undefined ? cal.shunt_uohm : `${shown(cal.shunt_uohm_r0)} µΩ`],
            ["Range 1 shunt", cal.shunt_uohm_r1 === undefined ? "-" : `${shown(cal.shunt_uohm_r1)} µΩ`],
            ["Nulled", `range 0: ${shown(cal.nulled_r0)}, range 1: ${shown(cal.nulled_r1)}`],
            ["Bus voltage side", cal.vbus_side],
        ]);
    }

    /**
     * Send one query that acts on the device, through `POST /api/scpi`.
     *
     * @param {string} query The query.
     * @returns {Promise<string | null>} Its answer, or null after showing its refusal.
     */
    async act(query) {
        showRefusal(this.refusal, null);
        const answer = await postScpi(query);
        return this.reply(answer);
    }

    /**
     * The one reply of a one-message `POST /api/scpi`, or null after its refusal is shown.
     *
     * @param {import("./api.js").Answer} answer The answer.
     * @returns {string | null} The first unit.
     */
    reply(answer) {
        if (!answer.ok) {
            showRefusal(this.refusal, answer);
            return null;
        }
        const reply = (answer.body.replies ?? [])[0] ?? {};
        const errors = replyErrors(reply);
        if (errors !== null) {
            showRefusal(this.refusal, errors);
            return null;
        }
        return String((reply.units ?? [])[0] ?? "");
    }

    /** Run `*TST?` and decode its bits. */
    async selfTest() {
        this.selfTestButton.disabled = true;
        try {
            const text = await this.act("*TST?");
            if (text === null) {
                return;
            }
            const value = Number.parseInt(text, 10);
            if (!Number.isInteger(value)) {
                this.selfTestResult.textContent = `*TST? answered ${text}`;
                return;
            }
            const decoded = decodeSelfTest(value);
            this.selfTestResult.textContent = decoded.passed ? "0: every part passes." : `${value}: ${decoded.failed.length} parts fail.`;
            this.selfTestResult.dataset.state = decoded.passed ? "done" : "pending";
            const lines = [...decoded.failed];
            if (decoded.unknown !== 0) {
                lines.push(`${decoded.unknown}: bits this panel does not know`);
            }
            this.selfTestBits.replaceChildren(...lines.map((line) => {
                const item = document.createElement("li");
                item.textContent = line;
                return item;
            }));
        } finally {
            this.selfTestButton.disabled = false;
        }
    }

    /** Scan the bus for the INA228. */
    async scan() {
        this.scanButton.disabled = true;
        try {
            const text = await this.act("DIAG:I2C:SCAN?");
            if (text === null) {
                return;
            }
            const fields = Object.fromEntries(text.split(/\s+/u).filter(Boolean).map((token) => token.split("=", 2)));
            const found = String(fields.found ?? "-");
            const ok = found.split(",").includes(String(fields.expected ?? "0x40"));
            this.scanResult.textContent = `expected ${shown(fields.expected)}, found ${found} (${shown(fields.n)} answered)`;
            this.scanResult.dataset.state = ok ? "done" : "pending";
        } finally {
            this.scanButton.disabled = false;
        }
    }
}
