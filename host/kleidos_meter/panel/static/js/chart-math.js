// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The live chart's arithmetic, with no DOM access: the sample ring, each
 * pixel column's minimum, maximum, sum and sum of squares, the line the chart
 * draws (the arithmetic mean per column and per smoothing window), the
 * figures of the window in view, the time view (follow, zoom, pan), the axis
 * ticks, the frame gate, the formatting of quantities, and the files an
 * export of the view makes (its CSV, and the text chunks of its PNG).
 *
 * Everything here is a pure function or a class over typed arrays, so the
 * browser tests can check it through the page and nothing in it allocates
 * per sample: the ring is sized once, and the column buffers are reused from
 * one frame to the next.
 *
 * Values are kept as the live events carry them: integers in thousandths of
 * the record's unit (`i_na`, `v_uv`, `p_uw`, `die_mc`), times in the
 * instrument's microseconds. A missing value is NaN.
 *
 * Display smoothing is a centered moving average over whole columns, never a
 * filter of the recorded data: the line at a column is the mean of every
 * sample within half a window of it, so it is a figure that can be quoted
 * ("mean current over 100 ms"), it puts a peak at its own time, and its
 * memory is the window and nothing more (decision 11 of the 2026-11-24 plan).
 */

/**
 * One channel the chart can draw: its live-event column, the unit its axis is
 * in and the factor from the column's integer to that unit.
 *
 * @typedef {object} Channel
 * @property {string} key The live events' column (`i_na`).
 * @property {string} name The channel as `SENSe:FUNCtion:ON` names it (`CURR`).
 * @property {string} label What the page calls it.
 * @property {string} unit The SI unit of its axis (`A`).
 * @property {number} scale The column's integer times this is in `unit`.
 */

/** @type {readonly Channel[]} */
export const CHANNELS = Object.freeze([
    { key: "i_na", name: "CURR", label: "Current", unit: "A", scale: 1e-9 },
    { key: "v_uv", name: "VOLT", label: "Bus voltage", unit: "V", scale: 1e-6 },
    { key: "p_uw", name: "POW", label: "Power", unit: "W", scale: 1e-6 },
    { key: "die_mc", name: "TEMP", label: "Die temperature", unit: "°C", scale: 1e-3 },
]);

/**
 * The channel a column or a `SENSe:FUNCtion:ON` name stands for.
 *
 * @param {string} keyOrName `i_na` or `CURR`.
 * @returns {Channel | undefined} The channel, undefined when none is.
 */
export function channelOf(keyOrName) {
    return CHANNELS.find((c) => c.key === keyOrName || c.name === keyOrName);
}

/**
 * What one bucket of a run's decimation holds of one column.
 *
 * @typedef {object} BucketCell
 * @property {number} n How many samples it holds.
 * @property {number} mean Their arithmetic mean.
 * @property {number} rms Their root mean square.
 * @property {number} min Their lowest value.
 * @property {number} max Their highest.
 */

/**
 * The per-column arrays a ring keeps for its buckets, beside the mean it keeps
 * as the column's value.
 *
 * @typedef {object} BucketColumns
 * @property {Float64Array} n Each bucket's sample count, 0 when it holds none of the column.
 * @property {Float64Array} low Its minimum.
 * @property {Float64Array} high Its maximum.
 * @property {Float64Array} square Its mean square.
 */

/**
 * A ring of samples of fixed capacity: one time array and one value array per
 * channel, all typed and allocated once. The oldest sample is forgotten when
 * a new one arrives at capacity. Times are kept non-decreasing: a batch that
 * goes back in time (the instrument rebooted, a new capture) clears the ring
 * first.
 *
 * It holds either samples or a run's buckets (`pushBucket`), never both: a
 * bucket stands for the samples of its span by their count, mean, root mean
 * square, minimum and maximum, and a sample after buckets, or a bucket after
 * samples, starts the ring again.
 */
export class SampleRing {
    /**
     * @param {number} capacity The most samples it holds.
     * @param {readonly string[]} keys The value columns it keeps.
     * @param {number} [buckets] The most buckets it holds, at most `capacity`; 0, the default, takes none.
     */
    constructor(capacity, keys, buckets = 0) {
        if (!Number.isInteger(capacity) || capacity < 1) {
            throw new RangeError("a ring holds at least one sample");
        }
        if (!Number.isInteger(buckets) || buckets < 0 || buckets > capacity) {
            throw new RangeError("a ring holds 0 to its capacity of buckets");
        }
        /** The most samples it holds. */
        this.capacity = capacity;
        /** @type {Float64Array} */
        this.times = new Float64Array(capacity);
        /** @type {Map<string, Float64Array>} */
        this.values = new Map(keys.map((key) => [key, new Float64Array(capacity)]));
        /** Where the oldest sample is. */
        this.head = 0;
        /** How many samples it holds. */
        this.size = 0;
        /** Samples it forgot because it was full, since the last clear. */
        this.evicted = 0;
        /** The most buckets it holds. */
        this.bucketCapacity = buckets;
        /** Whether it holds buckets rather than samples. */
        this.bucketed = false;
        /** The widest bucket it holds, microseconds: the finest a run's view can be. */
        this.bucketUs = 0;
        /** Where each bucket ends (exclusive), microseconds. */
        this.ends = new Float64Array(buckets);
        /** @type {Map<string, BucketColumns>} What each bucket holds of each column. */
        this.cells = new Map(
            keys.map((key) => [
                key,
                {
                    n: new Float64Array(buckets),
                    low: new Float64Array(buckets),
                    high: new Float64Array(buckets),
                    square: new Float64Array(buckets),
                },
            ]),
        );
    }

    /** Forget every sample and bucket. */
    clear() {
        this.head = 0;
        this.size = 0;
        this.evicted = 0;
        this.bucketed = false;
        this.bucketUs = 0;
    }

    /**
     * Where the sample of logical index `i` (0 is the oldest) is stored.
     *
     * @param {number} i The logical index.
     * @returns {number} Its slot in the arrays.
     */
    slot(i) {
        const at = this.head + i;
        return at >= this.capacity ? at - this.capacity : at;
    }

    /**
     * The time of the sample of logical index `i`.
     *
     * @param {number} i The logical index, 0 to size - 1.
     * @returns {number} Its time, microseconds.
     */
    timeAt(i) {
        return this.times[this.slot(i)];
    }

    /**
     * A value of the sample of logical index `i`.
     *
     * @param {string} key The column.
     * @param {number} i The logical index.
     * @returns {number} The value, NaN when the column is not kept or the sample lacks it.
     */
    valueAt(key, i) {
        const column = this.values.get(key);
        return column === undefined ? Number.NaN : column[this.slot(i)];
    }

    /**
     * Append one sample.
     *
     * @param {number} t Its time, microseconds.
     * @param {(key: string) => number} value Its value for a column, NaN when it has none.
     */
    push(t, value) {
        if (this.bucketed || (this.size > 0 && t < this.timeAt(this.size - 1))) {
            this.clear();
        }
        let at;
        if (this.size < this.capacity) {
            at = this.slot(this.size);
            this.size += 1;
        } else {
            at = this.head;
            this.head = this.head + 1 === this.capacity ? 0 : this.head + 1;
            this.evicted += 1;
        }
        this.times[at] = t;
        for (const [key, column] of this.values) {
            column[at] = value(key);
        }
    }

    /**
     * Append one bucket of a run's decimation: its value is the mean of each
     * column, and the chart weighs it by its count.
     *
     * @param {number} t Where it starts, microseconds.
     * @param {number} end Where it ends, exclusive.
     * @param {(key: string) => BucketCell | null} cell What it holds of a column, null when nothing.
     */
    pushBucket(t, end, cell) {
        if (!this.bucketed || (this.size > 0 && t < this.timeAt(this.size - 1))) {
            this.clear();
            this.bucketed = true;
        }
        if (this.size >= this.bucketCapacity) {
            throw new RangeError(`the ring holds at most ${this.bucketCapacity} buckets`);
        }
        const at = this.size; // a ring of buckets never wraps: its head stays 0
        this.size += 1;
        this.times[at] = t;
        this.ends[at] = end;
        this.bucketUs = Math.max(this.bucketUs, end - t);
        for (const [key, column] of this.values) {
            const held = cell(key);
            const cells = /** @type {BucketColumns} */ (this.cells.get(key));
            column[at] = held === null ? Number.NaN : held.mean;
            cells.n[at] = held === null ? 0 : held.n;
            cells.low[at] = held === null ? Number.NaN : held.min;
            cells.high[at] = held === null ? Number.NaN : held.max;
            cells.square[at] = held === null ? Number.NaN : held.rms * held.rms;
        }
    }

    /**
     * Append a `samples` event's columns.
     *
     * @param {{count: number, t_us: number[], [column: string]: unknown}} batch The event's data.
     * @returns {number} The rows appended.
     */
    pushBatch(batch) {
        const times = batch.t_us;
        for (let row = 0; row < batch.count; row += 1) {
            this.push(times[row], (key) => {
                const column = batch[key];
                if (!Array.isArray(column)) {
                    return Number.NaN;
                }
                const cell = column[row];
                return typeof cell === "number" ? cell : Number.NaN;
            });
        }
        return batch.count;
    }

    /**
     * The logical index of the first sample at or after `t`, `size` when none is.
     *
     * @param {number} t A time, microseconds.
     * @returns {number} The index.
     */
    lowerBound(t) {
        let lo = 0;
        let hi = this.size;
        while (lo < hi) {
            const mid = (lo + hi) >>> 1;
            if (this.timeAt(mid) < t) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    /**
     * The logical index of the first sample after `t`, `size` when none is.
     *
     * @param {number} t A time, microseconds.
     * @returns {number} The index.
     */
    upperBound(t) {
        let lo = 0;
        let hi = this.size;
        while (lo < hi) {
            const mid = (lo + hi) >>> 1;
            if (this.timeAt(mid) <= t) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    /** @returns {number} The oldest time held, NaN when empty. */
    get first() {
        return this.size === 0 ? Number.NaN : this.timeAt(0);
    }

    /** @returns {number} The newest time held, NaN when empty. */
    get last() {
        return this.size === 0 ? Number.NaN : this.timeAt(this.size - 1);
    }
}

/**
 * A mark as the chart draws it.
 *
 * @typedef {object} MarkEntry
 * @property {number} t The mark's time, microseconds.
 * @property {string} label Its label.
 */

/** A bounded list of marks, the oldest forgotten first. */
export class MarkList {
    /** @param {number} capacity The most marks it keeps. */
    constructor(capacity) {
        /** The most marks it keeps. */
        this.capacity = capacity;
        /** @type {MarkEntry[]} */
        this.items = [];
    }

    /**
     * Keep a mark.
     *
     * @param {number} t Its time, microseconds.
     * @param {string} label Its label.
     */
    add(t, label) {
        this.items.push({ t, label });
        if (this.items.length > this.capacity) {
            this.items.splice(0, this.items.length - this.capacity);
        }
    }

    /** Forget every mark. */
    clear() {
        this.items.length = 0;
    }

    /**
     * The marks between two times, oldest first.
     *
     * @param {number} t0 The window's start.
     * @param {number} t1 Its end.
     * @returns {MarkEntry[]} The marks in it.
     */
    between(t0, t1) {
        return this.items.filter((mark) => mark.t >= t0 && mark.t <= t1);
    }
}

/**
 * Each pixel column's minimum, maximum, first and last value, sample count,
 * sum and sum of squares over a window, in buffers kept from one frame to the
 * next.
 *
 * The buffers can reach beyond the view by a margin of whole columns on each
 * side (half a smoothing window), so a moving average has no edge effect
 * inside the view: view column `c` is buffer column `c + offset`, and the
 * buffers hold `total` columns. A column's count is a weight: a sample counts
 * 1, and a run's bucket counts its samples, spread over the columns it covers
 * in proportion to how much of it each covers.
 */
export class ColumnExtrema {
    constructor() {
        /** How many columns the view holds. */
        this.columns = 0;
        /** The margin: the buffer column of view column 0. */
        this.offset = 0;
        /** How many columns the buffers hold: the view's and both margins. */
        this.total = 0;
        this.min = new Float64Array(0);
        this.max = new Float64Array(0);
        this.first = new Float64Array(0);
        this.last = new Float64Array(0);
        /** Each column's samples (a weight: a bucket's spread is fractional). */
        this.count = new Float64Array(0);
        /** Each column's sum of values, count-weighted. */
        this.sum = new Float64Array(0);
        /** Each column's sum of squared values, count-weighted. */
        this.square = new Float64Array(0);
    }

    /**
     * Make room for `total` columns, reusing the buffers when they are large enough.
     *
     * @param {number} total The columns of the buffers.
     */
    resize(total) {
        // Grown for a long window, they are given back once a frame needs a
        // quarter of them or less.
        if (total > this.min.length || (this.min.length > SHRINK_FLOOR && total * 4 < this.min.length)) {
            this.min = new Float64Array(total);
            this.max = new Float64Array(total);
            this.first = new Float64Array(total);
            this.last = new Float64Array(total);
            this.count = new Float64Array(total);
            this.sum = new Float64Array(total);
            this.square = new Float64Array(total);
        }
        this.total = total;
        this.count.fill(0, 0, total);
        this.sum.fill(0, 0, total);
        this.square.fill(0, 0, total);
    }

    /**
     * Take `weight` samples of mean `value` and mean square `square` into a column.
     *
     * @param {number} column The buffer column.
     * @param {number} weight How many samples.
     * @param {number} value Their mean.
     * @param {number} square Their mean square.
     * @param {number} low Their minimum.
     * @param {number} high Their maximum.
     */
    take(column, weight, value, square, low, high) {
        if (this.count[column] === 0) {
            this.min[column] = low;
            this.max[column] = high;
            this.first[column] = value;
        } else {
            if (low < this.min[column]) {
                this.min[column] = low;
            }
            if (high > this.max[column]) {
                this.max[column] = high;
            }
        }
        this.last[column] = value;
        this.count[column] += weight;
        this.sum[column] += weight * value;
        this.square[column] += weight * square;
    }

    /**
     * Reduce one column of the ring over `[t0, t1]`, widened by `margin`
     * columns on each side, to `columns` pixel columns and the margins
     * (reduceLanes() with one lane).
     *
     * @param {SampleRing} ring The samples, or a run's buckets.
     * @param {string} key The column.
     * @param {number} t0 The view's start, microseconds.
     * @param {number} t1 Its end.
     * @param {number} columns The view's pixel columns.
     * @param {number} [margin] Whole columns beyond each edge, 0 by default.
     * @returns {ColumnExtrema} This, filled.
     */
    reduce(ring, key, t0, t1, columns, margin = 0) {
        reduceLanes(ring, [key], t0, t1, columns, margin, [this]);
        return this;
    }

    /**
     * Spread a ring of buckets over the buffers: each bucket's samples go to
     * the columns its span covers, each column taking the share of its count
     * that it covers, and its minimum and maximum go to every one of them.
     *
     * @param {SampleRing} ring The buckets.
     * @param {string} key The column.
     * @param {number} lo The buffers' start, microseconds.
     * @param {number} hi Their end.
     * @param {number} perColumn Columns per microsecond.
     */
    reduceBuckets(ring, key, lo, hi, perColumn) {
        const cells = ring.cells.get(key);
        if (cells === undefined) {
            return;
        }
        const last = this.total - 1;
        const end = ring.lowerBound(hi);
        for (let i = Math.max(0, ring.lowerBound(lo) - 1); i < end; i += 1) {
            const at = ring.slot(i);
            const n = cells.n[at];
            const start = ring.times[at];
            const length = ring.ends[at] - start;
            const from = Math.max(start, lo);
            const to = Math.min(start + length, hi);
            if (!(n > 0) || !(length > 0) || to <= from) {
                continue;
            }
            const value = ring.valueAt(key, i);
            const first = Math.min(last, Math.floor((from - lo) * perColumn));
            const after = Math.min(last, Math.ceil((to - lo) * perColumn) - 1);
            for (let column = first; column <= after; column += 1) {
                const covered =
                    Math.min(to, lo + (column + 1) / perColumn) - Math.max(from, lo + column / perColumn);
                if (covered > 0) {
                    this.take(column, (n * covered) / length, value, cells.square[at], cells.low[at], cells.high[at]);
                }
            }
        }
    }

    /**
     * The figures of the view's columns: over samples, those of every sample
     * of `[t0, t1]`, as `windowStats()` gives them; over a run's buckets, each
     * bucket weighed by the share of its span the view covers.
     *
     * @returns {WindowStats} The count, mean, root mean square, minimum and maximum.
     */
    viewStats() {
        let n = 0;
        let sum = 0;
        let square = 0;
        let min = Number.POSITIVE_INFINITY;
        let max = Number.NEGATIVE_INFINITY;
        for (let column = this.offset; column < this.offset + this.columns; column += 1) {
            if (this.count[column] > 0) {
                n += this.count[column];
                sum += this.sum[column];
                square += this.square[column];
                min = Math.min(min, this.min[column]);
                max = Math.max(max, this.max[column]);
            }
        }
        if (!(n > 0)) {
            return { n: 0, mean: Number.NaN, rms: Number.NaN, min: Number.NaN, max: Number.NaN };
        }
        return { n, mean: sum / n, rms: Math.sqrt(square / n), min, max };
    }

    /**
     * The lowest minimum and the highest maximum of the view's filled columns.
     *
     * @returns {[number, number] | null} The range, null when no column holds a value.
     */
    range() {
        let lo = Number.POSITIVE_INFINITY;
        let hi = Number.NEGATIVE_INFINITY;
        for (let column = this.offset; column < this.offset + this.columns; column += 1) {
            if (this.count[column] > 0) {
                lo = Math.min(lo, this.min[column]);
                hi = Math.max(hi, this.max[column]);
            }
        }
        return lo <= hi ? [lo, hi] : null;
    }
}

/** Buffer columns kept however small a frame is: the reduction never gives these back. */
const SHRINK_FLOOR = 8192;

/**
 * Reduce several columns of the ring over `[t0, t1]`, widened by `margin`
 * columns on each side, each into its own buffers, in one pass over the
 * samples: a sample's time and its pixel column are read once for every lane.
 *
 * @param {SampleRing} ring The samples, or a run's buckets.
 * @param {readonly string[]} keys The columns, one per lane.
 * @param {number} t0 The view's start, microseconds.
 * @param {number} t1 Its end.
 * @param {number} columns The view's pixel columns.
 * @param {number} margin Whole columns beyond each edge.
 * @param {ColumnExtrema[]} into One reduction per key, filled in place.
 */
export function reduceLanes(ring, keys, t0, t1, columns, margin, into) {
    const total = columns + 2 * margin;
    for (const ext of into) {
        ext.columns = columns;
        ext.offset = margin;
        ext.resize(total);
    }
    const span = t1 - t0;
    if (span <= 0 || columns <= 0) {
        return;
    }
    const width = span / columns;
    const lo = t0 - margin * width;
    const hi = t1 + margin * width;
    const perColumn = total / (hi - lo);
    if (ring.bucketed) {
        keys.forEach((key, lane) => into[lane].reduceBuckets(ring, key, lo, hi, perColumn));
        return;
    }
    /** @type {{values: Float64Array, ext: ColumnExtrema}[]} */
    const lanes = [];
    keys.forEach((key, lane) => {
        const values = ring.values.get(key);
        if (values !== undefined) {
            lanes.push({ values, ext: into[lane] });
        }
    });
    // The view's columns hold exactly the samples of [t0, t1], the newest
    // one of a following view included, and the margins the rest of
    // [lo, hi): each stretch is clamped to its own columns, so a sample on
    // an edge never lands on the wrong side of it by a rounding.
    const from = ring.lowerBound(t0);
    const to = ring.upperBound(t1);
    fillLanes(ring, lanes, ring.lowerBound(lo), from, lo, perColumn, 0, margin - 1);
    fillLanes(ring, lanes, from, to, lo, perColumn, margin, margin + columns - 1);
    fillLanes(ring, lanes, to, ring.lowerBound(hi), lo, perColumn, margin + columns, total - 1);
}

/** Each sample's buffer column, worked out once for every lane (fillLanes()). */
let scratchColumns = new Int32Array(0);

/**
 * Take the samples of logical indices `[start, end)` into the buffer columns
 * `first` to `last` of every lane: each sample's column is worked out once,
 * then each lane runs over its values alone.
 *
 * @param {SampleRing} ring The samples.
 * @param {{values: Float64Array, ext: ColumnExtrema}[]} lanes Each lane's values and buffers.
 * @param {number} start The first index.
 * @param {number} end After the last.
 * @param {number} lo The buffers' start, microseconds.
 * @param {number} perColumn Columns per microsecond.
 * @param {number} first The lowest column they may take.
 * @param {number} last The highest.
 */
function fillLanes(ring, lanes, start, end, lo, perColumn, first, last) {
    const n = end - start;
    if (n <= 0) {
        return;
    }
    if (scratchColumns.length < n) {
        scratchColumns = new Int32Array(n);
    }
    const cells = scratchColumns;
    // The ring's arrays read directly, slot by slot: these loops visit every
    // sample in view on every frame.
    const { times, capacity } = ring;
    const head = (ring.head + start) % capacity;
    for (let j = 0, at = head; j < n; j += 1) {
        const cell = Math.floor((times[at] - lo) * perColumn);
        cells[j] = cell < first ? first : cell > last ? last : cell;
        at = at + 1 === capacity ? 0 : at + 1;
    }
    for (const lane of lanes) {
        fillLane(lane.values, lane.ext, cells, n, head, capacity);
    }
}

/**
 * One lane's values into its buffers, at the columns fillLanes() worked out.
 *
 * @param {Float64Array} values The lane's ring of values.
 * @param {ColumnExtrema} ext Its buffers.
 * @param {Int32Array} cells Each sample's column.
 * @param {number} n How many samples.
 * @param {number} head The first sample's slot.
 * @param {number} capacity The ring's slots.
 */
function fillLane(values, ext, cells, n, head, capacity) {
    const { min, max, count, sum, square, first: firsts, last: lasts } = ext;
    for (let j = 0, at = head; j < n; j += 1) {
        const value = values[at];
        at = at + 1 === capacity ? 0 : at + 1;
        if (Number.isNaN(value)) {
            continue;
        }
        const column = cells[j];
        if (count[column] === 0) {
            min[column] = value;
            max[column] = value;
            firsts[column] = value;
        } else if (value < min[column]) {
            min[column] = value;
        } else if (value > max[column]) {
            max[column] = value;
        }
        lasts[column] = value;
        count[column] += 1;
        sum[column] += value;
        square[column] += value * value;
    }
}

/**
 * Half a smoothing window, in whole columns: the window is the odd number of
 * columns nearest to `windowColumns` (ties upward), and never less than one.
 *
 * @param {number} windowColumns The window, in columns (any real).
 * @returns {number} Its half, 0 for a window of one column or less.
 */
export function windowHalf(windowColumns) {
    if (!(windowColumns > 1) || !Number.isFinite(windowColumns)) {
        return 0;
    }
    return Math.round((windowColumns - 1) / 2);
}

/**
 * The line the chart draws, per view column.
 *
 * @typedef {object} Line
 * @property {Float64Array} mean Each view column's value, NaN where the line breaks.
 * @property {Float64Array} count The samples each value is the mean of (a weight).
 * @property {number} half Half the window, whole columns.
 * @property {number} span The window, `2 * half + 1` columns.
 */

/**
 * The centered moving average of the reduced columns: each view column's
 * value is the count-weighted mean of every sample in the whole columns
 * within `half` of it, `half` from `windowHalf( windowColumns )`.
 *
 * The window holds what exists: at the data's start or end it is the mean of
 * the real samples it reaches, and before the first column that holds data,
 * after the last, and across a gap wider than the window (two neighboring
 * data columns more than `span` apart) the line is NaN: nothing is
 * extrapolated. Inside a narrower gap every column's window reaches data, and
 * the line is the mean of what is there. A window of one column is each
 * column's own mean, exactly (the running sums leave and take the one column
 * whole, so nothing is rounded).
 *
 * @param {ColumnExtrema} columns The reduced columns, with a margin of `half` for no edge effect.
 * @param {number} windowColumns The window, in columns.
 * @param {Line} [out] Buffers to reuse, when large enough.
 * @returns {Line} The line.
 */
export function movingMean(columns, windowColumns, out) {
    const half = windowHalf(windowColumns);
    const span = 2 * half + 1;
    const n = columns.columns;
    const offset = columns.offset;
    const total = columns.total;
    const line =
        out !== undefined && out.mean.length >= n
            ? out
            : { mean: new Float64Array(n), count: new Float64Array(n), half, span };
    line.half = half;
    line.span = span;
    const { mean, count } = line;
    const held = columns.count;
    // The next buffer column that holds data, at or after each view column,
    // kept in `mean` until the forward pass replaces it.
    let next = Number.POSITIVE_INFINITY;
    for (let i = total - 1; i >= offset; i -= 1) {
        if (held[i] > 0) {
            next = i;
        }
        if (i < offset + n) {
            mean[i - offset] = next;
        }
    }
    let previous = Number.NEGATIVE_INFINITY;
    for (let i = 0; i < offset; i += 1) {
        if (held[i] > 0) {
            previous = i;
        }
    }
    let sum = 0;
    let weight = 0;
    for (let i = Math.max(0, offset - half); i <= Math.min(total - 1, offset + half); i += 1) {
        sum += columns.sum[i];
        weight += held[i];
    }
    for (let c = 0; c < n; c += 1) {
        const i = c + offset;
        if (c > 0) {
            const leaving = i - half - 1;
            if (leaving >= 0) {
                sum -= columns.sum[leaving];
                weight -= held[leaving];
            }
            const entering = i + half;
            if (entering < total) {
                sum += columns.sum[entering];
                weight += held[entering];
            }
        }
        if (held[i] > 0) {
            previous = i;
        }
        const bridged = held[i] > 0 || mean[c] - previous <= span;
        if (!bridged || !(weight > 0)) {
            mean[c] = Number.NaN;
            count[c] = 0;
        } else {
            mean[c] = sum / weight;
            count[c] = weight;
        }
    }
    return line;
}

/**
 * The figures of a window of samples.
 *
 * @typedef {object} WindowStats
 * @property {number} n How many samples (a weight over a run's buckets).
 * @property {number} mean Their arithmetic mean, NaN when there are none.
 * @property {number} rms Their root mean square, NaN when there are none.
 * @property {number} min Their lowest value, NaN when there are none.
 * @property {number} max Their highest, NaN when there are none.
 */

/**
 * The count, mean, root mean square, minimum and maximum of one column's
 * samples in `[t0, t1]`, from the samples themselves: the recorded values,
 * never the smoothed line. Over a run's buckets each bucket counts the share
 * of its samples that its overlap with the window is of its span.
 *
 * @param {SampleRing} ring The samples, or a run's buckets.
 * @param {string} key The column.
 * @param {number} t0 The window's start, microseconds.
 * @param {number} t1 Its end, inclusive.
 * @returns {WindowStats} The figures.
 */
export function windowStats(ring, key, t0, t1) {
    let n = 0;
    let sum = 0;
    let square = 0;
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;
    const cells = ring.cells.get(key);
    if (ring.bucketed && cells !== undefined) {
        const end = ring.lowerBound(t1);
        for (let i = Math.max(0, ring.lowerBound(t0) - 1); i < end; i += 1) {
            const at = ring.slot(i);
            const start = ring.times[at];
            const length = ring.ends[at] - start;
            const covered = Math.min(t1, start + length) - Math.max(t0, start);
            if (!(cells.n[at] > 0) || !(length > 0) || !(covered > 0)) {
                continue;
            }
            const weight = (cells.n[at] * covered) / length;
            n += weight;
            sum += weight * ring.valueAt(key, i);
            square += weight * cells.square[at];
            min = Math.min(min, cells.low[at]);
            max = Math.max(max, cells.high[at]);
        }
    } else if (!ring.bucketed) {
        for (let i = ring.lowerBound(t0); i < ring.size && ring.timeAt(i) <= t1; i += 1) {
            const value = ring.valueAt(key, i);
            if (Number.isNaN(value)) {
                continue;
            }
            n += 1;
            sum += value;
            square += value * value;
            min = Math.min(min, value);
            max = Math.max(max, value);
        }
    }
    if (!(n > 0)) {
        return { n: 0, mean: Number.NaN, rms: Number.NaN, min: Number.NaN, max: Number.NaN };
    }
    return { n, mean: sum / n, rms: Math.sqrt(square / n), min, max };
}

/** The display smoothing a chart offers: off (the mean per pixel column) and four windows. */
export const SMOOTHING = Object.freeze([
    { label: "Off", us: 0 },
    { label: "1 ms", us: 1_000 },
    { label: "10 ms", us: 10_000 },
    { label: "100 ms", us: 100_000 },
    { label: "1 s", us: 1_000_000 },
]);

/**
 * A duration as a caption writes it: three significant digits, no trailing zero.
 *
 * @param {number} us The duration, microseconds.
 * @returns {string} For example `4.6 ms`, `1 s`.
 */
export function formatDuration(us) {
    const [number, unit] = formatQuantity(us / 1e6, "s", 3).split(" ");
    const trimmed = number.includes(".") ? number.replace(/0+$/, "").replace(/\.$/, "") : number;
    return `${trimmed} ${unit}`;
}

/**
 * The smoothing window a choice gives on columns of a width, and the caption
 * that names it.
 *
 * @typedef {object} SmoothingWindow
 * @property {number} half Half the window, whole columns.
 * @property {number} span The window, whole columns.
 * @property {number} us Its length, microseconds (NaN for no column).
 * @property {string} caption What the chart says it draws.
 */

/**
 * The window a smoothing choice takes on columns `columnUs` wide: the odd
 * number of whole columns nearest to the choice, one at the least. The
 * caption names the effective window: the column's width when the line is
 * the mean per pixel, the window's length when it differs from the choice by
 * more than half a percent, and the choice beside either when one was made.
 *
 * A window is never longer than twice the view's `columns`, half a view
 * beyond each edge: a choice past that is drawn at that length, and the
 * caption names it with the choice.
 *
 * @param {number} choiceUs The choice, microseconds, 0 for off.
 * @param {number} columnUs A column's width, microseconds.
 * @param {number} [columns] The view's columns, no limit when not given.
 * @returns {SmoothingWindow} The window.
 */
export function smoothingWindow(choiceUs, columnUs, columns = Number.POSITIVE_INFINITY) {
    if (!(columnUs > 0) || !Number.isFinite(columnUs)) {
        return { half: 0, span: 1, us: Number.NaN, caption: "mean per pixel" };
    }
    // A window is never more than twice the view: a longer one would only
    // flatten the line, and its margins would cost columns by the million.
    const half = choiceUs > 0 ? Math.min(windowHalf(choiceUs / columnUs), Math.max(0, columns)) : 0;
    const span = 2 * half + 1;
    const us = span * columnUs;
    const asked = choiceUs > 0 ? ` (${formatDuration(choiceUs)} asked)` : "";
    if (span === 1) {
        return { half, span, us, caption: `mean per pixel, ${formatDuration(columnUs)}${asked}` };
    }
    const caption =
        Math.abs(us - choiceUs) <= choiceUs * 0.005
            ? `moving average ${formatDuration(choiceUs)}`
            : `moving average ${formatDuration(us)}${asked}`;
    return { half, span, us, caption };
}

/**
 * The INA228's averaging a sample was taken with, as a run's `meta.json`
 * (`noise_reduction`), `/api/capture` and the runs list have it.
 *
 * @typedef {object} Noise
 * @property {string} state `on` (the averaging settings') or `off` (the profile's own conversion).
 * @property {number} count The conversions averaged into a sample.
 * @property {number} conversion_us One conversion of the shunt, microseconds.
 * @property {number} period_us What a sample takes, microseconds.
 */

/**
 * The averaging `SENSe:PROFile:REPort?` gives the live profile.
 *
 * @param {Record<string, any> | null | undefined} fields The report, as JSON.
 * @returns {Noise | null} It, or null for an image that has none (before 0.18.0).
 */
export function noiseOf(fields) {
    if (typeof fields?.avg_state !== "string") {
        return null;
    }
    return {
        state: fields.avg_state,
        count: Number(fields.avg),
        conversion_us: Number(fields.vshct_us),
        period_us: Number(fields.period_us),
    };
}

/**
 * What a sample takes with the averaging on: every conversion of the shunt
 * and of the other inputs the profile converts, times the count.
 *
 * @param {{other_us: number}} profile The profile, as `/api/schema` declares it.
 * @param {number} count The averaging count.
 * @param {number} conversionUs The shunt conversion time, microseconds.
 * @returns {number} The sample's period, microseconds.
 */
export function averagedPeriod(profile, count, conversionUs) {
    return (conversionUs + profile.other_us) * count;
}

/**
 * An averaging in a few words.
 *
 * @param {Noise} noise The averaging.
 * @returns {string} For example `AVG 16, 18.4 ms per sample`.
 */
export function noiseText(noise) {
    return `AVG ${noise.count}, ${formatDuration(noise.period_us)} per sample`;
}

/**
 * The caption that says the samples were averaged in the INA228, empty when
 * they were not or the page does not know.
 *
 * @param {Noise | null | undefined} noise Their averaging.
 * @returns {string} For example `INA228 AVG 16, 18.4 ms per sample`.
 */
export function noiseCaption(noise) {
    return noise?.state === "on" ? `INA228 ${noiseText(noise)}` : "";
}

/**
 * What a chart says it draws, on one line: the filter, the run's buckets
 * when it draws a decimated run (each a mean of its samples, the resolution
 * the line cannot go under), then the averaging.
 *
 * @param {SmoothingWindow} window The smoothing window.
 * @param {Noise | null | undefined} noise The samples' averaging.
 * @param {number} [bucketUs] A run bucket's width, 0 for samples.
 * @returns {string} For example `moving average 1 s · INA228 AVG 16, 18.4 ms per sample`.
 */
export function chartCaption(window, noise, bucketUs = 0) {
    const buckets = bucketUs > 0 ? `run buckets ${formatDuration(bucketUs)}` : "";
    return [window.caption, buckets, noiseCaption(noise)].filter((text) => text !== "").join(" · ");
}

/**
 * The averaging as a view export's header says it.
 *
 * @param {Noise | null | undefined} noise The samples' averaging, null when the page does not know it.
 * @returns {[string, string][]} Its state (`on`, `off`, `unknown`), count, conversion time and period.
 */
export function noisePairs(noise) {
    if (!noise) {
        return [["noise_reduction", "unknown"]];
    }
    return [
        ["noise_reduction", noise.state],
        ["average_count", String(noise.count)],
        ["conversion_us", String(noise.conversion_us)],
        ["period_us", String(noise.period_us)],
    ];
}

/**
 * The value range an axis shows for the data's range: padded by a twentieth
 * of its span on each side, and widened around a flat line so it has height.
 *
 * @param {[number, number] | null} range The data's lowest and highest value.
 * @returns {[number, number]} The axis's lowest and highest value.
 */
export function paddedRange(range) {
    if (range === null) {
        return [0, 1];
    }
    const [lo, hi] = range;
    if (hi === lo) {
        const half = lo === 0 ? 1 : Math.abs(lo) * 0.01;
        return [lo - half, hi + half];
    }
    const pad = (hi - lo) * 0.05;
    return [lo - pad, hi + pad];
}

/**
 * Round tick positions for an axis: steps of 1, 2 or 5 times a power of ten.
 *
 * @param {number} lo The axis's lowest value.
 * @param {number} hi Its highest.
 * @param {number} most About how many ticks are wanted: the step is the
 *     round one at or above the span divided by it.
 * @returns {{step: number, ticks: number[]}} The step and the ticks inside the range.
 */
export function niceTicks(lo, hi, most) {
    const span = hi - lo;
    if (!(span > 0) || !Number.isFinite(span)) {
        return { step: 0, ticks: Number.isFinite(lo) ? [lo] : [] };
    }
    const rough = span / Math.max(1, most);
    const power = 10 ** Math.floor(Math.log10(rough));
    const fraction = rough / power;
    const factor = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    const step = factor * power;
    /** @type {number[]} */
    const ticks = [];
    for (let k = Math.ceil(lo / step); k * step <= hi + step * 1e-9; k += 1) {
        ticks.push(k * step);
    }
    return { step, ticks };
}

/** The SI prefixes a quantity is written with, from a thousand to the -4 (pico) to its square (mega). */
const PREFIXES = Object.freeze(["p", "n", "µ", "m", "", "k", "M"]);

/**
 * The SI prefix of a power of a thousand.
 *
 * @param {number} power The power, -4 (pico) to 2 (mega).
 * @returns {string} Its prefix, empty for 0.
 */
function prefixOf(power) {
    return PREFIXES[power + 4];
}

/**
 * The power of a thousand a value is written in: the largest at most its
 * magnitude, within the prefixes the page uses.
 *
 * @param {number} value A value in the base unit.
 * @returns {number} The power, -4 (pico) to 2 (mega).
 */
export function thousands(value) {
    const magnitude = Math.abs(value);
    if (magnitude === 0 || !Number.isFinite(magnitude)) {
        return 0;
    }
    return Math.max(-4, Math.min(2, Math.floor(Math.log10(magnitude) / 3)));
}

/**
 * A quantity as the page writes it: an SI prefix, and the digits asked for.
 *
 * @param {number} value The value, in `unit`.
 * @param {string} unit Its unit (`A`, `V`, `W`, `°C`, `s`); `°C` takes no prefix.
 * @param {number} [digits] Significant digits, 4 by default.
 * @param {number} [power] The power of a thousand to write it in, -4 to 2; chosen from the value if absent.
 * @returns {string} For example `88.60 mA`; `-` for NaN.
 */
export function formatQuantity(value, unit, digits = 4, power) {
    if (Number.isNaN(value)) {
        return "-";
    }
    if (unit === "°C") {
        return `${value.toFixed(Math.max(0, digits - 2))} °C`;
    }
    const chosen = power ?? thousands(value);
    const scaled = value / 1000 ** chosen;
    const whole = Math.abs(scaled) >= 1 ? Math.floor(Math.log10(Math.abs(scaled))) + 1 : 1;
    const decimals = Math.max(0, digits - whole);
    return `${scaled.toFixed(decimals)} ${prefixOf(chosen)}${unit}`;
}

/**
 * A column's integer as the quantity it stands for.
 *
 * @param {number} raw The live events' integer (`i_na` 88600000).
 * @param {Channel} channel Its channel.
 * @param {number} [digits] Significant digits.
 * @returns {string} For example `88.60 mA`.
 */
export function formatRaw(raw, channel, digits = 4) {
    return formatQuantity(raw * channel.scale, channel.unit, digits);
}

/**
 * Tick labels for an axis, all in the one prefix its largest tick takes, with
 * as many decimals as the step needs.
 *
 * @param {number[]} ticks The ticks, in the base unit.
 * @param {number} step Their step.
 * @param {string} unit The unit.
 * @returns {string[]} One label per tick.
 */
export function tickLabels(ticks, step, unit) {
    if (ticks.length === 0) {
        return [];
    }
    const largest = Math.max(...ticks.map((tick) => Math.abs(tick)));
    const power = unit === "°C" ? 0 : thousands(largest);
    const scaledStep = step / 1000 ** power;
    const decimals = scaledStep > 0 ? Math.max(0, -Math.floor(Math.log10(scaledStep) + 1e-9)) : 0;
    const prefix = unit === "°C" ? "" : prefixOf(power);
    return ticks.map((tick) => {
        const scaled = tick / 1000 ** power;
        const text = (Math.abs(scaled) < scaledStep * 1e-6 ? 0 : scaled).toFixed(decimals);
        return `${text} ${prefix}${unit}`;
    });
}

/**
 * The time window the chart shows: it follows the newest sample until a zoom
 * or a pan fixes it, and the window presets set its length.
 */
export class TimeView {
    /** @param {number} windowUs The window's length, microseconds. */
    constructor(windowUs) {
        /** The window's length, microseconds. */
        this.windowUs = windowUs;
        /** Whether its right edge follows the newest sample. */
        this.follow = true;
        /** Its start, microseconds. */
        this.t0 = 0;
        /** Its end, microseconds. */
        this.t1 = windowUs;
    }

    /**
     * Move a following window's end to the newest sample.
     *
     * @param {number} newest The newest sample's time, NaN when there is none.
     */
    track(newest) {
        if (this.follow && Number.isFinite(newest)) {
            this.t1 = newest;
            this.t0 = newest - this.windowUs;
        }
    }

    /**
     * Set the window's length, and follow the newest sample again.
     *
     * @param {number} windowUs The length, microseconds.
     * @param {number} newest The newest sample's time.
     */
    setWindow(windowUs, newest) {
        this.windowUs = windowUs;
        this.follow = true;
        this.t1 = Number.isFinite(newest) ? newest : windowUs;
        this.t0 = this.t1 - windowUs;
    }

    /**
     * Zoom about a point of the window; the window stops following.
     *
     * @param {number} factor Below 1 zooms in, above 1 out.
     * @param {number} anchor Where the point is, 0 (left edge) to 1 (right edge).
     * @param {number} [shortestUs] The shortest window allowed.
     */
    zoom(factor, anchor, shortestUs = 1000) {
        const span = this.t1 - this.t0;
        const next = Math.max(shortestUs, span * factor);
        const at = this.t0 + span * Math.min(1, Math.max(0, anchor));
        this.t0 = at - (at - this.t0) * (next / span);
        this.t1 = this.t0 + next;
        this.follow = false;
    }

    /**
     * Move the window by a share of its length; the window stops following.
     *
     * @param {number} share Positive moves later, negative earlier.
     */
    pan(share) {
        const shift = (this.t1 - this.t0) * share;
        this.t0 += shift;
        this.t1 += shift;
        this.follow = false;
    }

    /**
     * The horizontal position of a time.
     *
     * @param {number} t The time, microseconds.
     * @param {number} width The plot's width.
     * @returns {number} The position, 0 to `width` inside the window.
     */
    xOf(t, width) {
        return ((t - this.t0) * width) / (this.t1 - this.t0);
    }
}

/**
 * At most one frame per interval: the 30 fps cap.
 */
export class FrameGate {
    /** @param {number} intervalMs The shortest time between two frames. */
    constructor(intervalMs) {
        /** The shortest time between two frames, milliseconds. */
        this.intervalMs = intervalMs;
        /** When the last frame was drawn. */
        this.lastMs = Number.NEGATIVE_INFINITY;
        /** What the last frame cost to draw, milliseconds. */
        this.costMs = 0;
    }

    /**
     * Whether a frame may be drawn now; if so, it is counted as drawn.
     *
     * @param {number} nowMs The time now, milliseconds.
     * @returns {boolean} Whether to draw.
     */
    ready(nowMs) {
        if (nowMs - this.lastMs < Math.max(this.intervalMs, 2 * this.costMs) - 0.5) {
            return false;
        }
        this.lastMs = nowMs;
        return true;
    }

    /**
     * Say what the frame just drawn cost: the next waits at least twice that,
     * so drawing never takes more than half the page's time, whatever the
     * view holds.
     *
     * @param {number} costMs The frame's drawing time, milliseconds.
     */
    spent(costMs) {
        this.costMs = costMs;
    }
}

/** The 30 fps cap's interval, milliseconds. */
export const FRAME_INTERVAL_MS = 1000 / 30;

/**
 * Rows a `samples` event says were skipped since the last one.
 *
 * @param {number | null} expected The `n0` the next batch should carry, null for the first.
 * @param {number} n0 The batch's `n0`.
 * @returns {number} The rows missing between the two, 0 when none or the count restarted.
 */
export function rowsMissed(expected, n0) {
    if (expected === null || n0 <= expected) {
        return 0;
    }
    return n0 - expected;
}

/**
 * One answer of `/api/runs/<run>/decimated`.
 *
 * @typedef {object} Decimation
 * @property {string} column The column.
 * @property {number | null} t0_us The window's first timestamp.
 * @property {number | null} t1_us Its last, inclusive.
 * @property {number | null} bucket_us A bucket's width.
 * @property {number[]} t_us Each bucket's start.
 * @property {number[]} n Its samples.
 * @property {number[]} mean Their mean.
 * @property {number[]} rms Their root mean square.
 * @property {number[]} min Their minimum.
 * @property {number[]} max Their maximum.
 */

/**
 * A run's buckets, merged over its columns.
 *
 * @typedef {object} MergedBuckets
 * @property {number[]} t Each bucket's start, in time order.
 * @property {number[]} end Where it ends, exclusive: a bucket's width on, and no later than the window's end.
 * @property {Record<string, {n: number[], mean: number[], rms: number[], min: number[], max: number[]}>} columns
 *     Each column's figures per bucket, `n` 0 and the rest NaN where it holds none.
 */

/**
 * A run's decimations of several columns, merged into buckets the ring takes.
 *
 * Each answer covers the same window in buckets of the same width when asked
 * with the same `t0_us`, `t1_us` and `buckets`, but lists only the buckets
 * that hold a sample of its column. The merge lists every bucket any column
 * holds once, with each column's count, mean, root mean square, minimum and
 * maximum there, so the chart weighs each bucket by its samples and its band
 * keeps every spike.
 *
 * @param {readonly Decimation[]} answers The decimations, one per column.
 * @returns {MergedBuckets} The buckets.
 */
export function mergeBuckets(answers) {
    /** @type {Map<number, Map<string, number>>} */
    const byTime = new Map();
    let width = 0;
    let close = Number.NEGATIVE_INFINITY;
    for (const answer of answers) {
        width = Math.max(width, answer.bucket_us ?? 0);
        close = Math.max(close, (answer.t1_us ?? Number.NEGATIVE_INFINITY) + 1);
        answer.t_us.forEach((t, i) => {
            const slot = byTime.get(t) ?? new Map();
            slot.set(answer.column, i);
            byTime.set(t, slot);
        });
    }
    const t = [...byTime.keys()].sort((a, b) => a - b);
    const end = t.map((start) => Math.min(start + width, close));
    /** @type {MergedBuckets["columns"]} */
    const columns = {};
    for (const answer of answers) {
        const figures = { n: [], mean: [], rms: [], min: [], max: [] };
        for (const start of t) {
            const i = byTime.get(start)?.get(answer.column);
            figures.n.push(i === undefined ? 0 : answer.n[i]);
            for (const name of /** @type {const} */ (["mean", "rms", "min", "max"])) {
                figures[name].push(i === undefined ? Number.NaN : answer[name][i]);
            }
        }
        columns[answer.column] = figures;
    }
    return { t, end, columns };
}

/**
 * What bucket `i` of a merge holds of a column, as `SampleRing.pushBucket` takes it.
 *
 * @param {MergedBuckets} merged The merge.
 * @param {string} key The column.
 * @param {number} i The bucket.
 * @returns {BucketCell | null} Its figures, null when it holds none of the column.
 */
export function bucketOf(merged, key, i) {
    const figures = merged.columns[key];
    if (figures === undefined || !(figures.n[i] > 0)) {
        return null;
    }
    return {
        n: figures.n[i],
        mean: figures.mean[i],
        rms: figures.rms[i],
        min: figures.min[i],
        max: figures.max[i],
    };
}

// -- the view's exports -------------------------------------------------------------------

/** Each column's integer unit, as a run's own exports name it (recorder.py). */
const RAW_UNITS = Object.freeze({ i_na: "nA", v_uv: "uV", p_uw: "uW", die_mc: "mdegC" });

/** The columns of a view CSV's table, after its `# key=value` lines. */
export const VIEW_FIELDS = Object.freeze(["t_us", "mean", "min", "max", "n", "channel"]);

/**
 * What an export of the view is of.
 *
 * @typedef {object} ViewSubject
 * @property {string} source `live`, or `run <id>` for a run's history chart.
 * @property {string} instrument The instrument's serial, `unknown` when the page does not know it.
 * @property {Noise | null} noise The INA228's averaging the samples were taken with, null when unknown.
 */

/**
 * One lane as a frame drew it, per view column.
 *
 * @typedef {object} LaneSnapshot
 * @property {string} key The column (`i_na`).
 * @property {Float64Array} mean The line, NaN where it breaks.
 * @property {Float64Array} n The samples the line's value averages (a weight).
 * @property {Float64Array} min The column's lowest sample: the band's bottom.
 * @property {Float64Array} max Its highest: the band's top.
 * @property {Float64Array} held The column's own samples, 0 where it holds none.
 */

/**
 * One frame of the chart, as an export writes it.
 *
 * @typedef {object} ViewFrame
 * @property {number} t0 The view's start, microseconds.
 * @property {number} t1 Its end.
 * @property {number} columns Its pixel columns.
 * @property {number} columnUs A column's width, microseconds.
 * @property {number} choiceUs The smoothing asked for, 0 for off.
 * @property {SmoothingWindow} window The window drawn.
 * @property {number} [bucketUs] A run bucket's width when the view is of a run's buckets, else 0.
 * @property {LaneSnapshot[]} lanes Each lane, top to bottom.
 */

/**
 * A copy of what one lane drew: the line, and the view's columns of the reduction.
 *
 * @param {string} key The column.
 * @param {ColumnExtrema} columns The reduction the line was drawn from.
 * @param {Line} line The line drawn.
 * @returns {LaneSnapshot} The copy, one value per view column.
 */
export function laneSnapshot(key, columns, line) {
    const n = columns.columns;
    const from = columns.offset;
    return {
        key,
        mean: line.mean.slice(0, n),
        n: line.count.slice(0, n),
        min: columns.min.slice(from, from + n),
        max: columns.max.slice(from, from + n),
        held: columns.count.slice(from, from + n),
    };
}

/**
 * A number as an export writes it: the shortest text that reads back to the
 * same double, empty for NaN.
 *
 * @param {number} value The number.
 * @returns {string} Its text.
 */
function cell(value) {
    return Number.isNaN(value) ? "" : String(value);
}

/**
 * What a view export says of the view, in order: the `# key=value` lines of
 * its CSV and the text chunks of its PNG. The filter is named twice: as a
 * kind (`mean per pixel column` or `moving average`) and as the caption the
 * chart draws, with the window asked for, the window drawn and a column's
 * width beside them. A run's view is of its buckets, a decimation that is a
 * filter of its own: `source_resolution` and `bucket_us` name it. The INA228's
 * averaging, which changed the samples themselves, follows: the filter never
 * does.
 *
 * @param {ViewFrame} frame The frame.
 * @param {ViewSubject} subject What it is of.
 * @returns {[string, string][]} The pairs, each value on one line.
 */
export function viewHeader(frame, subject) {
    const keys = frame.lanes.map((lane) => lane.key);
    /** @type {[string, string][]} */
    const pairs = [
        ["filter", frame.window.span > 1 ? "moving average" : "mean per pixel column"],
        ["caption", chartCaption(frame.window, subject.noise, frame.bucketUs ?? 0)],
        ["asked_ms", cell(frame.choiceUs / 1000)],
        ["window_ms", cell(frame.window.us / 1000)],
        ["column_ms", cell(frame.columnUs / 1000)],
        ...bucketPairs(frame.bucketUs ?? 0),
        ...noisePairs(subject.noise),
        ["source", subject.source],
        ["instrument", subject.instrument],
        ["t0_us", cell(frame.t0)],
        ["t1_us", cell(frame.t1)],
        ["columns", String(frame.columns)],
        ["channels", keys.join(",")],
        ...keys.map((key) => {
            const unit = /** @type {Record<string, string>} */ (RAW_UNITS)[key] ?? "?";
            return /** @type {[string, string]} */ ([`unit_${key}`, unit]);
        }),
        [
            "fields",
            "t_us is each pixel column's middle; mean is the line drawn there, empty where it breaks;" +
                " min and max are the column's lowest and highest sample, the band, empty where it" +
                " holds none; n is how many samples the line's value averages",
        ],
    ];
    return pairs.map(([key, value]) => [key, value.replace(/[\r\n]+/g, " ")]);
}

/**
 * What a view export says of the data under its columns: every sample, or a
 * run's buckets (each the mean of its samples, its minimum and maximum the
 * bucket's) and their width.
 *
 * @param {number} bucketUs A bucket's width, 0 for samples.
 * @returns {[string, string][]} `source_resolution` and `bucket_us`.
 */
export function bucketPairs(bucketUs) {
    if (!(bucketUs > 0)) {
        return [
            ["source_resolution", "every sample"],
            ["bucket_us", ""],
        ];
    }
    return [
        ["source_resolution", "run buckets: each the mean of its samples, min and max its own"],
        ["bucket_us", String(bucketUs)],
    ];
}

/**
 * Whether a batch of samples that just landed changes what a chart shows: it
 * always does while the chart follows the newest sample; held, only a batch
 * that starts inside the view or its smoothing margin, or one whose landing
 * made the ring forget samples the view or its margin holds.
 *
 * @param {{follow: boolean, t0: number, t1: number}} view The time view.
 * @param {number} firstNewUs The batch's first time, microseconds.
 * @param {number} oldestHeldUs The ring's oldest time after the batch.
 * @param {number} marginUs Half the smoothing window, microseconds (NaN for none yet).
 * @returns {boolean} Whether to draw again.
 */
export function batchChangesView(view, firstNewUs, oldestHeldUs, marginUs) {
    if (view.follow) {
        return true;
    }
    const margin = Number.isFinite(marginUs) ? marginUs : 0;
    return firstNewUs <= view.t1 + margin || oldestHeldUs > view.t0 - margin;
}

/**
 * The CSV of a view: its header's `# key=value` lines, then `t_us`, `mean`,
 * `min`, `max`, `n` and `channel` for every pixel column of every lane, the
 * values in the column's own integer unit (`unit_i_na=nA`). It is what the
 * chart drew, never the recorded samples: those are a run's own exports.
 *
 * @param {ViewFrame} frame The frame.
 * @param {ViewSubject} subject What it is of.
 * @returns {string} The file's text, each line ended by a newline.
 */
export function viewCsv(frame, subject) {
    const lines = viewHeader(frame, subject).map(([key, value]) => `# ${key}=${value}`);
    lines.push(VIEW_FIELDS.join(","));
    for (const lane of frame.lanes) {
        for (let c = 0; c < frame.columns; c += 1) {
            const held = lane.held[c] > 0;
            lines.push(
                [
                    cell(frame.t0 + (c + 0.5) * frame.columnUs),
                    cell(lane.mean[c]),
                    held ? cell(lane.min[c]) : "",
                    held ? cell(lane.max[c]) : "",
                    cell(lane.n[c]),
                    lane.key,
                ].join(","),
            );
        }
    }
    return `${lines.join("\n")}\n`;
}

/**
 * The name a view export is saved under: its source with every character a
 * file system may refuse replaced, then `-view` and the extension.
 *
 * @param {string} source `live`, or `run <id>`.
 * @param {string} extension `csv` or `png`.
 * @returns {string} For example `live-view.png`.
 */
export function viewFileName(source, extension) {
    const base = source.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
    return `${base === "" ? "chart" : base}-view.${extension}`;
}

/**
 * The line an image of the view carries over its lanes: the source, the time
 * window from the axis's origin and its length, and the instrument.
 *
 * @param {ViewSubject} subject What it is of.
 * @param {number} t0 The view's start, microseconds.
 * @param {number} t1 Its end.
 * @param {number} origin The time the axis counts from.
 * @returns {string} For example `live · 2.0000 s to 12.000 s (10.0 s) · KM-EE529C1B03`.
 */
export function viewTitle(subject, t0, t1, origin) {
    const at = (/** @type {number} */ t) => formatQuantity((t - origin) / 1e6, "s", 5, 0);
    const length = formatQuantity((t1 - t0) / 1e6, "s", 3);
    return `${subject.source} · ${at(t0)} to ${at(t1)} (${length}) · ${subject.instrument}`;
}

/** The CRC-32 table of the PNG and zlib polynomial, built once. */
const CRC_TABLE = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
        let c = n;
        for (let k = 0; k < 8; k += 1) {
            c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        }
        table[n] = c >>> 0;
    }
    return table;
})();

/**
 * The CRC-32 of some bytes, as a PNG chunk's.
 *
 * @param {Uint8Array} bytes The bytes.
 * @returns {number} The CRC, unsigned.
 */
export function crc32(bytes) {
    let c = 0xffffffff;
    for (const byte of bytes) {
        c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
    }
    return (c ^ 0xffffffff) >>> 0;
}

/**
 * Text as a PNG's `tEXt` takes it: Latin-1, a character beyond it written `?`.
 *
 * @param {string} text The text.
 * @returns {number[]} Its bytes.
 */
function latin1(text) {
    return Array.from(text, (ch) => {
        const code = /** @type {number} */ (ch.codePointAt(0));
        return code > 0 && code <= 0xff ? code : 0x3f;
    });
}

/**
 * A PNG with `tEXt` chunks added after its header, one per pair: the keyword
 * (at most 79 characters) and its text.
 *
 * @param {Uint8Array} png The PNG, as a canvas encodes it.
 * @param {[string, string][]} pairs The keywords and their texts.
 * @returns {Uint8Array} The PNG with them.
 */
export function pngWithText(png, pairs) {
    const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
    if (png.length < 33 || signature.some((byte, i) => png[i] !== byte)) {
        throw new Error("not a PNG");
    }
    const headerEnd = 8 + 12 + new DataView(png.buffer, png.byteOffset + 8, 4).getUint32(0);
    const chunks = pairs.map(([keyword, text]) => {
        const data = [...latin1("tEXt"), ...latin1(keyword.slice(0, 79)), 0, ...latin1(text)];
        const chunk = new Uint8Array(data.length + 8);
        const view = new DataView(chunk.buffer);
        view.setUint32(0, data.length - 4);
        chunk.set(data, 4);
        view.setUint32(data.length + 4, crc32(chunk.subarray(4, data.length + 4)));
        return chunk;
    });
    const out = new Uint8Array(png.length + chunks.reduce((sum, chunk) => sum + chunk.length, 0));
    out.set(png.subarray(0, headerEnd), 0);
    let at = headerEnd;
    for (const chunk of chunks) {
        out.set(chunk, at);
        at += chunk.length;
    }
    out.set(png.subarray(headerEnd), at);
    return out;
}

/**
 * Bytes as Base64, for a `data:` URL.
 *
 * @param {Uint8Array} bytes The bytes.
 * @returns {string} Their Base64.
 */
export function base64Of(bytes) {
    let text = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
        text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(text);
}

/**
 * Base64 as bytes.
 *
 * @param {string} text The Base64.
 * @returns {Uint8Array} Its bytes.
 */
export function bytesOf(text) {
    return Uint8Array.from(atob(text), (ch) => ch.charCodeAt(0));
}
