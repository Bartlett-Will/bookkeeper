"use client";

/**
 * The month-end report, as a person would read it.
 *
 * This card's failure mode is not looking wrong. It is looking *authoritative*
 * while being incomplete — a page of confident totals over a month nobody has
 * categorized — and a reader has no way to detect that from the inside. Two
 * guards, both placed above the numbers rather than beside them:
 *
 * **Coverage leads.** Auto-apply is off, so essentially all spend is still
 * `Expenses:Unknown` and belongs to no envelope. Rendered plainly, that is a
 * month of near-zero spending with every envelope untouched, which reads as a
 * frugal month rather than a backlog. `report-chart.tsx` already states its
 * `unmapped_total` rather than drawing an empty-looking chart; a composite
 * inherits that problem in every section at once, so the notice goes first and
 * says what it does to the numbers underneath. A caveat printed after the
 * total it qualifies is read second, and by then the reader has a number in
 * mind.
 *
 * **A running month is labelled as one.** The sidecar's `coverage` enum
 * distinguishes `in-progress` (the month is not over — wait) from `partial`
 * (the month is over but the ledger stops part-way — sync), and both from
 * `complete`. The qualifier rides on the headline figures themselves, not just
 * the header: a number quoted out of a long card takes its nearest label with
 * it, and "spent" and "spent so far" are different claims.
 *
 * The card stands alone by construction. §5.3's amendment measured the blank
 * bubble as unconditional — a successful tool call returns empty prose — so
 * there is no sentence beside this explaining what it is.
 *
 * Nothing here is computed. The per-envelope budget bars render through
 * `budget-scales.ts` and `BudgetChart`'s row component, so there is exactly
 * one implementation of "how an overspend is drawn"; a second would be a
 * second chance to draw it clamped at full.
 */

import type { ReactNode } from "react";
import { BudgetChart } from "./budget-chart";
import { monthEndBudgetLines } from "./budget-scales";
import { CARD_CHROME } from "./card-chrome";
import { formatAmount, formatDate, pluralize } from "./format";
import {
  type CategorizationDescription,
  describeBacklog,
  describeCategorization,
  describePeriod,
  type PeriodDescription,
} from "./month-end-model";
import { DirectionGlyph, OutlierRow } from "./trend-card";
import {
  abstentionReason,
  describeDirection,
  outlierScope,
} from "./trend-model";
import type { MonthEndEnvelope, MonthEndReportData } from "./types";

export function MonthEndCard({ report }: { report: MonthEndReportData }) {
  const period = describePeriod(report);
  const categorization = describeCategorization(report);

  return (
    <section className={CARD_CHROME}>
      <Header period={period} report={report} />

      {report.errors.length > 0 ? (
        <p className="mb-3 rounded-md bg-destructive/10 px-2 py-1.5 text-[12px] text-destructive">
          {report.errors.join(" ")}
        </p>
      ) : null}

      {/*
        A month with no data is not a month of zeros. Rendering the sections
        below would produce a full report of nothing, which reads as a finding.
      */}
      {period.empty ? (
        <p className="py-6 text-center text-[13px] text-muted-foreground">
          {period.emptyReason}
        </p>
      ) : (
        <>
          <CategorizationNotice
            categorization={categorization}
            report={report}
          />
          <Headline
            categorization={categorization}
            period={period}
            report={report}
          />

          <ReportSection title="How each envelope finished">
            <EnvelopeMonthList report={report} />
          </ReportSection>

          <ReportSection title="Budget vs actual">
            <MonthEndBudget report={report} />
          </ReportSection>

          <ReportSection title="Direction">
            <DirectionList envelopes={report.envelopes} />
          </ReportSection>

          <ReportSection title="Unusual activity">
            <OutlierSection report={report} />
          </ReportSection>
        </>
      )}

      {report.warnings.length > 0 ? (
        <ul className="mt-3 space-y-0.5 border-border/60 border-t pt-3">
          {report.warnings.map((warning) => (
            <li className="text-[12px] text-muted-foreground" key={warning}>
              {warning}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function Header({
  period,
  report,
}: {
  period: PeriodDescription;
  report: MonthEndReportData;
}) {
  return (
    <header className="mb-3">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="font-medium text-[15px] text-foreground">
          {period.title}
        </h3>
        {/*
          A badge, not a footnote. A partial month wearing a finished month's
          heading is the misreading this card is most likely to cause, and a
          note at the top is invisible once the reader scrolls past it.
        */}
        {period.partial ? (
          <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-[11px] text-muted-foreground">
            {period.qualifier}
          </span>
        ) : null}
      </div>
      <p className="text-[12px] text-muted-foreground">
        {period.partial
          ? `Not a full month. Figures are current as at ${formatDate(report.asof)}.`
          : `The complete month, as at ${formatDate(report.asof)}.`}{" "}
        {pluralize(report.transactions, "transaction")}.
      </p>
    </header>
  );
}

/**
 * The categorization backlog, stated before anything it distorts.
 *
 * Severity is split because the cases need different sentences. With nothing
 * filed, the envelope figures are all zero and the card would otherwise
 * contradict itself; with some filed, the figures are real but read low.
 * `no-spend` gets no warning at all — a genuinely empty month is not a
 * categorization problem and must not wear a badge saying it is.
 */
function CategorizationNotice({
  categorization,
  report,
}: {
  categorization: CategorizationDescription;
  report: MonthEndReportData;
}) {
  if (!categorization.warns) {
    return null;
  }

  const { severe } = categorization;
  const backlog = describeBacklog(report, (amount) =>
    formatAmount(amount, report.currency)
  );

  return (
    <div
      className={
        severe
          ? "mb-3 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2"
          : "mb-3 rounded-md bg-muted/60 px-2.5 py-2"
      }
    >
      <p
        className={
          severe
            ? "flex items-center gap-1.5 font-medium text-[12px] text-destructive"
            : "flex items-center gap-1.5 font-medium text-[12px] text-foreground"
        }
      >
        <UncategorizedIcon />
        {categorization.headline}
      </p>
      {/*
        The count leads and the amount follows, each with its unit attached.
        A lone share reads as reassurance: live data had this month 98% filed
        by amount with 114 transactions still to review, and only one of those
        two numbers tells the reader how much work is left.
      */}
      <p className="mt-0.5 text-[12px] text-muted-foreground">
        {backlog.sentence ||
          `${formatAmount(report.unmapped_total, report.currency)} of ${formatAmount(report.total_spend, report.currency)} spent this month is in no envelope.`}{" "}
        {categorization.consequence} Confirm the review queue to bring it in.
      </p>
    </div>
  );
}

/**
 * The figures a reader takes away, each carrying its own period label.
 *
 * `totalSuffix` rides on the figure rather than the header for the reason
 * given at the top of the file. `total_spend` is the sidecar's own sum of
 * filed and unfiled spending — the denominator the per-envelope table has to
 * be read against — and adding those two strings here would be exactly the
 * arithmetic that field exists to keep out of this layer.
 */
function Headline({
  categorization,
  period,
  report,
}: {
  categorization: CategorizationDescription;
  period: PeriodDescription;
  report: MonthEndReportData;
}) {
  const suffix = period.totalSuffix ? ` ${period.totalSuffix}` : "";
  // The sidecar's per-envelope flags, never `Number(total_overspend) > 0`.
  // Money is a string end to end; deciding anything by parsing one — even a
  // text colour — is the habit that eventually decides a figure that way.
  const overspentCount = report.envelopes.filter(
    (envelope) => envelope.overspent
  ).length;

  return (
    <dl className="mb-4 grid grid-cols-1 gap-3 rounded-lg bg-muted/40 p-3 sm:grid-cols-3">
      <Figure
        label={`Spent${suffix}`}
        note={
          categorization.warns
            ? "everything, filed or not"
            : `${formatAmount(report.allocated_total, report.currency)} allocated`
        }
        value={formatAmount(report.total_spend, report.currency)}
      />
      <Figure
        label="Overspent envelopes"
        note={
          overspentCount > 0
            ? "balance went negative"
            : "no envelope in the red"
        }
        tone={overspentCount > 0 ? "bad" : "neutral"}
        value={formatAmount(report.total_overspend, report.currency)}
      />
      <Figure
        label="Available to allocate"
        note="overspend held back, not netted"
        value={formatAmount(report.available, report.currency)}
      />
    </dl>
  );
}

/**
 * Budget bars for the month, through the same layout the standalone chart uses.
 *
 * The month-end payload carries its own per-envelope figures rather than an
 * embedded budget report, so it is adapted rather than re-rendered — see
 * `monthEndBudgetLines`.
 */
function MonthEndBudget({ report }: { report: MonthEndReportData }) {
  return (
    <BudgetChart
      report={{
        currency: report.currency,
        envelopes: monthEndBudgetLines(report.envelopes),
        errors: [],
        from: report.from,
        ok: report.ok,
        summary: "",
        to: report.to,
        total_allocated: report.allocated_total,
        total_overspend: report.total_overspend,
        total_remaining: report.closing_total,
        total_spent: report.spent_total,
        unmapped_accounts: report.unmapped_accounts,
        unmapped_total: report.unmapped_total,
        warnings: [],
      }}
      variant="section"
    />
  );
}

/**
 * Direction per envelope, with abstention kept visibly apart from "steady".
 *
 * Reads the same `describeDirection` the trends card does, so the month-end
 * report cannot grow a more forgiving interpretation of `insufficient_data`.
 * Abstaining envelopes are grouped below the judged ones with a count in the
 * heading, and carry no figure — the sidecar's `direction_reason` instead.
 */
function DirectionList({ envelopes }: { envelopes: MonthEndEnvelope[] }) {
  const judged = envelopes.filter(
    (envelope) => !describeDirection(envelope.direction).abstained
  );
  const abstained = envelopes.filter(
    (envelope) => describeDirection(envelope.direction).abstained
  );

  if (envelopes.length === 0) {
    return (
      <p className="text-[12px] text-muted-foreground">
        No envelopes to judge.
      </p>
    );
  }

  return (
    <>
      {judged.length > 0 ? (
        <ul className="space-y-1.5">
          {judged.map((envelope) => {
            const direction = describeDirection(envelope.direction);
            return (
              <li className="flex items-baseline gap-2" key={envelope.name}>
                <DirectionGlyph glyph={direction.glyph} />
                <span className="truncate font-medium text-[13px] text-foreground">
                  {envelope.name}
                </span>
                <span className="text-[12px] text-muted-foreground">
                  {direction.label}
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}

      {abstained.length > 0 ? (
        <div className={judged.length > 0 ? "mt-3" : ""}>
          <p className="mb-1.5 text-[12px] text-muted-foreground">
            {pluralize(abstained.length, "envelope")} could not be judged:
          </p>
          <ul className="space-y-1">
            {abstained.map((envelope) => (
              <li
                className="flex flex-wrap items-baseline gap-x-2"
                key={envelope.name}
              >
                <span className="truncate text-[13px] text-muted-foreground">
                  {envelope.name}
                </span>
                <span className="text-[12px] text-muted-foreground/80">
                  {abstentionReason({
                    direction: envelope.direction,
                    mean: "0",
                    name: envelope.name,
                    periods_observed: envelope.periods_observed,
                    periods_required: envelope.periods_required,
                    points: [],
                    reason: envelope.direction_reason,
                    relative_slope: null,
                    slope: "0",
                    total: "0",
                  })}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

/**
 * Outliers, with the scope of the search attached.
 *
 * "Nothing looks unusual" is the sentence §5.3's amendment left unsolved:
 * short, unfalsifiable, and something a user would act on. The sidecar ships
 * `unjudged` so the honest version is available — nothing unusual *among the
 * envelopes with enough history to check* — and that qualifier is printed
 * whether or not anything was found.
 */
function OutlierSection({ report }: { report: MonthEndReportData }) {
  const unjudgedCount = report.unjudged.length;
  const judgedCount = report.envelopes.length - unjudgedCount;

  return (
    <>
      {report.outliers.length > 0 ? (
        <ul className="space-y-3">
          {report.outliers.map((outlier) => (
            <OutlierRow
              currency={report.currency}
              key={`${outlier.envelope}-${outlier.posted_date}-${outlier.description}`}
              outlier={outlier}
            />
          ))}
        </ul>
      ) : null}

      <p
        className={
          report.outliers.length > 0
            ? "mt-2 text-[12px] text-muted-foreground"
            : "text-[12px] text-muted-foreground"
        }
      >
        {/*
          The same sentence the trends card uses, from the same function. Two
          implementations would be two chances for one of them to drift into
          the unfalsifiable "nothing looks unusual".
        */}
        {outlierScope(
          {
            judgedCount: Math.max(0, judgedCount),
            nothingJudged: judgedCount <= 0 && report.envelopes.length > 0,
            unjudgedCount,
          },
          report.outliers.length
        )}
        {report.trend_from && report.trend_to
          ? ` Judged against ${formatDate(report.trend_from)} – ${formatDate(report.trend_to)}.`
          : ""}
      </p>
    </>
  );
}

/**
 * How each envelope opened, moved and closed over the month.
 *
 * Written rather than delegated to `EnvelopeCard`, and that is a correction:
 * forcing `MonthEndEnvelope` into `EnvelopeReportData` produced a real defect,
 * caught on screen. `EnvelopeBalance.overspend` means "how far the *balance*
 * went negative", while the month payload's `overspend` means "how far this
 * month's spend exceeded this month's allocation" — two different quantities
 * with the same name. Transport rendered as "over by US$0.00": flagged
 * `overspent` on its balance, with a budget overspend of zero.
 *
 * The month payload simply has no per-envelope balance-overspend figure, so
 * this list does not print one. It shows the closing balance, which is the
 * quantity that actually exists, and lets the sign carry the bad news. The
 * aggregate `total_overspend` is real and is stated above, in the headline.
 */
function EnvelopeMonthList({ report }: { report: MonthEndReportData }) {
  if (report.envelopes.length === 0) {
    return (
      <p className="text-[12px] text-muted-foreground">
        No envelopes are defined.
      </p>
    );
  }

  return (
    <ul className="space-y-1.5">
      {report.envelopes.map((envelope) => (
        <li
          className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5"
          key={envelope.name}
        >
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate font-medium text-[13px] text-foreground">
              {envelope.name}
            </span>
            {/*
              The balance verdict, as a word rather than a figure this payload
              does not carry. Distinct from the budget badge in the section
              below, which is about the month's allocation.
            */}
            {envelope.overspent ? (
              <span className="flex shrink-0 items-center gap-1 rounded bg-destructive/10 px-1.5 py-0.5 font-medium text-[11px] text-destructive">
                <OverIcon />
                balance negative
              </span>
            ) : null}
          </span>
          <span className="flex shrink-0 items-baseline gap-2 text-[12px] tabular-nums">
            <span className="text-muted-foreground">
              {formatAmount(envelope.opening_balance, report.currency)} →
            </span>
            <span
              className={
                envelope.overspent
                  ? "font-medium text-[13px] text-destructive"
                  : "font-medium text-[13px] text-foreground"
              }
            >
              {formatAmount(envelope.closing_balance, report.currency)}
            </span>
          </span>
        </li>
      ))}
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

function Figure({
  label,
  note,
  tone = "neutral",
  value,
}: {
  label: string;
  note: string;
  tone?: "neutral" | "bad";
  value: string;
}) {
  return (
    <div>
      <dt className="text-[12px] text-muted-foreground">{label}</dt>
      <dd
        className={
          tone === "bad"
            ? "font-semibold text-[18px] text-destructive leading-tight tabular-nums"
            : "font-semibold text-[18px] text-foreground leading-tight tabular-nums"
        }
      >
        {value}
      </dd>
      {note ? (
        <p className="text-[11px] text-muted-foreground/80">{note}</p>
      ) : null}
    </div>
  );
}

function ReportSection({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  return (
    <section className="mt-4 border-border/60 border-t pt-3">
      <h4 className="mb-2 font-medium text-[12px] text-muted-foreground uppercase tracking-wide">
        {title}
      </h4>
      {children}
    </section>
  );
}

function UncategorizedIcon() {
  return (
    <svg
      aria-hidden="true"
      className="size-3.5 shrink-0"
      fill="none"
      viewBox="0 0 14 14"
    >
      <rect
        height="9"
        rx="1.5"
        stroke="currentColor"
        strokeDasharray="2.2 1.8"
        strokeWidth="1.2"
        width="11"
        x="1.5"
        y="2.5"
      />
      <path
        d="M5.6 6.1a1.45 1.45 0 1 1 1.7 1.8v.7"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.2"
      />
      <circle cx="7.3" cy="10.1" fill="currentColor" r="0.65" />
    </svg>
  );
}
