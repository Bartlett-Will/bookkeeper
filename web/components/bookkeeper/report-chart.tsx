"use client";

/**
 * Spend by envelope over time, as a stacked column chart.
 *
 * Hand-rolled inline SVG on purpose. No charting library is installed, and
 * adding a runtime dependency for one chart that Phase 5 is going to rework is
 * a cost paid now for a design that has not settled. The arithmetic that would
 * justify a library — scales, stacking, tick selection — lives in
 * `chart-scales.ts` where it is tested directly; what is left here is markup.
 *
 * The palette is validated, not chosen. Its eight categorical slots and their
 * dark-mode steps were checked against this app's actual card surfaces
 * (`#ffffff` light, `oklch(0.225 0 0)` ≈ `#1c1c1c` dark) for lightness band,
 * chroma floor, colour-vision-deficiency separation and normal-vision
 * separation. Three light-mode slots sit below 3:1 contrast on white, which is
 * why the legend is always present and a table view ships beside the chart:
 * identity never rests on hue alone.
 *
 * Theming is by CSS custom property, scoped to this component rather than
 * added to `globals.css`. `next-themes` runs with `attribute="class"`, so the
 * `.dark` class on `<html>` is the single signal — it is already resolved for
 * the system setting, so a `prefers-color-scheme` query would only create a
 * second source of truth.
 */

import { useCallback, useId, useMemo, useState } from "react";
import {
  barPath,
  buildLayout,
  type ChartColumn,
  centsToAmount,
  MARGIN,
  PLOT_HEIGHT,
  PLOT_WIDTH,
  VIEW_HEIGHT,
  VIEW_WIDTH,
} from "./chart-scales";
import { formatAmount, formatMonth, pluralize } from "./format";
import type { SpendingReportData } from "./types";

const PALETTE_CSS = `
.bk-chart {
  --bk-grid: #e1e0d9;
  --bk-axis: #c3c2b7;
  --bk-series-0: #2a78d6;
  --bk-series-1: #eb6834;
  --bk-series-2: #1baf7a;
  --bk-series-3: #eda100;
  --bk-series-4: #e87ba4;
  --bk-series-5: #008300;
  --bk-series-6: #4a3aa7;
  --bk-other: #898781;
}
.dark .bk-chart {
  --bk-grid: #2c2c2a;
  --bk-axis: #383835;
  --bk-series-0: #3987e5;
  --bk-series-1: #d95926;
  --bk-series-2: #199e70;
  --bk-series-3: #c98500;
  --bk-series-4: #d55181;
  --bk-series-5: #008300;
  --bk-series-6: #9085e9;
  --bk-other: #898781;
}
`;

type Layout = ReturnType<typeof buildLayout>;
type PeriodFormatter = (period: string) => string;

function slotColor(slot: number, isOther: boolean): string {
  return isOther ? "var(--bk-other)" : `var(--bk-series-${slot})`;
}

export function ReportChart({ report }: { report: SpendingReportData }) {
  const layout = useMemo(() => buildLayout(report), [report]);
  const [hovered, setHovered] = useState<ChartColumn | null>(null);
  const [showTable, setShowTable] = useState(false);
  const titleId = useId();

  const toggleTable = useCallback(() => setShowTable((shown) => !shown), []);

  const periodLabel = useCallback<PeriodFormatter>(
    (period) => (report.granularity === "month" ? formatMonth(period) : period),
    [report.granularity]
  );

  const unmapped = Number(report.unmapped_total);
  const hasUnmapped = Number.isFinite(unmapped) && unmapped > 0;

  return (
    <section className="bk-chart w-full rounded-xl border border-border/60 bg-card p-4 shadow-[var(--shadow-card)]">
      {/* biome-ignore lint/security/noDangerouslySetInnerHtml: a constant stylesheet with no interpolation; the palette has to be CSS custom properties for the dark-mode swap to work. */}
      <style dangerouslySetInnerHTML={{ __html: PALETTE_CSS }} />

      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="font-medium text-[13px] text-foreground" id={titleId}>
            Spending by envelope
          </h3>
          <p className="text-[12px] text-muted-foreground">
            {periodLabel(report.from)} – {periodLabel(report.to)} ·{" "}
            {formatAmount(report.total, report.currency)} total
          </p>
        </div>
        <button
          className="rounded-md border border-border/60 px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          onClick={toggleTable}
          type="button"
        >
          {showTable ? "Show chart" : "Show table"}
        </button>
      </header>

      {report.errors.length > 0 ? (
        <p className="mb-3 rounded-md bg-destructive/10 px-2 py-1.5 text-[12px] text-destructive">
          {report.errors.join(" ")}
        </p>
      ) : null}

      {/*
        Stated rather than left as an empty-looking chart. Auto-apply is off, so
        most spend is still in `Expenses:Unknown` and belongs to no envelope; a
        chart that quietly omitted it would look like a period of no spending.
      */}
      {hasUnmapped ? (
        <p className="mb-3 rounded-md bg-muted/60 px-2 py-1.5 text-[12px] text-muted-foreground">
          {formatAmount(report.unmapped_total, report.currency)} is not assigned
          to any envelope yet and is not in this chart. Confirm the review queue
          to bring it in.
        </p>
      ) : null}

      {layout.isEmpty ? (
        <p className="py-8 text-center text-[13px] text-muted-foreground">
          No envelope spending in this period.
        </p>
      ) : (
        <>
          {showTable ? (
            <SpendingTable
              currency={report.currency}
              layout={layout}
              periodLabel={periodLabel}
            />
          ) : (
            <ChartBody
              currency={report.currency}
              hovered={hovered}
              labelledBy={titleId}
              layout={layout}
              onHover={setHovered}
              periodLabel={periodLabel}
            />
          )}

          {/* Always present: the dependable identity channel, and the relief
              for the light-mode slots that fall below 3:1 on white. */}
          <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
            {layout.series.map((series) => (
              <li
                className="flex items-center gap-1.5 text-[12px] text-muted-foreground"
                key={series.envelope}
              >
                <span
                  aria-hidden="true"
                  className="size-2.5 shrink-0 rounded-[2px]"
                  style={{
                    backgroundColor: slotColor(series.slot, series.isOther),
                  }}
                />
                <span>{series.envelope}</span>
                <span className="text-foreground/70 tabular-nums">
                  {formatAmount(
                    centsToAmount(series.totalCents),
                    report.currency
                  )}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function ChartBody({
  currency,
  hovered,
  labelledBy,
  layout,
  onHover,
  periodLabel,
}: {
  currency: string;
  hovered: ChartColumn | null;
  labelledBy: string;
  layout: Layout;
  onHover: (next: ChartColumn | null) => void;
  periodLabel: PeriodFormatter;
}) {
  const band = PLOT_WIDTH / Math.max(1, layout.columns.length);

  return (
    <div className="relative">
      <svg
        aria-labelledby={labelledBy}
        className="w-full"
        role="img"
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      >
        {/* Gridlines: solid hairlines one step off the surface, never dashed. */}
        {layout.ticks.map((tick) => (
          <g key={tick.value}>
            <line
              stroke="var(--bk-grid)"
              strokeWidth={1}
              x1={MARGIN.left}
              x2={MARGIN.left + PLOT_WIDTH}
              y1={tick.y}
              y2={tick.y}
            />
            <text
              className="fill-muted-foreground text-[11px] tabular-nums"
              dominantBaseline="middle"
              textAnchor="end"
              x={MARGIN.left - 8}
              y={tick.y}
            >
              {formatAmount(centsToAmount(tick.value), currency)}
            </text>
          </g>
        ))}

        <line
          stroke="var(--bk-axis)"
          strokeWidth={1}
          x1={MARGIN.left}
          x2={MARGIN.left + PLOT_WIDTH}
          y1={MARGIN.top + PLOT_HEIGHT}
          y2={MARGIN.top + PLOT_HEIGHT}
        />

        {layout.columns.map((column) => (
          <ColumnGroup
            band={band}
            column={column}
            currency={currency}
            dimmed={hovered !== null && hovered.period !== column.period}
            key={column.period}
            layout={layout}
            onHover={onHover}
            periodLabel={periodLabel}
          />
        ))}
      </svg>

      {hovered ? (
        <ColumnReadout
          column={hovered}
          currency={currency}
          layout={layout}
          periodLabel={periodLabel}
        />
      ) : null}
    </div>
  );
}

function ColumnGroup({
  band,
  column,
  currency,
  dimmed,
  layout,
  onHover,
  periodLabel,
}: {
  band: number;
  column: ChartColumn;
  currency: string;
  dimmed: boolean;
  layout: Layout;
  onHover: (next: ChartColumn | null) => void;
  periodLabel: PeriodFormatter;
}) {
  const show = useCallback(() => onHover(column), [column, onHover]);
  const hide = useCallback(() => onHover(null), [onHover]);

  const capY = column.segments.at(-1)?.y ?? MARGIN.top + PLOT_HEIGHT;
  const centre = column.x + column.width / 2;
  const total = formatAmount(centsToAmount(column.totalCents), currency);

  return (
    <g>
      {column.segments.map((segment) => (
        <path
          d={barPath(
            column.x,
            segment.y,
            column.width,
            segment.height,
            segment.roundedTop
          )}
          fill={slotColor(
            segment.slot,
            layout.series[segment.slot]?.isOther ?? false
          )}
          key={segment.envelope}
          opacity={dimmed ? 0.55 : 1}
        >
          <title>
            {`${segment.envelope}: ${formatAmount(centsToAmount(segment.cents), currency)}`}
          </title>
        </path>
      ))}

      {/* Selective direct label: the column total on the cap, never a number
          on every segment. */}
      {column.totalCents > 0 ? (
        <text
          className="fill-foreground text-[11px] tabular-nums"
          textAnchor="middle"
          x={centre}
          y={capY - 6}
        >
          {total}
        </text>
      ) : null}

      <text
        className="fill-muted-foreground text-[11px]"
        textAnchor="middle"
        x={centre}
        y={MARGIN.top + PLOT_HEIGHT + 20}
      >
        {periodLabel(column.period)}
      </text>

      {/*
        The hit target is the whole band, not the painted bar. A 24px column of
        thin segments is a pinpoint; hovering the band and reading every series
        at that period is what the reader wants anyway. Focusable, so keyboard
        reaches the same readout as the pointer.
      */}
      {/* biome-ignore lint/a11y/useSemanticElements: SVG cannot host a <button>; this transparent rect is the band-wide hover and focus target for its column. */}
      <rect
        aria-label={`${periodLabel(column.period)}: ${total}`}
        fill="transparent"
        height={PLOT_HEIGHT}
        onBlur={hide}
        onFocus={show}
        onMouseEnter={show}
        onMouseLeave={hide}
        role="button"
        tabIndex={0}
        width={band}
        x={centre - band / 2}
        y={MARGIN.top}
      />
    </g>
  );
}

/**
 * One tooltip listing every series at the hovered period.
 *
 * Values lead and names follow — the reader already knows which period they
 * are pointing at and wants the numbers. Series names come from the ledger, so
 * they go in as text nodes, never as markup.
 */
function ColumnReadout({
  column,
  currency,
  layout,
  periodLabel,
}: {
  column: ChartColumn;
  currency: string;
  layout: Layout;
  periodLabel: PeriodFormatter;
}) {
  const leftPercent = ((column.x + column.width / 2) / VIEW_WIDTH) * 100;
  const anchorRight = leftPercent > 60;

  return (
    <div
      className="pointer-events-none absolute top-2 z-10 min-w-40 rounded-lg border border-border/60 bg-card px-2.5 py-2 shadow-[var(--shadow-card)]"
      style={
        anchorRight
          ? { marginRight: 8, right: `${100 - leftPercent}%` }
          : { left: `${leftPercent}%`, marginLeft: 8 }
      }
    >
      <p className="mb-1 font-medium text-[12px] text-foreground">
        {periodLabel(column.period)}
      </p>
      {column.segments.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">No spending.</p>
      ) : (
        <ul className="space-y-0.5">
          {[...column.segments].reverse().map((segment) => (
            <li
              className="flex items-center gap-2 text-[12px]"
              key={segment.envelope}
            >
              <span
                aria-hidden="true"
                className="h-0.5 w-3 shrink-0 rounded-full"
                style={{
                  backgroundColor: slotColor(
                    segment.slot,
                    layout.series[segment.slot]?.isOther ?? false
                  ),
                }}
              />
              <span className="font-medium text-foreground tabular-nums">
                {formatAmount(centsToAmount(segment.cents), currency)}
              </span>
              <span className="truncate text-muted-foreground">
                {segment.envelope}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-1 border-border/60 border-t pt-1 text-[12px] text-muted-foreground">
        <span className="font-medium text-foreground tabular-nums">
          {formatAmount(centsToAmount(column.totalCents), currency)}
        </span>{" "}
        total
      </p>
    </div>
  );
}

/** The WCAG-clean twin of the chart. Every value the chart draws is here. */
function SpendingTable({
  currency,
  layout,
  periodLabel,
}: {
  currency: string;
  layout: Layout;
  periodLabel: PeriodFormatter;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <caption className="sr-only">
          Spend per envelope across {pluralize(layout.columns.length, "period")}
        </caption>
        <thead>
          <tr className="border-border/60 border-b text-muted-foreground">
            <th className="py-1.5 pr-3 text-left font-medium" scope="col">
              Period
            </th>
            {layout.series.map((series) => (
              <th
                className="py-1.5 pl-3 text-right font-medium"
                key={series.envelope}
                scope="col"
              >
                {series.envelope}
              </th>
            ))}
            <th className="py-1.5 pl-3 text-right font-medium" scope="col">
              Total
            </th>
          </tr>
        </thead>
        <tbody>
          {layout.columns.map((column) => (
            <tr
              className="border-border/40 border-b last:border-0"
              key={column.period}
            >
              <th className="py-1.5 pr-3 text-left font-normal" scope="row">
                {periodLabel(column.period)}
              </th>
              {layout.series.map((series) => {
                const segment = column.segments.find(
                  (candidate) => candidate.envelope === series.envelope
                );
                return (
                  <td
                    className="py-1.5 pl-3 text-right tabular-nums"
                    key={series.envelope}
                  >
                    {segment
                      ? formatAmount(centsToAmount(segment.cents), currency)
                      : "—"}
                  </td>
                );
              })}
              <td className="py-1.5 pl-3 text-right font-medium tabular-nums">
                {formatAmount(centsToAmount(column.totalCents), currency)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
