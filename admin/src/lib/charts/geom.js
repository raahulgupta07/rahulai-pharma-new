// Chart geometry. Hand-rolled SVG only — there is no charting library in this
// project and none is being added (contract §7).
//
// TWO layout modes live here, and the difference is the whole point.
//
//   * PIXEL mode (plotBox/xIn/yIn/niceScale) — the viewBox is built at the
//     container's MEASURED width, so one view unit is one CSS pixel. 11px text
//     is 11px, a 1px gridline is 1px, and the height is whatever you asked for.
//     Line and bar charts use this.
//   * FIXED mode (PX0…VIEW, xAt/yAt/ticks) — a 680×170 viewBox scaled by the
//     browser to the container width. Kept only for charts that are pinned to a
//     size in CSS (the donut is h-[132px] w-[132px], so it never scales).
//
// Fixed mode used to be the only mode, and it was wrong for anything full-width:
// measured on the running console, a line chart rendered 1128×303 at a 1440px
// window and 1608×423 at 1920px — the browser scaling the entire drawing,
// TYPE INCLUDED, so a label declared at 10.5px landed on screen at 17.4px and
// 24.8px respectively. A wider monitor did not show more chart, it showed a
// bigger one. Do not add a new full-width chart in fixed mode.
//
// The invariant that matters: a value that is not a finite number is NOT
// plotted. It is not zero, and drawing it on the baseline would claim it was.

import { isNum } from './format.js';

export const PX0 = 34; // left edge of the plot (y-axis labels live left of it)
export const PX1 = 654;
export const PY0 = 10; // top of the plot
export const PY1 = 148; // baseline
export const AXIS_Y = 164; // x-axis label baseline
export const VIEW = '0 0 680 170';

/** Five gridlines, evenly spaced, top to bottom. */
export const GRID_ROWS = [0, 1, 2, 3, 4].map((i) => PY0 + ((PY1 - PY0) * i) / 4);

export function xAt(i, n) {
  if (n <= 1) return (PX0 + PX1) / 2;
  return PX0 + (i * (PX1 - PX0)) / (n - 1);
}

export function yAt(v, max) {
  if (!isNum(v)) return null;
  const m = max > 0 ? max : 1;
  return PY1 - (Math.max(0, v) / m) * (PY1 - PY0);
}

/** Largest finite value across several series, floored at 1 so nothing divides by zero. */
export function maxOf(...lists) {
  let m = 0;
  for (const list of lists) {
    for (const v of list ?? []) if (isNum(v) && v > m) m = v;
  }
  return m > 0 ? m : 1;
}

/** Tick VALUES top-to-bottom for the five gridlines. */
export function ticks(max) {
  return [4, 3, 2, 1, 0].map((i) => (max * i) / 4);
}

/**
 * Straight polyline through the plotted points, split at every gap.
 * A gap is a missing measurement; bridging it would invent the missing days.
 */
export function polySegments(points) {
  const segs = [];
  let cur = [];
  for (const p of points) {
    if (p.y == null) {
      if (cur.length) segs.push(cur);
      cur = [];
    } else cur.push(p);
  }
  if (cur.length) segs.push(cur);
  return segs;
}

const f = (n) => n.toFixed(1);

export function linePath(seg) {
  if (!seg.length) return '';
  if (seg.length === 1) return `M${f(seg[0].x - 4)},${f(seg[0].y)} L${f(seg[0].x + 4)},${f(seg[0].y)}`;
  return seg.map((p, i) => `${i ? 'L' : 'M'}${f(p.x)},${f(p.y)}`).join(' ');
}

/**
 * Area fill under a segment. Reserved for VOLUME series — a rate drawn with an
 * area fill reads as a quantity, which is the single most common way a latency
 * or hit-rate chart misleads.
 */
export function areaPath(seg) {
  if (seg.length < 2) return '';
  return `${linePath(seg)} L${f(seg[seg.length - 1].x)},${PY1} L${f(seg[0].x)},${PY1} Z`;
}

/** Column geometry shared by every stacked-bar chart. */
export function columns(n) {
  const slot = (PX1 - PX0) / Math.max(1, n);
  const w = Math.min(56, Math.max(4, slot * 0.62));
  return { slot, w, xOf: (i) => PX0 + slot * (i + 0.5) - w / 2, cxOf: (i) => PX0 + slot * (i + 0.5) };
}

/** At most 8 x-axis labels however long the range is. */
export const labelEvery = (n) => Math.max(1, Math.ceil(n / 8));

/**
 * Sparkline path over a 150×30 box.
 *
 * `range || 1` is load-bearing: a flat series (every value identical) makes
 * max-min exactly 0, and a naive sparkline divides by it. Flat series are
 * normal in a young product — this one has days with one value repeated — so
 * the guard is hit in ordinary use, not only in theory. A flat series draws as
 * a centred straight line, which is the honest picture.
 */
export function sparkPath(values) {
  const pts = (values ?? []).filter(isNum);
  if (pts.length < 2) return null;
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const range = max - min;
  const n = pts.length;
  const d = pts.map((v, i) => {
    const x = (i * 150) / (n - 1);
    const y = range === 0 ? 15 : 28 - ((v - min) / (range || 1)) * 26;
    return `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
  });
  const line = d.join(' ');
  return { line, area: `${line} L150 30 L0 30 Z` };
}

/** Donut arc path, r_outer 62 / r_inner 44 inside a 132×132 box. */
export function donutArc(startFrac, endFrac) {
  const R = 62;
  const r = 44;
  const C = 66;
  const a0 = startFrac * Math.PI * 2 - Math.PI / 2;
  const a1 = endFrac * Math.PI * 2 - Math.PI / 2;
  const big = endFrac - startFrac > 0.5 ? 1 : 0;
  const p = (ang, rad) => `${(C + rad * Math.cos(ang)).toFixed(2)} ${(C + rad * Math.sin(ang)).toFixed(2)}`;
  return `M${p(a0, R)} A${R} ${R} 0 ${big} 1 ${p(a1, R)} L${p(a1, r)} A${r} ${r} 0 ${big} 0 ${p(a0, r)} Z`;
}

/**
 * Single-hue heatmap ramp. Never a rainbow: a rainbow ramp implies category
 * boundaries between the colours, and a magnitude has none.
 * Returns `null` for an unknown cell — the caller draws it as an empty cell,
 * not as the coldest colour, because "no data" is not "zero".
 */
export function heatStyle(v, max) {
  if (!isNum(v)) return { background: 'var(--color-surface-2)', color: 'var(--color-ink-3)', known: false };
  const t = max > 0 ? Math.min(1, v / max) : 0;
  // ONE label colour, and a ramp that stays light enough to carry it.
  //
  // This used to run the mix from 8% to 100% accent and flip the label to
  // on-accent above t=0.55. Measured on the rendered pixels, that left a band
  // of cells where NEITHER label colour reached 4.5:1 — on the old blue accent
  // every cell from t=0.60 to t=0.90 failed, the worst at 2.79:1. A two-colour
  // threshold on a continuous single-hue ramp always has such a band: the
  // mid-tone is too dark for the light label and too light for the dark one,
  // and some cell will always land there.
  //
  // Capping the ramp at 50% accent removes the band instead of moving it. The
  // darkest cell now measures 5.27:1 against --color-ink and the lightest
  // 13.11:1, so every cell passes with one colour and no branch. The gradient
  // is shorter but still reads as one — and a heatmap's job is relative
  // magnitude, which a shorter ramp still carries.
  return {
    background: `color-mix(in srgb, var(--color-accent) ${(8 + t * 42).toFixed(0)}%, var(--color-accent-soft))`,
    color: 'var(--color-ink)',
    known: true
  };
}


// ---------------------------------------------------------------- PIXEL MODE

/**
 * Plot box for a chart drawn at its container's own pixel size.
 *
 * `width` is measured (bind:clientWidth), `height` is chosen by the caller and
 * does NOT follow the width — that coupling is what made these charts 400px
 * tall on a wide monitor. Padding leaves room for the axis labels, which are
 * drawn at their declared size because nothing is being scaled.
 */
export function plotBox(width, height = 200, pad = {}) {
  const W = Math.max(220, Math.round(width || 0) || 640);
  const H = Math.max(120, Math.round(height));
  const PL = pad.left ?? 42;
  const PR = pad.right ?? 14;
  const PT = pad.top ?? 12;
  const PB = pad.bottom ?? 26;
  return { W, H, PL, PR, PT, PB, x0: PL, x1: W - PR, y0: PT, y1: H - PB };
}

/**
 * Axis scale with human tick values.
 *
 * `ticks(max)` divides the max into quarters, which is right for a large range
 * and absurd for a small one: with max = 1 it produces 1, 0.75, 0.5, 0.25, 0,
 * and an integer formatter renders that as "1, 1, 1, 0, 0" — three identical
 * labels on a chart whose only value is 1. This walks a 1/2/2.5/5/10 ladder
 * instead, so the steps are whole numbers people recognise, and it returns
 * FEWER rows for a small range rather than five lines through empty space.
 */
export function niceScale(max, maxRows = 5) {
  const m = isNum(max) && max > 0 ? max : 1;
  if (m <= maxRows - 1) {
    const top = Math.max(1, Math.ceil(m));
    const rows = [];
    for (let v = 0; v <= top; v += 1) rows.push(v);
    return { top, step: 1, rows };
  }
  const pow = Math.pow(10, Math.floor(Math.log10(m)));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    const step = mult * pow / 2;
    // A step of 1 or more must be a WHOLE number. The 2.5 rung is what puts
    // 12.5 on the ladder for a range around 41, and the axis then prints its
    // rounded labels — 0, 13, 25, 38, 50 — over gridlines that are actually at
    // 12.5 and 37.5. A reader measuring a bar against "38" reads a number that
    // is off by half a step, and nothing on screen says so. The guard is
    // written as `>= 1` rather than unconditionally because a sub-1 step is a
    // real fraction the labels can print in full; today nothing reaches it (a
    // max below `maxRows - 1` is served by the integer branch above), and that
    // is the branch to revisit if a cost axis ever needs 0.25 steps.
    if (step >= 1 && !Number.isInteger(step)) continue;
    if (m / step <= maxRows - 1) {
      const top = Math.ceil(m / step) * step;
      const rows = [];
      for (let v = 0; v <= top + step / 1000; v += step) rows.push(+v.toFixed(6));
      return { top, step, rows };
    }
  }
  const step = m / (maxRows - 1);
  const rows = [];
  for (let i = 0; i < maxRows; i += 1) rows.push(step * i);
  return { top: m, step, rows };
}

/** X of bucket `i` of `n`, inside a measured box. */
export function xIn(i, n, box) {
  if (n <= 1) return (box.x0 + box.x1) / 2;
  return box.x0 + (i * (box.x1 - box.x0)) / (n - 1);
}

/** Y of value `v` against `top`, inside a measured box. `null` for unknown. */
export function yIn(v, top, box) {
  if (!isNum(v)) return null;
  const t = top > 0 ? top : 1;
  return box.y1 - (Math.max(0, v) / t) * (box.y1 - box.y0);
}

/** Column geometry inside a measured box (bars). */
export function columnsIn(n, box) {
  const slot = (box.x1 - box.x0) / Math.max(1, n);
  const w = Math.min(56, Math.max(3, slot * 0.62));
  return { slot, w, xOf: (i) => box.x0 + slot * (i + 0.5) - w / 2, cxOf: (i) => box.x0 + slot * (i + 0.5) };
}

/**
 * Which bucket is the pointer over?
 *
 * Takes the whole plot width as the hit area rather than the dots. The old
 * charts had no readout at all beyond an SVG <title> on each 3px dot — a ~1s
 * browser delay, one series at a time, and nothing on touch.
 */
export function bucketAt(clientX, svgEl, n, box) {
  const r = svgEl.getBoundingClientRect();
  if (!r.width) return 0;
  const px = (clientX - r.left) * (box.W / r.width);
  const stepPx = (box.x1 - box.x0) / Math.max(1, n - 1);
  const i = Math.round((px - box.x0) / (stepPx || 1));
  return Math.max(0, Math.min(n - 1, i));
}

/** Same, for bar charts: buckets are slots, not points. */
export function slotAt(clientX, svgEl, n, box) {
  const r = svgEl.getBoundingClientRect();
  if (!r.width) return 0;
  const px = (clientX - r.left) * (box.W / r.width);
  const slot = (box.x1 - box.x0) / Math.max(1, n);
  const i = Math.floor((px - box.x0) / (slot || 1));
  return Math.max(0, Math.min(n - 1, i));
}

/** Line/area path builders for a measured box (segments already split on gaps). */
export function linePathPx(seg) {
  if (!seg.length) return '';
  if (seg.length === 1) return `M${f(seg[0].x - 4)},${f(seg[0].y)} L${f(seg[0].x + 4)},${f(seg[0].y)}`;
  return seg.map((p, i) => `${i ? 'L' : 'M'}${f(p.x)},${f(p.y)}`).join(' ');
}

export function areaPathPx(seg, box) {
  if (seg.length < 2) return '';
  return `${linePathPx(seg)} L${f(seg[seg.length - 1].x)},${box.y1} L${f(seg[0].x)},${box.y1} Z`;
}

/** How many x labels fit at ~64px each, without overlapping. */
export function labelEveryPx(n, box) {
  const per = (box.x1 - box.x0) / Math.max(1, n);
  return Math.max(1, Math.ceil(64 / Math.max(1, per)));
}
