// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The live chart on a Canvas 2D: one lane and one axis per unit, a
 * shared time axis, the marks, zoom, pan and a crosshair with keyboard
 * equivalents, and a text readout for assistive technology.
 *
 * Each lane draws, per pixel column, the band of the samples' minimum to
 * maximum, faint, and over it the line: the arithmetic mean of each column,
 * or with display smoothing the centered moving average over a window of
 * whole columns (chart-math.js, `movingMean()`). The band is always the raw
 * extremes, so smoothing never hides a spike, and the caption drawn inside
 * the plot names what the line is ("mean per pixel, 4.6 ms", "moving average
 * 100 ms"), so an image of the chart carries it. The samples are never
 * changed: the readout's mean, RMS, minimum and maximum are those of the
 * samples in view, whatever the smoothing.
 *
 * "Export view" saves what the chart shows, made in the page with no request
 * (`exportView()`): a PNG of the canvas on the theme's ground, with a line over
 * the lanes that names the source (`live` or the run), the time window and the
 * instrument, and the CSV of the same frame, per pixel column the line, the
 * band's minimum and maximum and the samples the line averages, its header
 * naming the filter and the window. Both are drawn from one frame, the one
 * the export draws.
 *
 * The arithmetic is chart-math.js's; this module only measures the canvas,
 * reads the theme's tokens and draws. It draws only when something changed,
 * and at most 30 frames a second (`FrameGate`).
 *
 * Keys, with the chart focused: Left and Right pan by a tenth of the window,
 * Shift with them moves the crosshair, + and - zoom about the crosshair (or
 * the middle), F or Home follows the newest sample again, Escape hides the
 * crosshair. With a pointer: drag pans, Ctrl or a pinch with the wheel zooms,
 * a horizontal wheel pans, a double click follows again.
 */

import {
    ColumnExtrema,
    FRAME_INTERVAL_MS,
    FrameGate,
    MarkList,
    SampleRing,
    TimeView,
    base64Of,
    bytesOf,
    channelOf,
    chartCaption,
    formatQuantity,
    formatRaw,
    laneSnapshot,
    movingMean,
    niceTicks,
    noiseCaption,
    paddedRange,
    pngWithText,
    reduceLanes,
    smoothingWindow,
    tickLabels,
    viewCsv,
    viewHeader,
    viewTitle,
} from "./chart-math.js";

/** The width the value labels take on the left of each lane, CSS pixels. */
export const AXIS_WIDTH = 84;
/** Right of the plot, CSS pixels. */
export const RIGHT_PAD = 8;
/** The height of the time axis under the lanes, CSS pixels. */
export const TIME_AXIS_HEIGHT = 24;
/** Between two lanes, CSS pixels. */
export const LANE_GAP = 16;
/** Above the first lane, for the marks' labels, CSS pixels. */
export const TOP_PAD = 18;
/** The shortest time between two readouts while nothing is pointed at, milliseconds. */
const READOUT_INTERVAL_MS = 1000;
/** The line's width, CSS pixels. */
const LINE_WIDTH = 1.5;

/**
 * Where a lane is drawn on a canvas of a height.
 *
 * @param {number} height The canvas's height, CSS pixels.
 * @param {number} lanes How many lanes it holds.
 * @param {number} lane Which, from 0 at the top.
 * @returns {{top: number, height: number}} The lane's top and height, CSS pixels.
 */
export function laneBox(height, lanes, lane) {
    const count = Math.max(1, lanes);
    const space = height - TOP_PAD - TIME_AXIS_HEIGHT - LANE_GAP * (count - 1);
    const laneH = Math.max(24, space / count);
    return { top: TOP_PAD + lane * (laneH + LANE_GAP), height: laneH };
}

/**
 * The theme's colors and the label font, read from the canvas's own style.
 *
 * @typedef {object} Theme
 * @property {string} ink The line, and the text that matters.
 * @property {string} ink2
 * @property {string} ink3
 * @property {string} bg The ground, behind the caption and the labels drawn over data.
 * @property {string} band The minimum-to-maximum band, faint.
 * @property {string} rule
 * @property {string} ruleStrong
 * @property {string} accent
 * @property {string} font
 */

/**
 * What one lane showed at the last frame, for the readout.
 *
 * @typedef {object} LaneFigures
 * @property {string} key The column.
 * @property {import("./chart-math.js").WindowStats} view The figures of the samples in view.
 * @property {{line: number, n: number, min: number, max: number} | null} cursor
 *     At the crosshair's column: the line's value (NaN where it breaks), the
 *     column's samples and their minimum and maximum; null with no crosshair.
 */

/**
 * A view saved: the files an export of it makes.
 *
 * @typedef {object} ViewExport
 * @property {string} png The PNG of the canvas, as a `data:` URL.
 * @property {string} csv The CSV of the frame's columns.
 */

/**
 * The chart on one canvas, over one ring of samples and one list of marks.
 */
export class LiveChart {
    /**
     * @param {object} parts What it draws on and from.
     * @param {HTMLCanvasElement} parts.canvas The canvas.
     * @param {HTMLElement} parts.readout The text readout beside it.
     * @param {SampleRing} parts.ring The samples.
     * @param {MarkList} parts.marks The marks.
     * @param {TimeView} parts.view The time window.
     */
    constructor({ canvas, readout, ring, marks, view }) {
        this.canvas = canvas;
        this.readout = readout;
        this.ring = ring;
        this.marks = marks;
        this.view = view;
        /** @type {string[]} The columns drawn, one lane each. */
        this.keys = [];
        /** The time the axis counts from, NaN until a sample arrives. */
        this.origin = Number.NaN;
        /** @type {number | null} The crosshair's position in the plot, CSS pixels. */
        this.crosshair = null;
        /** What the readout says when no sample is held. */
        this.emptyText = "No samples yet.";
        /** The display smoothing asked for, microseconds; 0 draws the mean per pixel column. */
        this.smoothingUs = 0;
        /** @type {import("./chart-math.js").SmoothingWindow} The window the last frame drew. */
        this.window = smoothingWindow(0, Number.NaN);
        /** @type {import("./chart-math.js").Noise | null} The INA228's averaging the samples were taken with. */
        this.noise = null;
        /** @type {LaneFigures[]} What each lane showed at the last frame. */
        this.figures = [];
        /** The crosshair's pixel column at the last frame, -1 with none. */
        this.cursorColumn = -1;
        /** The time at the middle of that column, microseconds. */
        this.cursorTime = Number.NaN;
        /** @type {import("./chart-math.js").Line[]} Each lane's line, its buffers kept from frame to frame. */
        this.lines = [];
        /** The last frame's pixel columns and a column's width, microseconds. */
        this.columns = 0;
        this.columnUs = Number.NaN;
        /** @type {import("./chart-math.js").LaneSnapshot[] | null} Each lane as drawn, kept only by a draw for an export. */
        this.snapshot = null;
        /** @type {ColumnExtrema[]} Each lane's reduction, its buffers kept from frame to frame. */
        this.extrema = [];
        this.gate = new FrameGate(FRAME_INTERVAL_MS);
        this.dirty = true;
        this.pending = false;
        /** Whether a person asked for what the next frame shows: its readout is announced. */
        this.asked = false;
        this.lastReadout = Number.NEGATIVE_INFINITY;
        /** @type {ReturnType<typeof setTimeout> | null} A readout held back by the interval, still to be written. */
        this.readoutTimer = null;
        /** @type {Theme | null} */
        this.theme = null;
        /** @type {[number, number][]} Each lane's value range as last drawn. */
        this.laneRanges = [];
        /** @type {{x: number, t0: number, t1: number} | null} A drag in progress. */
        this.drag = null;
        const context = canvas.getContext("2d");
        if (context === null) {
            throw new Error("no 2D context");
        }
        this.context = context;
        new ResizeObserver(() => this.invalidate()).observe(canvas);
        window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
            this.theme = null;
            this.invalidate();
        });
        this.wire();
    }

    /**
     * Which columns to draw, one lane each.
     *
     * @param {string[]} keys `i_na`, `v_uv`, ...
     */
    setChannels(keys) {
        this.keys = keys.filter((key) => channelOf(key) !== undefined);
        this.invalidate();
    }

    /**
     * The display smoothing: the line becomes the moving average over a window
     * of this length; the band and the samples stay as they are.
     *
     * @param {number} us The window, microseconds, 0 for the mean per pixel column.
     */
    setSmoothing(us) {
        this.smoothingUs = us;
        this.ask();
    }

    /** Forget the axis origin: the next sample starts it again. */
    resetOrigin() {
        this.origin = Number.NaN;
        this.invalidate();
    }

    /** Ask for a frame; it is drawn on the next animation frame the gate lets through. */
    invalidate() {
        this.dirty = true;
        if (!this.pending) {
            this.pending = true;
            requestAnimationFrame((now) => this.frame(now));
        }
    }

    /** A person asked for a change: the next frame is drawn and its readout announced. */
    ask() {
        this.asked = true;
        this.invalidate();
    }

    /**
     * One animation frame: draw if something changed and the gate allows,
     * then write the readout from what was drawn.
     *
     * @param {number} now The frame's time, milliseconds.
     */
    frame(now) {
        this.pending = false;
        if (!this.dirty) {
            return;
        }
        if (!this.gate.ready(now)) {
            this.pending = true;
            requestAnimationFrame((later) => this.frame(later));
            return;
        }
        this.dirty = false;
        const began = performance.now();
        this.draw();
        this.gate.spent(performance.now() - began);
        const asked = this.asked;
        this.asked = false;
        this.speak(now, asked);
    }

    /** @returns {Theme} The theme's colors and font, read once per theme change. */
    readTheme() {
        if (this.theme === null) {
            const style = getComputedStyle(this.canvas);
            const token = (/** @type {string} */ name) => style.getPropertyValue(name).trim();
            this.theme = {
                ink: token("--ink"),
                ink2: token("--ink-2"),
                ink3: token("--ink-3"),
                bg: token("--bg"),
                band: token("--trace-band"),
                rule: token("--rule"),
                ruleStrong: token("--rule-strong"),
                accent: token("--accent"),
                font: `${style.fontSize} ${style.fontFamily}`,
            };
        }
        return this.theme;
    }

    /** @returns {{width: number, height: number, plotX: number, plotW: number}} The plot's box, CSS pixels. */
    box() {
        const width = this.canvas.clientWidth;
        const height = this.canvas.clientHeight;
        return { width, height, plotX: AXIS_WIDTH, plotW: Math.max(1, width - AXIS_WIDTH - RIGHT_PAD) };
    }

    /** Draw the whole chart. */
    draw() {
        const { width, height, plotX, plotW } = this.box();
        const ratio = window.devicePixelRatio || 1;
        const canvas = this.canvas;
        const pixelsW = Math.max(1, Math.round(width * ratio));
        const pixelsH = Math.max(1, Math.round(height * ratio));
        if (canvas.width !== pixelsW || canvas.height !== pixelsH) {
            canvas.width = pixelsW;
            canvas.height = pixelsH;
        }
        const ctx = this.context;
        const theme = this.readTheme();
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, width, height);
        ctx.font = theme.font;
        ctx.textBaseline = "middle";

        if (Number.isNaN(this.origin) && this.ring.size > 0) {
            this.origin = this.ring.first;
        }
        this.view.track(this.ring.last);
        const { t0, t1 } = this.view;
        const columns = Math.max(1, Math.floor(plotW * ratio));
        const columnUs = (t1 - t0) / columns;
        this.columns = columns;
        this.columnUs = columnUs;
        this.window = smoothingWindow(this.smoothingUs, columnUs, columns);
        const caption = chartCaption(this.window, this.noise, this.bucketUs());
        if (canvas.dataset.caption !== caption) {
            canvas.dataset.caption = caption;
        }
        this.cursorColumn =
            this.crosshair === null ? -1 : Math.min(columns - 1, Math.max(0, Math.floor(this.crosshair * ratio)));
        this.cursorTime = this.cursorColumn < 0 ? Number.NaN : t0 + (this.cursorColumn + 0.5) * columnUs;
        this.figures = [];

        const first = laneBox(height, this.keys.length, 0);
        this.drawTimeAxis(theme, plotX, plotW, height, TOP_PAD, height - TIME_AXIS_HEIGHT);
        if (this.keys.length === 0 || this.ring.size === 0) {
            ctx.fillStyle = theme.ink2;
            ctx.textAlign = "center";
            const text = this.keys.length === 0 ? "No channel streams on this profile." : this.emptyText;
            ctx.fillText(text, plotX + plotW / 2, first.top + first.height / 2);
        }
        while (this.extrema.length < this.keys.length) {
            this.extrema.push(new ColumnExtrema());
        }
        reduceLanes(this.ring, this.keys, t0, t1, columns, this.window.half, this.extrema.slice(0, this.keys.length));
        this.keys.forEach((key, lane) => {
            const { top, height: laneH } = laneBox(height, this.keys.length, lane);
            this.drawLane(theme, key, lane, top, laneH, plotX, plotW, columns, ratio);
        });
        if (this.keys.length > 0 && this.ring.size > 0) {
            this.drawCaption(theme, first.top, plotX, plotW);
        }
        this.drawMarks(theme, plotX, plotW, height - TIME_AXIS_HEIGHT);
        this.drawCrosshair(theme, plotX, height, ratio);
    }

    /**
     * The shared time axis: ticks in seconds from the origin, and the grid.
     *
     * @param {Theme} theme The colors.
     * @param {number} plotX The plot's left edge.
     * @param {number} plotW Its width.
     * @param {number} height The canvas's height.
     * @param {number} top The lanes' top.
     * @param {number} bottom Their bottom.
     */
    drawTimeAxis(theme, plotX, plotW, height, top, bottom) {
        const ctx = this.context;
        const origin = Number.isNaN(this.origin) ? this.view.t0 : this.origin;
        const lo = (this.view.t0 - origin) / 1e6;
        const hi = (this.view.t1 - origin) / 1e6;
        const most = Math.max(2, Math.floor(plotW / 96));
        const { step, ticks } = niceTicks(lo, hi, most);
        const labels = tickLabels(ticks, step, "s");
        const right = this.canvas.clientWidth;
        ctx.textAlign = "center";
        ticks.forEach((tick, index) => {
            const x = plotX + ((tick - lo) / (hi - lo)) * plotW;
            ctx.strokeStyle = theme.rule;
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, bottom);
            ctx.stroke();
            ctx.fillStyle = theme.ink3;
            const half = ctx.measureText(labels[index]).width / 2;
            ctx.fillText(labels[index], Math.min(x, right - half - 2), height - TIME_AXIS_HEIGHT / 2);
        });
    }

    /**
     * One lane: its axis and grid, the band of each pixel column's minimum to
     * maximum, and the line over it, the column means or their moving average
     * (`this.window`). The reduction reaches half a window beyond each edge of
     * the view, so the line has no edge effect inside it.
     *
     * With no smoothing the line joins each column's mean to the next column
     * that holds a sample, however far (as the trace always has); with a
     * window it breaks where the window finds no sample (decision 11's gap
     * rule).
     *
     * @param {Theme} theme The colors.
     * @param {string} key The column.
     * @param {number} lane Which lane.
     * @param {number} top The lane's top.
     * @param {number} laneH Its height.
     * @param {number} plotX The plot's left edge.
     * @param {number} plotW Its width.
     * @param {number} columns The pixel columns.
     * @param {number} ratio Device pixels per CSS pixel.
     */
    drawLane(theme, key, lane, top, laneH, plotX, plotW, columns, ratio) {
        const ctx = this.context;
        const channel = /** @type {import("./chart-math.js").Channel} */ (channelOf(key));
        const { span } = this.window;
        const ext = this.extrema[lane];
        const line = movingMean(ext, span, this.lines[lane]);
        this.lines[lane] = line;
        this.snapshot?.push(laneSnapshot(key, ext, line));
        const range = ext.range();
        const [lo, hi] = paddedRange(range);
        this.laneRanges[lane] = [lo, hi];
        const at = this.cursorColumn + ext.offset;
        this.figures[lane] = {
            key,
            view: ext.viewStats(),
            cursor:
                this.cursorColumn < 0
                    ? null
                    : { line: line.mean[this.cursorColumn], n: ext.count[at], min: ext.min[at], max: ext.max[at] },
        };
        const y = (/** @type {number} */ raw) => top + laneH * (1 - (raw - lo) / (hi - lo));

        ctx.strokeStyle = theme.ruleStrong;
        ctx.lineWidth = 1;
        ctx.strokeRect(plotX, top, plotW, laneH);
        ctx.textAlign = "left";
        ctx.fillStyle = theme.ink2;
        ctx.fillText(channel.label, plotX + 8, top + 12);
        if (range === null) {
            return; // nothing in view: no scale to show
        }
        const most = Math.max(2, Math.floor(laneH / 36));
        const { step, ticks } = niceTicks(lo * channel.scale, hi * channel.scale, most);
        const labels = tickLabels(ticks, step, channel.unit);
        ctx.textAlign = "right";
        ticks.forEach((tick, index) => {
            const tickY = y(tick / channel.scale);
            ctx.strokeStyle = theme.rule;
            ctx.beginPath();
            ctx.moveTo(plotX, tickY);
            ctx.lineTo(plotX + plotW, tickY);
            ctx.stroke();
            ctx.fillStyle = theme.ink3;
            ctx.fillText(labels[index], plotX - 8, tickY);
        });

        ctx.save();
        ctx.beginPath();
        ctx.rect(plotX, top, plotW, laneH);
        ctx.clip();
        const columnW = 1 / ratio;
        // The band: one path of one rectangle per column, filled once, so no
        // two columns overlap and the faint fill keeps one opacity.
        ctx.beginPath();
        for (let column = 0; column < columns; column += 1) {
            const i = column + ext.offset;
            if (ext.count[i] === 0) {
                continue;
            }
            const yTop = y(ext.max[i]);
            ctx.rect(plotX + column * columnW, yTop, columnW, Math.max(columnW, y(ext.min[i]) - yTop));
        }
        ctx.fillStyle = theme.band;
        ctx.fill();
        // The line, through each column's middle; a run of it reaches half a
        // column beyond its first and last point, so one column alone shows.
        const join = span === 1;
        ctx.beginPath();
        let lastX = Number.NaN;
        let lastY = Number.NaN;
        for (let column = 0; column < columns; column += 1) {
            const value = line.mean[column];
            if (Number.isNaN(value)) {
                if (!join && !Number.isNaN(lastX)) {
                    ctx.lineTo(lastX + columnW / 2, lastY);
                    lastX = Number.NaN;
                }
                continue;
            }
            const x = plotX + (column + 0.5) * columnW;
            const pointY = y(value);
            if (Number.isNaN(lastX)) {
                ctx.moveTo(x - columnW / 2, pointY);
            }
            ctx.lineTo(x, pointY);
            lastX = x;
            lastY = pointY;
        }
        if (!Number.isNaN(lastX)) {
            ctx.lineTo(lastX + columnW / 2, lastY);
        }
        ctx.strokeStyle = theme.ink;
        ctx.lineWidth = LINE_WIDTH;
        ctx.lineJoin = "round";
        ctx.stroke();
        ctx.restore();
        ctx.lineWidth = 1;
    }

    /**
     * What the line is, written inside the plot at the first lane's top right
     * (below its label when the plot is too narrow for both), and below it,
     * when the INA228 averaged the samples, that it did; on the ground's color
     * so it reads over the data.
     *
     * @param {Theme} theme The colors.
     * @param {number} top The first lane's top.
     * @param {number} plotX The plot's left edge.
     * @param {number} plotW Its width.
     */
    drawCaption(theme, top, plotX, plotW) {
        const ctx = this.context;
        const text = this.window.caption;
        const width = ctx.measureText(text).width;
        const label = ctx.measureText(/** @type {any} */ (channelOf(this.keys[0])).label).width;
        const right = plotX + plotW - 8;
        const y = right - width < plotX + 8 + label + 12 ? top + 28 : top + 12;
        ctx.textAlign = "right";
        for (const [line, at] of /** @type {[string, number][]} */ ([
            [text, y],
            [noiseCaption(this.noise), y + 16],
        ])) {
            const wide = ctx.measureText(line).width;
            if (wide > 0) {
                ctx.fillStyle = theme.bg;
                ctx.fillRect(right - wide - 4, at - 8, wide + 8, 16);
                ctx.fillStyle = theme.ink2;
                ctx.fillText(line, right, at);
            }
        }
    }

    /**
     * The marks in the window: a line across the lanes and the label above.
     *
     * @param {Theme} theme The colors.
     * @param {number} plotX The plot's left edge.
     * @param {number} plotW Its width.
     * @param {number} bottom The lanes' bottom.
     */
    drawMarks(theme, plotX, plotW, bottom) {
        const ctx = this.context;
        ctx.strokeStyle = theme.ink3;
        ctx.fillStyle = theme.ink2;
        ctx.textAlign = "left";
        ctx.setLineDash([4, 4]);
        for (const mark of this.marks.between(this.view.t0, this.view.t1)) {
            const x = plotX + this.view.xOf(mark.t, plotW);
            ctx.beginPath();
            ctx.moveTo(x, TOP_PAD);
            ctx.lineTo(x, bottom);
            ctx.stroke();
            ctx.fillText(mark.label, x + 4, TOP_PAD / 2);
        }
        ctx.setLineDash([]);
    }

    /**
     * The crosshair: a line in the accent through the middle of its column,
     * and on each lane a dot on the line with the line's value beside it.
     *
     * @param {Theme} theme The colors.
     * @param {number} plotX The plot's left edge.
     * @param {number} height The canvas's height.
     * @param {number} ratio Device pixels per CSS pixel.
     */
    drawCrosshair(theme, plotX, height, ratio) {
        if (this.cursorColumn < 0) {
            return;
        }
        const ctx = this.context;
        const x = plotX + (this.cursorColumn + 0.5) / ratio;
        const right = plotX + this.box().plotW;
        ctx.strokeStyle = theme.accent;
        ctx.beginPath();
        ctx.moveTo(x, TOP_PAD);
        ctx.lineTo(x, height - TIME_AXIS_HEIGHT);
        ctx.stroke();
        this.figures.forEach((figures, lane) => {
            const value = figures.cursor?.line ?? Number.NaN;
            if (Number.isNaN(value)) {
                return;
            }
            const [lo, hi] = this.laneRanges[lane] ?? [0, 1];
            const { top, height: laneH } = laneBox(height, this.keys.length, lane);
            const dotY = top + laneH * (1 - (value - lo) / (hi - lo));
            ctx.fillStyle = theme.accent;
            ctx.beginPath();
            ctx.arc(x, dotY, 3, 0, 2 * Math.PI);
            ctx.fill();
            const text = formatRaw(value, /** @type {any} */ (channelOf(figures.key)));
            const width = ctx.measureText(text).width;
            const left = x + 8 + width > right ? x - 8 - width : x + 8;
            const labelY = Math.max(top + 10, dotY - 12);
            ctx.fillStyle = theme.bg;
            ctx.fillRect(left - 3, labelY - 8, width + 6, 16);
            ctx.fillStyle = theme.accent;
            ctx.textAlign = "left";
            ctx.fillText(text, left, labelY);
        });
    }

    /**
     * Write the text readout from what the last frame drew: the crosshair's
     * column, or the newest sample, with the figures of the window in view; at
     * most once a second unless asked now, and a readout the interval held
     * back is written once it has passed, so a chart that stops changing
     * never keeps an older one.
     *
     * The readout is a live region that a screen reader announces only when a
     * person asked for it (a key, a pan, a zoom, the crosshair moved, the
     * smoothing changed: `force`): the update a capture makes once a second by
     * itself is written with the region silent, or it would be announced for
     * the whole capture over everything else the page says (WCAG 2.2.2).
     *
     * @param {number} now The time now, milliseconds.
     * @param {boolean} force Write it whatever the interval, and announce it.
     */
    speak(now, force) {
        if (!force && this.crosshair === null && now - this.lastReadout < READOUT_INTERVAL_MS) {
            if (this.readoutTimer === null) {
                this.readoutTimer = setTimeout(() => {
                    this.readoutTimer = null;
                    this.speak(performance.now(), false);
                }, READOUT_INTERVAL_MS - (now - this.lastReadout));
            }
            return;
        }
        if (this.readoutTimer !== null) {
            clearTimeout(this.readoutTimer);
            this.readoutTimer = null;
        }
        this.lastReadout = now;
        const text = this.describe();
        if (text === this.readout.textContent) {
            return;
        }
        this.readout.setAttribute("aria-live", force ? "polite" : "off");
        this.readout.textContent = text;
    }

    /** @returns {string} What the readout says of the last frame. */
    describe() {
        const ring = this.ring;
        if (ring.size === 0) {
            return this.emptyText;
        }
        const origin = Number.isNaN(this.origin) ? ring.first : this.origin;
        const seconds = (/** @type {number} */ t) => formatQuantity((t - origin) / 1e6, "s", 5, 0);
        const caption = `line ${chartCaption(this.window, this.noise, this.bucketUs())}`;
        const lanes = this.keys.map((key, lane) => {
            const channel = /** @type {import("./chart-math.js").Channel} */ (channelOf(key));
            const raw = (/** @type {number} */ value) => (Number.isNaN(value) ? "-" : formatRaw(value, channel));
            const figures = this.figures[lane];
            const view = figures?.view;
            const inView =
                view === undefined || view.n === 0
                    ? "nothing in view"
                    : `in view mean ${raw(view.mean)}, RMS ${raw(view.rms)}, min ${raw(view.min)}, max ${raw(view.max)}`;
            return { channel, raw, inView, cursor: figures?.cursor ?? null };
        });
        if (this.cursorColumn >= 0) {
            const parts = lanes.map(({ channel, raw, inView, cursor }) => {
                const value = cursor === null || Number.isNaN(cursor.line) ? "no line here" : raw(cursor.line);
                const column =
                    cursor === null || !(cursor.n > 0)
                        ? "no sample in this column"
                        : `column ${raw(cursor.min)} to ${raw(cursor.max)}`;
                return `${channel.label} ${value} (${column}, ${inView})`;
            });
            return `Crosshair at ${seconds(this.cursorTime)}, ${caption}: ${parts.join("; ")}.`;
        }
        const newest = ring.size - 1;
        const parts = this.keys.map((key, lane) => {
            const { channel, raw, inView } = lanes[lane];
            return `${channel.label} ${raw(ring.valueAt(key, newest))} (${inView})`;
        });
        const length = formatQuantity((this.view.t1 - this.view.t0) / 1e6, "s", 3);
        const follow = this.view.follow ? "following the newest sample" : "held";
        return `Newest at ${seconds(ring.timeAt(newest))}, window ${length}, ${follow}, ${caption}: ${parts.join("; ")}.`;
    }

    /**
     * Say how the INA228 averaged the samples shown.
     *
     * @param {import("./chart-math.js").Noise | null} noise Their averaging, null when unknown.
     */
    setNoise(noise) {
        this.noise = noise;
        this.invalidate();
    }

    /**
     * Save the view: draw a frame now, and make from it a PNG of the canvas
     * and the CSV of what it plots, in the page, asking serve for nothing.
     *
     * The PNG is the canvas's size, its pixels laid over the theme's ground so
     * it reads outside the page, with a line over the lanes, on the strip the
     * marks' labels use, naming what it is of (`viewTitle()`); it carries the
     * CSV's header as text chunks. The caption that names the filter is drawn
     * inside the plot already.
     *
     * @param {import("./chart-math.js").ViewSubject} subject What the view is of.
     * @returns {ViewExport} The two files.
     */
    exportView(subject) {
        this.snapshot = [];
        this.draw();
        const lanes = this.snapshot;
        this.snapshot = null;
        const { t0, t1 } = this.view;
        /** @type {import("./chart-math.js").ViewFrame} */
        const frame = {
            t0,
            t1,
            columns: this.columns,
            columnUs: this.columnUs,
            choiceUs: this.smoothingUs,
            window: this.window,
            bucketUs: this.bucketUs(),
            lanes,
        };
        const header = viewHeader(frame, subject);
        const origin = Number.isNaN(this.origin) ? t0 : this.origin;
        const title = viewTitle(subject, t0, t1, origin);
        const image = document.createElement("canvas");
        image.width = this.canvas.width;
        image.height = this.canvas.height;
        const ctx = image.getContext("2d");
        if (ctx === null) {
            throw new Error("no 2D context");
        }
        const theme = this.readTheme();
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, image.width, image.height);
        ctx.drawImage(this.canvas, 0, 0);
        const ratio = window.devicePixelRatio || 1;
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.font = theme.font;
        ctx.textBaseline = "middle";
        const { plotX, plotW } = this.box();
        const right = plotX + plotW;
        const width = ctx.measureText(title).width;
        ctx.fillStyle = theme.bg;
        ctx.fillRect(Math.max(0, right - width - 4), 1, width + 8, TOP_PAD - 2);
        ctx.fillStyle = theme.ink2;
        ctx.textAlign = "right";
        ctx.fillText(title, right, TOP_PAD / 2);
        const encoded = bytesOf(image.toDataURL().split(",")[1]); // PNG, the default
        const pairs = /** @type {[string, string][]} */ ([
            ["Title", `Kleidos meter view: ${subject.source}, ${subject.instrument}`],
            ["Description", title],
            ["Software", "Kleidos meter panel"],
            ...header,
        ]);
        const png = `data:image/png;base64,${base64Of(pngWithText(encoded, pairs))}`;
        return { png, csv: viewCsv(frame, subject) };
    }

    /**
     * Move the crosshair, showing it when hidden; the next frame's readout is announced.
     *
     * @param {number | null} x Its position in the plot, CSS pixels, null to hide it.
     */
    point(x) {
        const { plotW } = this.box();
        this.crosshair = x === null ? null : Math.min(plotW, Math.max(0, x));
        this.ask();
    }

    /** Listen to the pointer and the keyboard. */
    wire() {
        const canvas = this.canvas;
        canvas.addEventListener("keydown", (event) => this.key(event));
        canvas.addEventListener("pointerdown", (event) => {
            canvas.setPointerCapture(event.pointerId);
            this.drag = { x: event.offsetX, t0: this.view.t0, t1: this.view.t1 };
        });
        canvas.addEventListener("pointermove", (event) => {
            const { plotX, plotW } = this.box();
            if (this.drag !== null && event.buttons !== 0) {
                const share = (this.drag.x - event.offsetX) / plotW;
                const span = this.drag.t1 - this.drag.t0;
                if (Math.abs(this.drag.x - event.offsetX) > 2) {
                    this.view.follow = false;
                    this.view.t0 = this.drag.t0 + span * share;
                    this.view.t1 = this.drag.t1 + span * share;
                }
            }
            this.point(event.offsetX - plotX);
        });
        canvas.addEventListener("pointerup", () => {
            this.drag = null;
        });
        canvas.addEventListener("pointerleave", () => {
            this.drag = null;
            this.point(null);
        });
        canvas.addEventListener("dblclick", () => this.followAgain());
        canvas.addEventListener(
            "wheel",
            (event) => {
                const { plotX, plotW } = this.box();
                if (event.ctrlKey || event.metaKey) {
                    event.preventDefault();
                    const anchor = (event.offsetX - plotX) / plotW;
                    this.view.zoom(event.deltaY < 0 ? 0.8 : 1.25, anchor, this.shortestUs());
                    this.invalidate();
                } else if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
                    event.preventDefault();
                    this.view.pan(event.deltaX / plotW);
                    this.invalidate();
                }
            },
            { passive: false },
        );
    }

    /** @returns {number} A run bucket's width when the ring holds a run's buckets, else 0. */
    bucketUs() {
        return this.ring.bucketed ? this.ring.bucketUs : 0;
    }

    /**
     * The shortest window a zoom may reach: 1 ms, and over a run's buckets
     * one bucket per pixel column, the finest the run was fetched at, so no
     * column draws a bucket's mean as though it were its own.
     *
     * @returns {number} Microseconds.
     */
    shortestUs() {
        return Math.max(1000, this.bucketUs() * this.columns);
    }

    /** Follow the newest sample again, at the window's length. */
    followAgain() {
        this.view.setWindow(this.view.windowUs, this.ring.last);
        this.ask();
    }

    /**
     * The keyboard's equivalents of the pointer's gestures.
     *
     * @param {KeyboardEvent} event The key.
     */
    key(event) {
        const { plotW } = this.box();
        const anchor = this.crosshair === null ? 0.5 : this.crosshair / plotW;
        let handled = true;
        if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && event.shiftKey) {
            const from = this.crosshair ?? plotW / 2;
            this.point(from + (event.key === "ArrowLeft" ? -1 : 1) * Math.max(1, plotW / 100));
        } else if (event.key === "ArrowLeft") {
            this.view.pan(-0.1);
        } else if (event.key === "ArrowRight") {
            this.view.pan(0.1);
        } else if (event.key === "+" || event.key === "=") {
            this.view.zoom(0.5, anchor, this.shortestUs());
        } else if (event.key === "-" || event.key === "_") {
            this.view.zoom(2, anchor, this.shortestUs());
        } else if (event.key === "f" || event.key === "F" || event.key === "Home") {
            this.followAgain();
        } else if (event.key === "Escape") {
            this.point(null);
        } else {
            handled = false;
        }
        if (handled) {
            event.preventDefault();
            this.ask();
        }
    }
}
