"use client";

/**
 * Budget vs actual: what was allocated, what was spent, and where that went
 * wrong.
 *
 * A bullet row per envelope — one bar for spend, one rule at the allocation —
 * rather than paired bars. Paired bars make the reader compare two lengths and
 * do the subtraction; a bar crossing a marked line puts the answer in the
 * geometry, and "past the line" is what this chart is for.
 *
 * **Marks are CSS-sized `div`s, not SVG.** The no-charting-library decision
 * stands and this is still hand-rolled, but `report-chart.tsx`'s fixed
 * `viewBox` is the right tool for a stacked time series with scales and ticks
 * and the wrong one for a list of horizontal bars that should reflow with the
 * card. `envelope-card.tsx` already draws its meter this way; following it
 * keeps the two cards' bars the same weight and radius at every width.
 *
 * **No new palette.** Colour here does a *status* job, not a categorical one:
 * budgeted spend takes `primary`, over-budget takes `destructive`, and both
 * are theme variables this app already ships in light and dark. The
 * categorical eight-slot palette in `report-chart.tsx` was validated against
 * real card surfaces; inventing hues here without rerunning that validation
 * would put unchecked colour on screen. Every status also carries an icon and
 * a word, so nothing is asserted by colour alone.
 *
 * Every figure is the sidecar's. `status`, `overspend`, `remaining` and
 * `percent_consumed` are computed in `Decimal`; the only arithmetic here turns
 * a server-supplied percentage into a fraction of a track width.
 */

import {
  type BudgetRow,
  buildBudgetLayout,
  formatConsumed,
} from "./budget-scales";
import { chromeFor, type ReportVariant } from "./card-chrome";
import { formatAmount, formatDate, pluralize } from "./format";
import type { BudgetReportData } from "./types";

type Layout = ReturnType<typeof buildBudgetLayout>;

export function BudgetChart({
  report,
  variant = "card",
}: {
  report: BudgetReportData;
  variant?: ReportVariant;
}) {
  const layout = buildBudgetLayout(report.envelopes);

  return (
    <section className={chromeFor(variant)}>
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h3 className="font-medium text-[13px] text-foreground">
            Budget vs actual
          </h3>
          <p className="text-[12px] text-muted-foreground">
            {formatDate(report.from)} – {formatDate(report.to)} ·{" "}
            {pluralize(report.envelopes.length, "envelope")}
          </p>
        </div>
        <div className="text-right">
          <p className="font-semibold text-[20px] text-foreground leading-tight tabular-nums">
            {formatAmount(report.total_spent, report.currency)}
          </p>
          <p className="text-[12px] text-muted-foreground">
            spent of {formatAmount(report.total_allocated, report.currency)}{" "}
            allocated
          </p>
        </div>
      </header>

      {report.errors.length > 0 ? (
        <p className="mb-3 rounded-md bg-destructive/10 px-2 py-1.5 text-[12px] text-destructive">
          {report.errors.join(" ")}
        </p>
      ) : null}

      <BudgetNotices layout={layout} report={report} />

      {layout.isEmpty ? (
        <p className="py-8 text-center text-[13px] text-muted-foreground">
          No envelopes are defined for this period.
        </p>
      ) : (
        <>
          <ul className="space-y-3">
            {layout.rows.map((row) => (
              <BudgetRowView
                currency={report.currency}
                key={row.name}
                layout={layout}
                row={row}
              />
            ))}
          </ul>
          <Legend layout={layout} />
        </>
      )}
    </section>
  );
}

/**
 * The two things a reader is most likely to be wrong about, said before the
 * rows rather than after them.
 */
function BudgetNotices({
  layout,
  report,
}: {
  layout: Layout;
  report: BudgetReportData;
}) {
  const unmapped = Number(report.unmapped_total);
  const hasUnmapped = Number.isFinite(unmapped) && unmapped > 0;

  return (
    <>
      {layout.overCount > 0 ? (
        <p className="mb-3 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-md bg-destructive/10 px-2.5 py-2 text-[12px] text-destructive">
          <OverIcon />
          <span className="font-medium">
            {formatAmount(report.total_overspend, report.currency)} over budget
          </span>
          <span className="text-destructive/80">
            across {pluralize(layout.overCount, "envelope")}.
          </span>
        </p>
      ) : null}

      {/*
        Auto-apply is off, so most spend is still `Expenses:Unknown` and is in
        none of the bars below. Stated rather than omitted — `report-chart.tsx`
        sets this standard, and a budget chart missing most of the window's
        spending is the more dangerous place to break it, because every bar
        reads short and the shortfall looks like underspending.
      */}
      {hasUnmapped ? (
        <p className="mb-3 rounded-md bg-muted/60 px-2 py-1.5 text-[12px] text-muted-foreground">
          {formatAmount(report.unmapped_total, report.currency)} is not assigned
          to any envelope yet, so it is in none of these bars. Confirm the
          review queue to bring it in.
        </p>
      ) : null}
    </>
  );
}

/**
 * One envelope.
 *
 * The two states this component exists to keep apart are branched here rather
 * than merged into one bar with different numbers, because they are not the
 * same drawing: one has a denominator and one does not.
 */
function BudgetRowView({
  currency,
  layout,
  row,
}: {
  currency: string;
  layout: Layout;
  row: BudgetRow;
}) {
  const consumed = formatConsumed(row.line.percent_consumed);

  return (
    <li>
      <div className="flex items-baseline justify-between gap-3">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate font-medium text-[13px] text-foreground">
            {row.name}
          </span>
          {row.isOver ? (
            <span className="flex shrink-0 items-center gap-1 rounded bg-destructive/10 px-1.5 py-0.5 font-medium text-[11px] text-destructive">
              <OverIcon />
              over by {formatAmount(row.line.overspend, currency)}
            </span>
          ) : null}
          {/*
            Not "0%" and not "100%". Spending against nothing budgeted has no
            percentage at all, and the badge says which state this is in words
            — the missing bar below would otherwise read as "no spend".
          */}
          {row.kind === "unallocated" ? (
            <span className="flex shrink-0 items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-medium text-[11px] text-muted-foreground">
              <NoBudgetIcon />
              nothing allocated
            </span>
          ) : null}
        </span>
        <span className="shrink-0 text-[13px] text-muted-foreground tabular-nums">
          {consumed === null ? (
            <span title="No allocation, so no percentage exists.">—</span>
          ) : (
            <span
              className={
                row.isOver ? "font-medium text-destructive" : undefined
              }
            >
              {consumed}
            </span>
          )}
        </span>
      </div>

      {row.kind === "unallocated" ? (
        <UnallocatedTrack />
      ) : (
        <MeasuredTrack layout={layout} row={row} />
      )}

      <p className="mt-1 text-[12px] text-muted-foreground tabular-nums">
        {formatAmount(row.line.spent, currency)} spent of{" "}
        {formatAmount(row.line.allocated, currency)} allocated
        {row.clipped ? (
          <span className="ml-1 text-muted-foreground/80">
            (bar cut off; the figure is exact)
          </span>
        ) : null}
      </p>
    </li>
  );
}

/**
 * A bar that is allowed to cross its own budget line.
 *
 * The overspend is a second segment past the rule, in the status colour, with
 * a surface-coloured gap between the two so the crossing reads as a change of
 * material rather than only as a longer bar. It is emphatically **not**
 * clamped at full: `envelope-card.tsx`'s meter can clamp because its badge
 * names the overspend beside it, but a chart whose entire subject is the
 * comparison would be drawing "exactly on budget" over a real overspend.
 */
function MeasuredTrack({ layout, row }: { layout: Layout; row: BudgetRow }) {
  return (
    <div aria-hidden="true" className="relative mt-1.5 h-2 w-full">
      <div className="absolute inset-0 rounded-full bg-muted" />

      <div className="absolute inset-y-0 left-0 flex">
        <div
          className="h-2 rounded-l-full bg-primary"
          style={{ width: `${row.withinFraction * 100}%` }}
        />
        {row.overFraction > 0 ? (
          <div
            className="h-2 border-card border-l-2 bg-destructive"
            style={{ width: `${row.overFraction * 100}%` }}
          />
        ) : null}
      </div>

      {/* The allocation rule. One shared x for every row, so the eye can sweep
          the chart for what crossed it. */}
      <div
        className="absolute top-[-3px] bottom-[-3px] w-px bg-foreground/45"
        style={{ left: `${layout.allocationFraction * 100}%` }}
      />

      {/* An open end says "continues" where a flat end would say "stops
          exactly here". */}
      {row.clipped ? (
        <div className="absolute inset-y-0 right-0 w-2 rounded-r-full bg-gradient-to-r from-destructive to-transparent" />
      ) : null}
    </div>
  );
}

/**
 * The zero-allocation case: a track with nothing to fill it.
 *
 * Dashed and empty. A proportional bar needs a denominator, and drawing one
 * anyway — at either end of the track — would be the chart inventing a budget
 * that does not exist. The spend is printed underneath as an amount, which is
 * the only honest quantity available.
 */
function UnallocatedTrack() {
  return (
    <div
      aria-hidden="true"
      className="mt-1.5 h-2 w-full rounded-full border border-border border-dashed"
    />
  );
}

/** Always present: three marks are in play and two mean something a length cannot say. */
function Legend({ layout }: { layout: Layout }) {
  return (
    <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 border-border/60 border-t pt-3 text-[12px] text-muted-foreground">
      <li className="flex items-center gap-1.5">
        <span
          aria-hidden="true"
          className="h-1.5 w-4 rounded-full bg-primary"
        />
        <span>within budget</span>
      </li>
      {layout.overCount > 0 ? (
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="h-1.5 w-4 rounded-full bg-destructive"
          />
          <span>over budget</span>
        </li>
      ) : null}
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-3 w-px bg-foreground/45" />
        <span>allocation</span>
      </li>
      {layout.unallocatedCount > 0 ? (
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="h-1.5 w-4 rounded-full border border-border border-dashed"
          />
          <span>nothing allocated</span>
        </li>
      ) : null}
    </ul>
  );
}

function OverIcon() {
  return (
    <svg
      aria-hidden="true"
      className="size-3 shrink-0"
      fill="none"
      viewBox="0 0 12 12"
    >
      <path
        d="M6 1.5 11 10.5H1L6 1.5Z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.2"
      />
      <path
        d="M6 5v2.2"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.2"
      />
      <circle cx="6" cy="9" fill="currentColor" r="0.6" />
    </svg>
  );
}

function NoBudgetIcon() {
  return (
    <svg
      aria-hidden="true"
      className="size-3 shrink-0"
      fill="none"
      viewBox="0 0 12 12"
    >
      <circle
        cx="6"
        cy="6"
        r="4.5"
        stroke="currentColor"
        strokeDasharray="2 1.6"
        strokeWidth="1.2"
      />
      <path
        d="M4 6h4"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.2"
      />
    </svg>
  );
}
