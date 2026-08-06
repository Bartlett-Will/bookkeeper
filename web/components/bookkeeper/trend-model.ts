/**
 * Presentation logic for trends and outliers. Pure, so the distinction that
 * matters most here can be tested rather than eyeballed.
 *
 * **Abstention is not a direction.** The sidecar answers `insufficient_data`
 * when it cannot judge an envelope, and `api.py` says so in as many words:
 * it "is *not* `flat`: `flat` means the slope was measured and is small". "We do not have the
 * history to judge this" and "spending is steady" are different claims, and a
 * reader acts on them differently. The temptation is to let abstention fall
 * through the same rendering path as a zero slope, which produces a chart
 * saying "steady" about envelopes nobody has enough history for — wasting the
 * honesty the backend went to the trouble of expressing.
 *
 * The rule enforced below: an abstaining line **never carries a figure**.
 * `showsFigures` is false for it, so there is no number a reader can mistake
 * for a measured zero.
 *
 * **An outlier must be interrogable.** §5.3's amendment names unfalsifiable
 * claims as this app's live failure mode, and "this transaction is unusual" is
 * exactly that shape. The sidecar ships `median`, `scale`, `scale_method`,
 * `threshold` and `score` with every flag precisely so the finding can be
 * checked from one record, and `assessments` so that "nothing unusual" stays
 * distinguishable from "not enough data to look".
 */

import type {
  EnvelopeTrend,
  OutlierAssessment,
  OutlierTransaction,
} from "./types";

/**
 * The mark a trend line gets.
 *
 * `"unknown"` is deliberately not in the same family as the other three. The
 * arrows and the rule are magnitudes; the unknown mark is a hollow, dotted
 * glyph that reads as "no answer" and cannot be mistaken at a glance for a
 * flat line — which is exactly what a horizontal dash would be.
 */
export type TrendGlyph = "up" | "down" | "steady" | "unknown";

export type DirectionPresentation = {
  glyph: TrendGlyph;
  /** The claim, in words. Never a direction word when abstaining. */
  label: string;
  /**
   * Whether measured figures may be printed alongside this verdict.
   *
   * False for abstention, always. A slope of `0.00` beside "not enough data"
   * is a measured zero to every reader who sees it, and the sidecar measured
   * nothing it was willing to stand behind.
   */
  showsFigures: boolean;
  /** True when the sidecar declined to judge. */
  abstained: boolean;
};

/**
 * The direction enum, on its own.
 *
 * Split out from `describeTrend` because two different payloads carry this
 * verdict and both must render it identically: `/reports/trends` sends a full
 * `EnvelopeTrend` with the statistics behind it, while the month-end report
 * sends only `direction` and `direction_reason` on each envelope. Reading the
 * enum in one place is what stops the month-end card growing its own, subtly
 * more forgiving, interpretation of `insufficient_data`.
 *
 * Rising spend is not "bad" and falling spend is not "good". Groceries up 4%
 * in a month with a house guest is nothing; a subscriptions envelope up 4% is
 * a price rise worth knowing about. Nothing here can tell those apart, so
 * direction gets no status colour and the status palette stays reserved for
 * states the sidecar actually asserts.
 */
export function describeDirection(direction: string): DirectionPresentation {
  switch (direction) {
    case "up":
      return {
        abstained: false,
        glyph: "up",
        label: "spending up",
        showsFigures: true,
      };
    case "down":
      return {
        abstained: false,
        glyph: "down",
        label: "spending down",
        showsFigures: true,
      };
    case "flat":
      return {
        abstained: false,
        glyph: "steady",
        label: "steady",
        showsFigures: true,
      };
    default:
      // `insufficient_data`, and anything unrecognised. An unknown direction is
      // treated as an abstention rather than guessed at — the cautious end,
      // and the only one that cannot manufacture a verdict.
      return {
        abstained: true,
        glyph: "unknown",
        label: "not enough data to say",
        showsFigures: false,
      };
  }
}

export type TrendPresentation = DirectionPresentation & {
  trend: EnvelopeTrend;
};

export function describeTrend(trend: EnvelopeTrend): TrendPresentation {
  return { ...describeDirection(trend.direction), trend };
}

/**
 * Split judged envelopes from abstained ones.
 *
 * Two sections rather than one mixed list. Interleaved, the abstentions would
 * be noticed one row at a time and a reader skimming for direction would read
 * straight past them; a section with its own heading makes the count of
 * unjudged envelopes visible before any individual row is.
 */
export function partitionTrends(trends: EnvelopeTrend[]): {
  judged: TrendPresentation[];
  abstained: TrendPresentation[];
} {
  const judged: TrendPresentation[] = [];
  const abstained: TrendPresentation[] = [];
  for (const trend of trends) {
    const presented = describeTrend(trend);
    if (presented.abstained) {
      abstained.push(presented);
    } else {
      judged.push(presented);
    }
  }
  return { abstained, judged };
}

/**
 * The relative slope as a percentage, when there is one.
 *
 * `relative_slope` is a `Decimal` string and null when the sidecar could not
 * form it — a mean of zero has no relative anything. Null propagates rather
 * than becoming `0%`.
 */
export function formatRelativeSlope(relative: string | null): string | null {
  if (relative === null) {
    return null;
  }
  const value = Number(relative);
  if (!Number.isFinite(value)) {
    return null;
  }
  const percent = Math.round(value * 100);
  return `${percent > 0 ? "+" : ""}${percent}% per period`;
}

/**
 * Why an envelope could not be judged, in the sidecar's words with a fallback.
 *
 * The sidecar's `reason` is preferred because it knows which rule it applied
 * and what threshold it applied it at. The fallback names the count, so a
 * reader still learns how far short the history fell — "1 of 3" says whether
 * waiting another month fixes it.
 */
export function abstentionReason(trend: EnvelopeTrend): string {
  if (trend.reason.trim().length > 0) {
    return trend.reason;
  }
  return `${trend.periods_observed} of ${trend.periods_required} periods have spending.`;
}

/**
 * What can honestly be said about outlier detection over a set of envelopes.
 *
 * This function exists to prevent one sentence: "nothing looks unusual".
 * §5.3's amendment names it as the residue that suppressing numbers did not
 * fix — short, unfalsifiable, and actionable. It is only true of envelopes
 * that were actually examined, and with a sparse ledger most are not. The
 * sidecar ships `assessments` for exactly this, and the honest sentence is
 * "nothing unusual among the N we could check".
 */
export type OutlierCoverage = {
  judgedCount: number;
  unjudgedCount: number;
  /** True when nothing could be examined at all — the strongest caveat. */
  nothingJudged: boolean;
};

export function outlierCoverage(
  assessments: OutlierAssessment[]
): OutlierCoverage {
  const judgedCount = assessments.filter(
    (assessment) => assessment.judged
  ).length;
  return {
    judgedCount,
    nothingJudged: assessments.length > 0 && judgedCount === 0,
    unjudgedCount: assessments.length - judgedCount,
  };
}

export type OutlierStrip = {
  /** Fractional bounds of the band inside which nothing is flagged. */
  bandStart: number;
  bandEnd: number;
  /** Where this transaction sits, 0–1. */
  markerFraction: number;
  /** True when the score ran past the drawn axis and the marker is pinned. */
  clipped: boolean;
};

/**
 * Position a flagged transaction against the rule that flagged it.
 *
 * Plotted in **score units, not dollars**. The sidecar already reduced the
 * transaction to a `score` and states the `threshold` it had to beat, so the
 * strip can show "how far past the line" using only two numbers the sidecar
 * computed — no money arithmetic in the browser, and no reconstruction of a
 * statistic that was worked out in `Decimal`.
 *
 * The band is symmetric because the threshold is: an unusually *small* charge
 * on an envelope is as much an outlier as a large one, and drawing only the
 * upper bound would imply otherwise.
 */
export function outlierStrip(outlier: OutlierTransaction): OutlierStrip {
  const score = Number(outlier.score);
  const threshold = Math.abs(Number(outlier.threshold));

  // Without a usable threshold there is no rule to draw, so the band collapses
  // to the centre and the marker sits with it rather than at a false position.
  if (
    !(Number.isFinite(score) && Number.isFinite(threshold)) ||
    threshold === 0
  ) {
    return {
      bandEnd: 0.5,
      bandStart: 0.5,
      clipped: false,
      markerFraction: 0.5,
    };
  }

  // Enough headroom that a marker just past the threshold is visibly past it.
  const bound = Math.max(Math.abs(score), threshold * 1.6);
  const place = (value: number) => (value + bound) / (2 * bound);

  return {
    bandEnd: place(threshold),
    bandStart: place(-threshold),
    clipped: Math.abs(score) > bound,
    markerFraction: place(Math.max(-bound, Math.min(bound, score))),
  };
}
