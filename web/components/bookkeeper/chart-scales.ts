/**
 * Geometry for the spending chart. Pure, DOM-free, and deliberately separate
 * from the component that draws it.
 *
 * The chart is hand-rolled inline SVG rather than a charting library, which is
 * a decision with a cost: the scales, the stacking and the tick selection are
 * ours to get right. Keeping them here means they can be tested as arithmetic
 * instead of eyeballed as pixels, which is the only reason hand-rolling is a
 * reasonable trade at all.
 *
 * **Money is summed in integer cents, never floats.** A stacked column whose
 * segments are laid out in floating point will not add up to the total printed
 * above it — 0.1 + 0.2 is visible as a one-cent disagreement between a bar and
 * its own label, and in a ledger that reads as a bug in the ledger. Cents are
 * exact for every figure this app will ever chart, and the conversion to pixels
 * happens once, at the end, where imprecision is just anti-aliasing.
 */

import type { SpendingPoint, SpendingReportData } from "./types";

/**
 * Where the tail of the envelope list goes.
 *
 * Past this many series a categorical palette stops working: an eighth and
 * ninth hue are not distinguishable from the ones already on screen under
 * common colour-vision deficiencies, and generating more is the standard way
 * charts become unreadable. The smallest envelopes fold into one grey band
 * instead, which is honest — it still carries their spend, it just stops
 * claiming the reader can tell them apart.
 */
export const MAX_SERIES = 7;
export const OTHER_LABEL = "Other";

/** Viewport of the generated SVG. Fixed, with the element scaled by CSS. */
export const VIEW_WIDTH = 760;
export const VIEW_HEIGHT = 320;
export const MARGIN = { bottom: 40, left: 60, right: 16, top: 16 };

export const PLOT_WIDTH = VIEW_WIDTH - MARGIN.left - MARGIN.right;
export const PLOT_HEIGHT = VIEW_HEIGHT - MARGIN.top - MARGIN.bottom;

/** Bars are capped rather than filling their band — the leftover is air. */
export const MAX_BAR_WIDTH = 24;
/** The surface-coloured gap that separates touching stack segments. */
export const SEGMENT_GAP = 2;
export const CORNER_RADIUS = 4;

/** A decimal string as exact integer cents. Non-numeric input reads as zero. */
export function toCents(amount: string): number {
  const value = Number(amount);
  return Number.isFinite(value) ? Math.round(value * 100) : 0;
}

export function centsToAmount(cents: number): string {
  return (cents / 100).toFixed(2);
}

export type ChartSeries = {
  envelope: string;
  /** Palette slot, assigned once by total spend and never by rank within a column. */
  slot: number;
  totalCents: number;
  isOther: boolean;
};

export type StackSegment = {
  envelope: string;
  slot: number;
  cents: number;
  y: number;
  height: number;
  /** Only the topmost segment of a column carries the rounded cap. */
  roundedTop: boolean;
};

export type ChartColumn = {
  period: string;
  x: number;
  width: number;
  totalCents: number;
  segments: StackSegment[];
};

export type ChartLayout = {
  series: ChartSeries[];
  columns: ChartColumn[];
  ticks: { value: number; y: number }[];
  maxCents: number;
  /** True when there is nothing to draw, so the caller shows an empty state. */
  isEmpty: boolean;
};

/**
 * Rank envelopes by total spend across the window and assign palette slots.
 *
 * Slots are assigned here, once, from the whole-window total — never per
 * column. If a series' colour were derived from its rank inside each column,
 * the same envelope would change colour from month to month, and a reader who
 * learned "groceries is blue" would be misled by the next bar.
 */
export function buildSeries(points: SpendingPoint[]): ChartSeries[] {
  const totals = new Map<string, number>();
  for (const point of points) {
    const cents = toCents(point.amount);
    totals.set(point.envelope, (totals.get(point.envelope) ?? 0) + cents);
  }

  const ranked = [...totals.entries()]
    .map(([envelope, totalCents]) => ({ envelope, totalCents }))
    // Ties break on name so the palette is stable across renders of the same
    // data; an unstable sort here would repaint the chart on every poll.
    .sort(
      (a, b) =>
        b.totalCents - a.totalCents || a.envelope.localeCompare(b.envelope)
    );

  if (ranked.length <= MAX_SERIES) {
    return ranked.map((entry, index) => ({
      envelope: entry.envelope,
      isOther: false,
      slot: index,
      totalCents: entry.totalCents,
    }));
  }

  const named = ranked.slice(0, MAX_SERIES - 1);
  const tail = ranked.slice(MAX_SERIES - 1);
  const series: ChartSeries[] = named.map((entry, index) => ({
    envelope: entry.envelope,
    isOther: false,
    slot: index,
    totalCents: entry.totalCents,
  }));
  series.push({
    envelope: OTHER_LABEL,
    isOther: true,
    slot: MAX_SERIES - 1,
    totalCents: tail.reduce((sum, entry) => sum + entry.totalCents, 0),
  });
  return series;
}

/** Which series a given envelope's spend is drawn as, after the tail is folded. */
function seriesIndexFor(series: ChartSeries[], envelope: string): number {
  const exact = series.findIndex(
    (candidate) => !candidate.isOther && candidate.envelope === envelope
  );
  if (exact !== -1) {
    return exact;
  }
  return series.findIndex((candidate) => candidate.isOther);
}

/**
 * Round a maximum up to a clean axis bound.
 *
 * Ticks land on 1/2/2.5/5 × 10ⁿ so the axis reads 0 / 200 / 400 rather than
 * 0 / 187 / 374. The reader is meant to be able to estimate a bar's value from
 * the grid without arithmetic.
 */
export function niceTicks(
  maxCents: number,
  count = 4
): { value: number; y: number }[] {
  if (maxCents <= 0) {
    return [{ value: 0, y: MARGIN.top + PLOT_HEIGHT }];
  }

  const rough = maxCents / count;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step =
    [1, 2, 2.5, 5, 10]
      .map((multiple) => multiple * magnitude)
      .find((candidate) => candidate >= rough) ?? 10 * magnitude;

  const bound = Math.ceil(maxCents / step) * step;
  const ticks: { value: number; y: number }[] = [];
  for (let value = 0; value <= bound + step / 2; value += step) {
    ticks.push({
      value,
      y: MARGIN.top + PLOT_HEIGHT - (value / bound) * PLOT_HEIGHT,
    });
  }
  return ticks;
}

/**
 * Lay out the whole chart.
 *
 * Columns come from `report.periods` rather than from the points, so a month
 * with no spend keeps its slot on the axis. Dropping empty periods would
 * silently compress the time axis and make a gap in spending look like it
 * never happened.
 */
export function buildLayout(report: SpendingReportData): ChartLayout {
  const series = buildSeries(report.points);
  const periods =
    report.periods.length > 0
      ? report.periods
      : [...new Set(report.points.map((point) => point.period))].sort();

  // period → series index → cents
  const grid = new Map<string, number[]>();
  for (const period of periods) {
    grid.set(period, new Array(series.length).fill(0));
  }
  for (const point of report.points) {
    const row = grid.get(point.period);
    if (!row) {
      continue;
    }
    const index = seriesIndexFor(series, point.envelope);
    if (index !== -1) {
      row[index] += toCents(point.amount);
    }
  }

  const columnTotals = periods.map((period) =>
    (grid.get(period) ?? []).reduce((sum, cents) => sum + cents, 0)
  );
  const maxCents = Math.max(0, ...columnTotals);
  const ticks = niceTicks(maxCents);
  const bound = ticks.at(-1)?.value ?? 0;

  const band = periods.length > 0 ? PLOT_WIDTH / periods.length : PLOT_WIDTH;
  const barWidth = Math.min(MAX_BAR_WIDTH, band * 0.6);

  const columns: ChartColumn[] = periods.map((period, columnIndex) => {
    const row = grid.get(period) ?? [];
    const segments: StackSegment[] = [];
    let cursor = MARGIN.top + PLOT_HEIGHT;

    // Drawn bottom-up in series order, so the palette reads consistently from
    // the baseline in every column.
    const drawn = row
      .map((cents, index) => ({ cents, index }))
      .filter((entry) => entry.cents > 0);

    drawn.forEach((entry, position) => {
      const rawHeight = bound > 0 ? (entry.cents / bound) * PLOT_HEIGHT : 0;
      const isTop = position === drawn.length - 1;
      // The gap is taken out of the segment rather than added between them, so
      // the stack still ends exactly at the column's true total height.
      const height = Math.max(0, rawHeight - (isTop ? 0 : SEGMENT_GAP));
      cursor -= rawHeight;
      segments.push({
        cents: entry.cents,
        envelope: series[entry.index].envelope,
        height,
        roundedTop: isTop,
        slot: series[entry.index].slot,
        y: cursor,
      });
    });

    return {
      period,
      segments,
      totalCents: columnTotals[columnIndex],
      width: barWidth,
      x: MARGIN.left + band * columnIndex + (band - barWidth) / 2,
    };
  });

  return {
    columns,
    isEmpty: maxCents === 0,
    maxCents,
    series,
    ticks,
  };
}

/**
 * A rounded-top, square-bottom bar path.
 *
 * `rx` on a `<rect>` rounds all four corners, which would lift a stack segment
 * off the baseline and off the segment beneath it. The data-end is the only
 * end that gets a radius.
 */
export function barPath(
  x: number,
  y: number,
  width: number,
  height: number,
  rounded: boolean
): string {
  if (height <= 0) {
    return "";
  }
  const radius = rounded ? Math.min(CORNER_RADIUS, height, width / 2) : 0;
  if (radius === 0) {
    return `M${x},${y}h${width}v${height}h${-width}Z`;
  }
  return [
    `M${x},${y + radius}`,
    `a${radius},${radius} 0 0 1 ${radius},${-radius}`,
    `h${width - 2 * radius}`,
    `a${radius},${radius} 0 0 1 ${radius},${radius}`,
    `v${height - radius}`,
    `h${-width}`,
    "Z",
  ].join("");
}
