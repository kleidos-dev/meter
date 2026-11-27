// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The pin map's rules and words, with no DOM: a map read from a
 * report, the rules it must keep, what a pin of the board is under a map,
 * and what differs between two maps.
 *
 * The board is `GET /api/pins`'s `board`, the package's pin table. The rules
 * are `pinmap::validate()` (firmware/src/pinmap.h) in JavaScript: the browser
 * test runs every line of firmware/test/native/golden/pinmap.vectors through
 * {@link validateMap} in the page and compares it with the firmware's verdict.
 */

/**
 * A pin map, keyed as `SYSTem:PINS?` names its fields.
 *
 * @typedef {object} PinMap
 * @property {number | null} sda The INA228's SDA pad, or null.
 * @property {number | null} scl Its SCL pad.
 * @property {number | null} alert Its ALERT pad.
 * @property {"auto" | number} addr Its address; `auto` is a scan.
 * @property {number} scl_hz The bus clock at boot, Hz; NaN for no number.
 * @property {string} pull_sda SDA's internal pull, `up` or `off`.
 * @property {string} pull_scl SCL's.
 * @property {string} pull_alert ALERT's.
 * @property {number | null} relay1 Relay 1's pad, or null.
 * @property {string} relay1_active The level that energizes it, `high` or `low`.
 * @property {number | null} relay2 Relay 2's pad, or null.
 * @property {string} relay2_active The level that energizes it.
 */

/**
 * A role as the table declares it.
 *
 * @typedef {object} Role
 * @property {RoleKey} key Its key in the map.
 * @property {string} label What the page calls it.
 * @property {number} below The first GPIO it cannot take.
 * @property {string} needs What it needs of a pin.
 */

/**
 * A terminal of the board (or a GPIO on no rail).
 *
 * @typedef {object} Row
 * @property {string} rail `left`, `right` or `offboard`.
 * @property {number} terminal 1 to 22; 0 off the rails.
 * @property {string} silk What is printed beside it.
 * @property {number | null} gpio Its GPIO, or null.
 * @property {"free" | "caution" | "reserved"} class What it may be used for.
 * @property {string} group Why, a key of the board's `groups`.
 * @property {string} hover What it is, in a sentence.
 */

/**
 * The board's pin table, `GET /api/pins`'s `board`.
 *
 * @typedef {object} Board
 * @property {{name: string, gpio_count: number}} board The board.
 * @property {{address_first: number, address_last: number, clock_min_hz: number, clock_max_hz: number}} bus Its ranges.
 * @property {Role[]} roles The five roles, in the rules' order.
 * @property {Record<string, {sentence: string, source: string}>} groups Each group's reason.
 * @property {Row[]} rows Every terminal and every GPIO on none.
 */

/** @typedef {"sda" | "scl" | "alert" | "relay1" | "relay2"} RoleKey */
/** @typedef {RoleKey | "addr" | "scl_hz"} Where */

/**
 * A rule a map breaks.
 *
 * @typedef {object} Problem
 * @property {"missing" | "no_pin" | "reserved" | "too_high" | "shared" | "address" | "clock"} problem Which.
 * @property {Where} at The field it is at.
 */

/** A map's keys, in `SYSTem:PINS?`'s order. */
export const KEYS = Object.freeze(/** @type {const} */ ([
    "sda",
    "scl",
    "alert",
    "addr",
    "scl_hz",
    "pull_sda",
    "pull_scl",
    "pull_alert",
    "relay1",
    "relay1_active",
    "relay2",
    "relay2_active",
]));

/** The keys that hold a GPIO or none. */
const GPIO_KEYS = new Set(["sda", "scl", "alert", "relay1", "relay2"]);

/** A group's word in a pin's name; a free pin has none. */
const GROUP_WORDS = Object.freeze({
    none: "",
    power: "power",
    ground: "ground",
    reset: "reset",
    strapping: "strapping",
    memory: "flash or PSRAM",
    usb: "USB",
    uart0: "console UART",
    led: "status LED",
    jtag: "JTAG",
    vdd_spi: "VDD_SPI",
});

/** What each state is called, in a pin's name and in the legend. */
export const STATE_WORDS = Object.freeze({
    free: "free",
    used: "in use",
    reserved: "reserved by the board",
    caution: "usable with caution",
});

/**
 * A map from a report's fields, as JSON or as their words (`none`, `auto`, `0x41`, `"7"`).
 *
 * @param {Record<string, unknown>} fields At least the twelve keys of {@link KEYS}.
 * @returns {PinMap} The map.
 */
export function mapFrom(fields) {
    /** @type {Record<string, unknown>} */
    const map = {};
    for (const key of KEYS) {
        const value = fields[key];
        if (GPIO_KEYS.has(key)) {
            map[key] = value === null || value === "none" ? null : Number(value);
        } else if (key === "addr") {
            map[key] = value === "auto" ? "auto" : Number(value);
        } else {
            map[key] = key === "scl_hz" ? Number(value) : String(value);
        }
    }
    return /** @type {PinMap} */ (/** @type {unknown} */ (map));
}

/**
 * Why a role cannot take a GPIO whatever else the map says, or null when it can.
 *
 * @param {Role} role The role.
 * @param {number} gpio The GPIO.
 * @param {Board} board The table.
 * @returns {"no_pin" | "reserved" | "too_high" | null} The rule it breaks.
 */
export function roleProblem(role, gpio, board) {
    const row = board.rows.find((candidate) => candidate.gpio === gpio);
    if (row === undefined) {
        return "no_pin";
    }
    if (row.class === "reserved") {
        return "reserved";
    }
    return gpio >= role.below ? "too_high" : null;
}

/**
 * Every rule a map breaks: at most one per role, in the table's order, then
 * the address and the clock.
 *
 * @param {PinMap} map The map.
 * @param {Board} board The table.
 * @returns {Problem[]} The problems; the first is the firmware's.
 */
export function problemsOf(map, board) {
    /** @type {Problem[]} */
    const found = [];
    board.roles.forEach((role, index) => {
        const gpio = map[role.key];
        if (gpio === null) {
            found.push({ problem: "missing", at: role.key });
            return;
        }
        const why = roleProblem(role, gpio, board);
        if (why !== null) {
            found.push({ problem: why, at: role.key });
        } else if (board.roles.slice(0, index).some((earlier) => map[earlier.key] === gpio)) {
            found.push({ problem: "shared", at: role.key });
        }
    });
    const { address_first: first, address_last: last, clock_min_hz: slowest, clock_max_hz: fastest } = board.bus;
    if (map.addr !== "auto" && !(map.addr >= first && map.addr <= last)) {
        found.push({ problem: "address", at: "addr" });
    }
    if (!(map.scl_hz >= slowest && map.scl_hz <= fastest)) {
        found.push({ problem: "clock", at: "scl_hz" });
    }
    return found;
}

/**
 * The first rule a map breaks, as `pinmap::validate()` finds it.
 *
 * @param {PinMap} map The map.
 * @param {Board} board The table.
 * @returns {Problem | null} The problem, or null for a valid map.
 */
export function validateMap(map, board) {
    return problemsOf(map, board)[0] ?? null;
}

/**
 * The roles a map gives a GPIO.
 *
 * @param {PinMap} map The map.
 * @param {number | null} gpio The GPIO; none of a terminal that has none.
 * @param {Board} board The table.
 * @returns {Role[]} The roles, in the table's order.
 */
export function rolesAt(map, gpio, board) {
    return gpio === null ? [] : board.roles.filter((role) => map[role.key] === gpio);
}

/**
 * A map with a pin's use changed: the role moved to it from wherever it was,
 * and every other role the pin had left with none; `null` leaves the pin unused.
 *
 * @param {PinMap} map The map.
 * @param {number} gpio The pin's GPIO.
 * @param {RoleKey | null} use The role, or null.
 * @param {Board} board The table.
 * @returns {PinMap} Another map.
 */
export function withUse(map, gpio, use, board) {
    const next = { ...map };
    for (const role of rolesAt(map, gpio, board)) {
        next[role.key] = null;
    }
    if (use !== null) {
        next[use] = gpio;
    }
    return next;
}

/**
 * The label of a role, as the table has it.
 *
 * @param {string} key The role's key.
 * @param {Board} board The table.
 * @returns {string} Its label.
 */
function label(key, board) {
    return board.roles.find((role) => role.key === key)?.label ?? key;
}

/**
 * Why a role cannot take a pin, in a sentence.
 *
 * @param {Role} role The role.
 * @param {number} gpio The GPIO.
 * @param {"no_pin" | "reserved" | "too_high"} why What {@link roleProblem} found.
 * @param {Board} board The table.
 * @returns {string} The sentence.
 */
export function roleReason(role, gpio, why, board) {
    if (why === "no_pin") {
        return `the chip has no GPIO${gpio}`;
    }
    if (why === "too_high") {
        return `needs a pin below GPIO${role.below}`;
    }
    const group = board.rows.find((row) => row.gpio === gpio)?.group ?? "";
    return `GPIO${gpio} is reserved by the board. ${board.groups[group]?.sentence ?? ""}`.trim();
}

/**
 * A problem in a sentence that names the role and the pin.
 *
 * @param {Problem} problem The problem.
 * @param {PinMap} map The map it was found in.
 * @param {Board} board The table.
 * @returns {string} The sentence.
 */
export function problemText(problem, map, board) {
    const { problem: kind, at } = problem;
    if (at === "addr") {
        const { address_first: first, address_last: last } = board.bus;
        return `The INA228's address is auto or ${addressText(first)} to ${addressText(last)}.`;
    }
    if (at === "scl_hz") {
        return `The I2C clock is ${board.bus.clock_min_hz} to ${board.bus.clock_max_hz} Hz.`;
    }
    const gpio = map[at];
    const role = /** @type {Role} */ (board.roles.find((candidate) => candidate.key === at));
    if (kind === "missing" || gpio === null) {
        return `${role.label} has no pin.`;
    }
    if (kind === "shared") {
        const first = rolesAt(map, gpio, board)[0];
        return `${role.label} is on GPIO${gpio}, which ${first.label} holds.`;
    }
    return `${role.label}: ${roleReason(role, gpio, /** @type {"reserved"} */ (kind), board)}`;
}

/**
 * An address as the instrument writes it.
 *
 * @param {"auto" | number} address The address.
 * @returns {string} `auto`, or `0x` and two hex digits.
 */
export function addressText(address) {
    return address === "auto" ? "auto" : `0x${address.toString(16).toUpperCase().padStart(2, "0")}`;
}

/**
 * A GPIO as the page writes it.
 *
 * @param {number | null} gpio The GPIO.
 * @returns {string} `GPIO7`, or `no pin`.
 */
export function gpioText(gpio) {
    return gpio === null ? "no pin" : `GPIO${gpio}`;
}

/**
 * What a terminal is under a map.
 *
 * @typedef {object} PinState
 * @property {keyof typeof STATE_WORDS} state Its state: a role's pin is `used`, whatever its class.
 * @property {string} what The roles it has, or its group's word; may be empty.
 * @property {string} word The state's word, and the caution's of a pin in use.
 * @property {string[]} conflicts What is wrong at it, one sentence each.
 * @property {boolean} changed Whether the live map gives it another use.
 * @property {string} name Its accessible name: place, GPIO, what, word, conflicts.
 */

/**
 * What a terminal is under the draft map, against the live one.
 *
 * A role left with no pin is a conflict at the pin the live map gives it, so
 * what took its place shows what it cost.
 *
 * @param {Row} row The terminal.
 * @param {PinMap} draft The map on the page.
 * @param {PinMap} live The instrument's map.
 * @param {Board} board The table.
 * @returns {PinState} Its state.
 */
export function pinState(row, draft, live, board) {
    const roles = rolesAt(draft, row.gpio, board);
    const before = rolesAt(live, row.gpio, board);
    const state = roles.length > 0 ? "used" : row.class;
    const word = state === "used" && row.class === "caution" ? `${STATE_WORDS.used}, with caution` : STATE_WORDS[state];
    const what = roles.length > 0
        ? roles.map((role) => role.label).join(" and ")
        : (GROUP_WORDS[/** @type {keyof typeof GROUP_WORDS} */ (row.group)] ?? row.group);
    const conflicts = problemsOf(draft, board)
        .filter(({ problem, at }) => (problem === "missing" ? before : roles).some((role) => role.key === at))
        .map((problem) => problemText(problem, draft, board));
    const changed = roles.map((role) => role.key).join() !== before.map((role) => role.key).join();
    const place = row.gpio === null ? row.silk : `GPIO${row.gpio}`;
    const parts = [`${row.rail} ${row.terminal}`, place, what, word].filter((part) => part !== "");
    return {
        state,
        what,
        word,
        conflicts,
        changed,
        name: parts.join(", ") + conflicts.map((text) => `, conflict: ${text}`).join(""),
    };
}

/**
 * One field that differs between two maps.
 *
 * @typedef {object} Difference
 * @property {typeof KEYS[number]} key The field.
 * @property {string} label What the page calls it.
 * @property {string} from The live map's value, as the page writes it.
 * @property {string} to The draft's.
 */

/**
 * A field of a map as the page writes it.
 *
 * @param {PinMap} map The map.
 * @param {typeof KEYS[number]} key The field.
 * @returns {string} Its text.
 */
function fieldText(map, key) {
    const value = map[key];
    if (GPIO_KEYS.has(key)) {
        return gpioText(/** @type {number | null} */ (value));
    }
    if (key === "addr") {
        return addressText(map.addr);
    }
    if (key === "scl_hz") {
        return Number.isNaN(map.scl_hz) ? "no number" : `${map.scl_hz} Hz`;
    }
    return key.endsWith("_active") ? `active ${value}` : `pull ${value}`;
}

/**
 * What differs between the live map and the draft, in the report's order.
 *
 * @param {PinMap} live The instrument's map.
 * @param {PinMap} draft The map on the page.
 * @param {Board} board The table.
 * @returns {Difference[]} The differences.
 */
export function differences(live, draft, board) {
    return KEYS.filter((key) => !Object.is(live[key], draft[key])).map((key) => ({
        key,
        label: key === "addr"
            ? "INA228 address"
            : key === "scl_hz"
                ? "I2C clock at boot"
                : label(key.replace(/^pull_/u, "").replace(/_active$/u, ""), board),
        from: fieldText(live, key),
        to: fieldText(draft, key),
    }));
}

/**
 * What `POST /api/pins/stage` takes to make the draft of the live map.
 *
 * @param {PinMap} live The instrument's map.
 * @param {PinMap} draft The map on the page.
 * @param {Board} board The table.
 * @returns {Record<string, unknown>} `clear`, and each field that differs.
 */
export function stageOrder(live, draft, board) {
    const changed = differences(live, draft, board).map(({ key }) => [key, draft[key]]);
    return { clear: true, ...Object.fromEntries(changed) };
}

/**
 * Each role's pin and configuration, and the bus's, for a list.
 *
 * @param {PinMap} map The map.
 * @param {Board} board The table.
 * @returns {[string, string][]} Each label and its text.
 */
export function assignments(map, board) {
    /** @type {[string, string][]} */
    const pairs = board.roles.map((role) => {
        const option = role.key === "relay1" || role.key === "relay2"
            ? fieldText(map, /** @type {"relay1_active"} */ (`${role.key}_active`))
            : fieldText(map, /** @type {"pull_sda"} */ (`pull_${role.key}`));
        return [role.label, `${gpioText(map[role.key])}, ${option}`];
    });
    return [...pairs, ["INA228 address", addressText(map.addr)], ["I2C clock at boot", fieldText(map, "scl_hz")]];
}

/** What a proof that failed says, by `SYSTem:PINS:RESult?`'s `reason`. */
const REASONS = Object.freeze({
    no_chip: "the INA228 did not answer on the map's SDA, SCL and address",
    alert: "the INA228 answered, and its ALERT did not reach the map's ALERT pin",
    relays: "a relay pad did not read back the level it was given",
});

/** What a refusal says, by `reason`. */
const REFUSALS = Object.freeze({
    relays: "a relay is not at rest",
    invalid: "the map breaks a rule of the board",
});

/**
 * The last change's result in a sentence.
 *
 * @param {Record<string, unknown>} result `SYSTem:PINS:RESult?`, as JSON.
 * @returns {string} The sentence.
 */
export function resultText(result) {
    const reason = String(result.reason ?? "");
    /** @type {Record<string, string>} */
    const words = result.result === "refused" ? REFUSALS : REASONS;
    const why = words[reason] ?? (reason === "" ? "no reason given" : reason);
    switch (result.result) {
        case "none":
            return "No change was applied since boot.";
        case "ok":
            return reason === ""
                ? "Applied: the INA228 answered on the new map and the map is saved."
                : `Applied, though ${why}: the map is saved and the console is the way back.`;
        case "reverted":
            return `Reverted: ${why}. The last good map is back.`;
        case "refused":
            return `Refused: ${why}. Nothing changed.`;
        default:
            return `The instrument answered ${String(result.result)}: ${why}.`;
    }
}
