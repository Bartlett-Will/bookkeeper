/**
 * The two judgements the month-end card must get right, as pure functions.
 *
 * Both exist because the card's failure mode is not looking wrong — it is
 * looking *authoritative* while being incomplete, which no amount of visual
 * polish fixes and no reader can detect from the inside.
 *
 * **1. A partial month is not a month.** The sidecar's `coverage` enum is the
 * whole point: `future` | `in-progress` | `no-data` | `partial` | `complete`,
 * with the note that "so far this month" and "for July" are different claims a
 * user acts on differently, "so a client must not render them with the same
 * words". `describePeriod` makes that structural — the qualifier travels with
 * the figures rather than sitting once at the top of a long card, because a
 * number quoted out of a card takes its nearest label with it.
 *
 * `partial` and `in-progress` are also kept apart, because they have different
 * fixes. In progress means the month is not over — wait. Partial means the
 * month is over but the ledger's data stops part-way through — sync.
 *
 * **2. An uncategorized month is not a frugal month.** Auto-apply is off, so
 * essentially all spend is still `Expenses:Unknown` and belongs to no
 * envelope. A card printing envelope figures without saying so shows a month
 * of near-zero spending that a reader has no way to know is a backlog.
 * `report-chart.tsx` already states its `unmapped_total` rather than drawing an
 * empty-looking chart; this is the same standard applied to a composite where
 * every section inherits the problem at once.
 *
 * No money arithmetic happens here. Both judgements are read off enums the
 * sidecar computed in `Decimal`, and its totals are passed through as strings
 * for the caller to format.
 */

import type { MonthEndReportData } from "./types";

export type PeriodDescription = {
  /** The month, in the sidecar's own words. Rendered, never parsed. */
  title: string;
  /** Whether any total in this card is short of a full month. */
  partial: boolean;
  /** Why it is short, or empty when it is not. */
  qualifier: string;
  /**
   * The word to attach to totals: `"so far"` or the empty string.
   *
   * Returned rather than left to each call site so a section added later
   * cannot quietly print a partial total as a final one.
   */
  totalSuffix: string;
  /** True when there is nothing to report at all — no data, or not yet begun. */
  empty: boolean;
  /** What to say instead of a report, when `empty`. */
  emptyReason: string;
};

export function describePeriod(report: MonthEndReportData): PeriodDescription {
  const title = report.label.trim().length > 0 ? report.label : report.month;
  const base = {
    empty: false,
    emptyReason: "",
    partial: false,
    qualifier: "",
    title,
    totalSuffix: "",
  };

  switch (report.coverage) {
    case "complete":
      return base;
    case "future":
      return {
        ...base,
        empty: true,
        emptyReason: "That month has not started yet.",
      };
    case "no-data":
      return {
        ...base,
        empty: true,
        emptyReason: "The ledger has no transactions in that month at all.",
      };
    case "in-progress":
      return {
        ...base,
        partial: true,
        qualifier: "still in progress",
        totalSuffix: "so far",
      };
    case "partial":
      return {
        ...base,
        partial: true,
        // A month whose data stops on the 12th is not a month of no spending
        // after the 12th — it is a month nobody has synced since.
        qualifier: report.data_through
          ? `data stops at ${report.data_through}`
          : "data is incomplete",
        totalSuffix: "so far",
      };
    default:
      // An unrecognised coverage is treated as partial, never as complete.
      // Defaulting the other way would turn a body we failed to understand
      // into a confident statement that the month is finished.
      return {
        ...base,
        partial: true,
        qualifier: "coverage unknown",
        totalSuffix: "so far",
      };
  }
}

/**
 * How much of the month's spending reached an envelope at all.
 *
 * `none` is separated from `partial` because they are different warnings. With
 * nothing categorized, every envelope figure in the card is zero and the card
 * is not reporting a quiet month — it is reporting a queue of unfiled work.
 * With some categorized, the figures are real but incomplete, which needs a
 * caveat rather than a contradiction. `no-spend` is neither: a genuinely empty
 * month is not a categorization problem and must not wear a warning that says
 * it is.
 */
export type CoverageLevel = "none" | "partial" | "full" | "no-spend";

export type CategorizationDescription = {
  level: CoverageLevel;
  /** Whether a warning should be shown above the figures. */
  warns: boolean;
  /** True for the case where the per-envelope table describes nothing at all. */
  severe: boolean;
  /** The headline warning, or empty when there is nothing to warn about. */
  headline: string;
  /** What it means for the figures below it. */
  consequence: string;
};

export function describeCategorization(
  report: MonthEndReportData
): CategorizationDescription {
  const quiet = { consequence: "", headline: "", severe: false, warns: false };

  switch (report.categorization) {
    case "full":
      return { ...quiet, level: "full" };
    case "no-spend":
      return { ...quiet, level: "no-spend" };
    case "none":
      return {
        consequence:
          "Every envelope figure below is therefore zero. That is the categorization backlog, not a month without spending.",
        headline:
          "None of this month's spending has been filed to an envelope.",
        level: "none",
        severe: true,
        warns: true,
      };
    default:
      // `partial`, and anything unrecognised. Warning on an unknown value is
      // the cautious direction: a missed caveat is worse than a spare one.
      return {
        consequence:
          "The envelope figures below cover only what has been filed so far, so they read low.",
        headline:
          "Only part of this month's spending has been filed to an envelope.",
        level: "partial",
        severe: false,
        warns: true,
      };
  }
}

/**
 * The size of the categorization backlog, in both units, because one lies.
 *
 * This function exists because of a measurement. On live sandbox data the same
 * July reported `categorized_share: 0.9768` and `uncategorized_count: 114`:
 * **98% filed by amount, and 114 transactions still to review.** The card used
 * to print only the share, and "98% of this month's spending is filed" tells a
 * reader they are nearly done when the actual remaining work is 114 rows.
 *
 * Neither unit is wrong and neither is sufficient. The amount says how much of
 * the *money* the envelope figures below account for — which is what makes
 * those figures trustworthy or not. The count says how much *work* is left.
 * A long tail of small uncategorized transactions under a few large
 * categorized ones produces exactly this split, and it is the normal shape of
 * a real ledger, not a corner case.
 *
 * So both are stated, each with its unit attached, and the bare percentage is
 * gone. A lone "98%" is the single most reassuring and least informative
 * number available here.
 *
 * No arithmetic: the count and both amounts are read verbatim. In particular
 * `categorized_count + uncategorized_count` is **not** used as a denominator —
 * on live data it came to 119 against `transactions: 122`, because transfers
 * and opening balances are neither. Summing them would invent a total the
 * sidecar never claimed.
 */
export type BacklogDescription = {
  /** How many transactions still need filing. Zero when there is no backlog. */
  count: number;
  /** The sentence, or empty when there is nothing to report. */
  sentence: string;
};

export function describeBacklog(
  report: MonthEndReportData,
  formatAmount: (amount: string) => string
): BacklogDescription {
  const count = Math.max(0, report.uncategorized_count);
  if (count === 0) {
    return { count: 0, sentence: "" };
  }

  const unfiled = formatAmount(report.unmapped_total);
  const total = formatAmount(report.total_spend);
  return {
    count,
    sentence: `${count.toLocaleString()} ${count === 1 ? "transaction is" : "transactions are"} not filed to an envelope, covering ${unfiled} of the ${total} spent this month.`,
  };
}
