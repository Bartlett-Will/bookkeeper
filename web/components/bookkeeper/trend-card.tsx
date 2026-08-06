"use client";

/**
 * Trends per envelope, and the transactions the sidecar flagged as unusual.
 *
 * Two things this card is built to avoid.
 *
 * **Abstention rendered as "flat".** The sidecar returns
 * `direction: "undetermined"` when it has too little history to judge an
 * envelope, and `api.py` is explicit that this "is an abstention and is not
 * `flat`". A reader acts on the two differently. So abstaining envelopes get a
 * hollow dotted mark instead of a rule, the words "not enough data to say"
 * instead of a direction, their reason spelled out, no figure at all, and
 * their own section with a count in its heading. Four channels, because one is
 * how a distinction quietly disappears in a redesign.
 *
 * **An outlier the reader cannot check.** §5.3's amendment names unfalsifiable
 * claims as this app's live failure mode, and "this transaction is unusual" is
 * exactly that shape. Every flagged row carries its justification — the median
 * it was compared against, the spread and how that spread was derived, the
 * threshold it had to beat, and a strip placing its score against that
 * threshold. The `assessments` roll-up is the same instinct one level up: it
 * is what lets the card say "nothing unusual among the 4 envelopes we could
 * check" instead of the unfalsifiable "nothing looks unusual".
 *
 * Direction carries no status colour. A rise is not bad and a fall is not
 * good — the sidecar computes a direction, not a judgement, and painting rises
 * red would be the card asserting something nobody measured.
 */

import { chromeFor, type ReportVariant } from "./card-chrome";
import { formatAmount, formatDate, formatMagnitude, pluralize } from "./format";
import {
  abstentionReason,
  formatRelativeSlope,
  outlierCoverage,
  outlierStrip,
  partitionTrends,
  type TrendPresentation,
} from "./trend-model";
import type { OutlierTransaction, TrendsReportData } from "./types";

export function TrendCard({
  report,
  variant = "card",
}: {
  report: TrendsReportData;
  variant?: ReportVariant;
}) {
  const { judged, abstained } = partitionTrends(report.envelopes);
  const coverage = outlierCoverage(report.assessments);

  return (
    <section className={chromeFor(variant)}>
      <header className="mb-3">
        <h3 className="font-medium text-[13px] text-foreground">
          Trends and unusual activity
        </h3>
        <p className="text-[12px] text-muted-foreground">
          {formatDate(report.from)} – {formatDate(report.to)} ·{" "}
          {pluralize(report.envelopes.length, "envelope")}
        </p>
      </header>

      {report.errors.length > 0 ? (
        <p className="mb-3 rounded-md bg-destructive/10 px-2 py-1.5 text-[12px] text-destructive">
          {report.errors.join(" ")}
        </p>
      ) : null}

      {report.envelopes.length === 0 ? (
        <p className="py-6 text-center text-[13px] text-muted-foreground">
          No envelopes to compare over this period.
        </p>
      ) : null}

      {judged.length > 0 ? (
        <ul className="space-y-2">
          {judged.map((trend) => (
            <JudgedRow
              currency={report.currency}
              key={trend.trend.name}
              trend={trend}
            />
          ))}
        </ul>
      ) : null}

      {/*
        Its own section, with the count in the heading. Interleaved with the
        judged rows these would be noticed one at a time, and a reader skimming
        for direction would read straight past them.
      */}
      {abstained.length > 0 ? (
        <section className="mt-4 border-border/60 border-t pt-3">
          <h4 className="mb-2 flex items-center gap-1.5 font-medium text-[12px] text-muted-foreground">
            <UnknownGlyph />
            Not enough data to judge {pluralize(abstained.length, "envelope")}
          </h4>
          <ul className="space-y-1.5">
            {abstained.map((trend) => (
              <AbstainedRow key={trend.trend.name} trend={trend} />
            ))}
          </ul>
        </section>
      ) : null}

      <section className="mt-4 border-border/60 border-t pt-3">
        <h4 className="mb-2 font-medium text-[12px] text-foreground">
          {report.outliers.length > 0
            ? pluralize(report.outliers.length, "unusual transaction")
            : "Nothing unusual found"}
        </h4>

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

        {/*
          The sentence §5.3's amendment left unsolved. "Nothing looks unusual"
          is short, unfalsifiable and actionable; "nothing unusual among the 4
          we could check, 9 had too little history" is the same news with its
          scope attached, and the scope is the part that was missing.
        */}
        <p className="mt-1.5 text-[12px] text-muted-foreground">
          {coverage.nothingJudged
            ? `No envelope had enough history to check — ${pluralize(coverage.unjudgedCount, "envelope")} went unexamined, so this is not a finding that nothing is unusual.`
            : `Checked ${pluralize(coverage.judgedCount, "envelope")}${
                coverage.unjudgedCount > 0
                  ? `; ${coverage.unjudgedCount} had too little history to check`
                  : ""
              }. A transaction is flagged past ${report.outlier_threshold}× the spread, from at least ${pluralize(report.min_transactions, "transaction")}.`}
        </p>
      </section>
    </section>
  );
}

function JudgedRow({
  currency,
  trend,
}: {
  currency: string;
  trend: TrendPresentation;
}) {
  const relative = formatRelativeSlope(trend.trend.relative_slope);

  return (
    <li className="flex items-baseline justify-between gap-3">
      <span className="flex min-w-0 items-center gap-2">
        <DirectionGlyph glyph={trend.glyph} />
        <span className="truncate font-medium text-[13px] text-foreground">
          {trend.trend.name}
        </span>
        <span className="shrink-0 text-[12px] text-muted-foreground">
          {trend.label}
        </span>
      </span>
      {/* `showsFigures` is false only for abstention, which never reaches this
          component — the check stays so a direction added to the enum later
          cannot print a figure the sidecar did not stand behind. */}
      {trend.showsFigures ? (
        <span className="flex shrink-0 items-baseline gap-1.5 text-[12px] tabular-nums">
          <span className="font-medium text-foreground">
            {formatAmount(trend.trend.mean, currency)}
          </span>
          <span className="text-muted-foreground">
            avg{relative ? ` · ${relative}` : ""}
          </span>
        </span>
      ) : null}
    </li>
  );
}

/**
 * An envelope with no verdict.
 *
 * No figure anywhere on the row. A number beside "not enough data" is a
 * measurement to everyone who reads it, and nothing was measured. The reason
 * carries the useful part: which rule fell short, so the reader knows whether
 * another month fixes it.
 */
function AbstainedRow({ trend }: { trend: TrendPresentation }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className="truncate text-[13px] text-muted-foreground">
        {trend.trend.name}
      </span>
      <span className="text-[12px] text-muted-foreground/80">
        {abstentionReason(trend.trend)}
      </span>
    </li>
  );
}

/**
 * A flagged transaction, with the evidence for the flag.
 *
 * The strip is the interrogable part: the band inside which nothing is
 * flagged, and this transaction's score outside it. A reader who disagrees can
 * see *why* the sidecar disagreed, and the figures underneath name the median,
 * the spread, and how the spread was derived — enough to recompute the score
 * from this one row, which is what the sidecar ships them for.
 */
export function OutlierRow({
  currency,
  outlier,
}: {
  currency: string;
  outlier: OutlierTransaction;
}) {
  const strip = outlierStrip(outlier);

  return (
    <li>
      <div className="flex items-baseline justify-between gap-3">
        <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          <span className="truncate font-medium text-[13px] text-foreground">
            {outlier.description}
          </span>
          <span className="shrink-0 text-[12px] text-muted-foreground">
            {formatDate(outlier.posted_date)} · {outlier.envelope}
          </span>
        </span>
        <span className="shrink-0 font-medium text-[13px] text-foreground tabular-nums">
          {formatAmount(outlier.amount, currency)}
        </span>
      </div>

      <OutlierScale outlier={outlier} strip={strip} />

      <p className="mt-1 text-[12px] text-muted-foreground">
        Scored <span className="tabular-nums">{outlier.score}</span> against a
        threshold of <span className="tabular-nums">{outlier.threshold}</span>,
        from a median of{" "}
        <span className="tabular-nums">
          {formatMagnitude(outlier.median, currency)}
        </span>{" "}
        and a spread of{" "}
        <span className="tabular-nums">
          {formatMagnitude(outlier.scale, currency)}
        </span>
        {outlier.scale_method ? ` by ${outlier.scale_method}` : ""}.
      </p>
    </li>
  );
}

/**
 * The score against the threshold that flagged it.
 *
 * Plotted in score units rather than dollars, using only the two numbers the
 * sidecar computed. The band is symmetric because the threshold is: an
 * unusually *small* charge is as much an outlier as a large one, and drawing
 * only the upper bound would imply otherwise.
 */
function OutlierScale({
  outlier,
  strip,
}: {
  outlier: OutlierTransaction;
  strip: ReturnType<typeof outlierStrip>;
}) {
  const width = Math.max(0, strip.bandEnd - strip.bandStart);

  return (
    <div
      aria-label={`Scored ${outlier.score} against a threshold of ${outlier.threshold}`}
      className="relative mt-1.5 h-3 w-full"
      role="img"
    >
      <div className="absolute inset-x-0 top-[5px] h-0.5 rounded-full bg-muted" />
      <div
        className="absolute top-[5px] h-0.5 rounded-full bg-foreground/35"
        style={{
          left: `${strip.bandStart * 100}%`,
          width: `${width * 100}%`,
        }}
      />
      {/* A 2px surface ring so the marker stays legible where it overlaps the
          band. */}
      <div
        className="absolute top-0 size-3 rounded-full border-2 border-card bg-destructive"
        style={{
          left: `${strip.markerFraction * 100}%`,
          transform: "translateX(-50%)",
        }}
      />
    </div>
  );
}

export function DirectionGlyph({
  glyph,
}: {
  glyph: TrendPresentation["glyph"];
}) {
  if (glyph === "unknown") {
    return <UnknownGlyph />;
  }

  const paths: Record<string, string> = {
    down: "M6 2.5v7M3 6.5l3 3 3-3",
    steady: "M2.5 6h7",
    up: "M6 9.5v-7M3 5.5l3-3 3 3",
  };

  return (
    <svg
      aria-hidden="true"
      className="size-3 shrink-0 text-foreground/70"
      fill="none"
      viewBox="0 0 12 12"
    >
      <path
        d={paths[glyph]}
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.3"
      />
    </svg>
  );
}

/**
 * The abstention mark: hollow, dotted, and carrying a question rather than a
 * direction. Deliberately not a horizontal rule — that is `steady`'s mark, and
 * at a glance the two would be the same shape.
 */
function UnknownGlyph() {
  return (
    <svg
      aria-hidden="true"
      className="size-3 shrink-0 text-muted-foreground"
      fill="none"
      viewBox="0 0 12 12"
    >
      <circle
        cx="6"
        cy="6"
        r="4.6"
        stroke="currentColor"
        strokeDasharray="1.8 1.5"
        strokeWidth="1.1"
      />
      <path
        d="M4.7 4.8a1.35 1.35 0 1 1 1.6 1.7v.6"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.1"
      />
      <circle cx="6.3" cy="8.6" fill="currentColor" r="0.6" />
    </svg>
  );
}
