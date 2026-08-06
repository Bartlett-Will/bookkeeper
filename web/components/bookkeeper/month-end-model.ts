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
 * What can honestly be said about the per-envelope table's completeness.
 *
 * Returns the sidecar's own `categorized_share` untouched for display. It is a
 * `Decimal` string 0–1; the card renders it rather than deriving a share from
 * `spent_total` and `unmapped_total`, which would be exactly the arithmetic
 * `total_spend` exists to keep out of this layer.
 */
export function categorizedShareLabel(report: MonthEndReportData): string {
  const raw = report.categorized_share.trim();
  // `Number("")` is 0, not NaN. Without this guard a field we could not read
  // would render as a confident "0% filed" — a claim about the user's ledger
  // manufactured out of an absence.
  if (raw.length === 0) {
    return "";
  }
  const share = Number(raw);
  if (!Number.isFinite(share) || share < 0) {
    return "";
  }
  return `${Math.round(share * 100)}% of this month's spending is filed`;
}
