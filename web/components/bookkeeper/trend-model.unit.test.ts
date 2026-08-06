import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  abstentionReason,
  describeTrend,
  formatRelativeSlope,
  outlierCoverage,
  outlierStrip,
  partitionTrends,
} from "./trend-model";
import type {
  EnvelopeTrend,
  OutlierAssessment,
  OutlierTransaction,
} from "./types";

function trend(
  overrides: Partial<EnvelopeTrend> & { name: string }
): EnvelopeTrend {
  return {
    direction: "rising",
    mean: "100.00",
    periods_observed: 3,
    points: [],
    reason: "",
    relative_slope: "0.12",
    slope: "12.00",
    total: "300.00",
    ...overrides,
  };
}

function outlier(overrides: Partial<OutlierTransaction>): OutlierTransaction {
  return {
    amount: "-240.00",
    description: "WHOLE FOODS",
    envelope: "Groceries",
    median: "42.00",
    posted_date: "2026-07-14",
    scale: "12.00",
    scale_method: "median absolute deviation",
    score: "6.4",
    threshold: "3.5",
    ...overrides,
  };
}

function assessment(
  overrides: Partial<OutlierAssessment> & { envelope: string }
): OutlierAssessment {
  return {
    judged: true,
    median: "42.00",
    outliers_found: 0,
    reason: "",
    sample_size: 12,
    scale: "12.00",
    scale_method: "median absolute deviation",
    ...overrides,
  };
}

describe("describeTrend — abstention is not a direction", () => {
  it("gives `undetermined` a mark outside the direction family", () => {
    // `api.py`: "`undetermined` is an abstention and is not `flat`". A
    // horizontal dash would be indistinguishable from `flat` at a glance,
    // which is the flattening this guards against.
    const abstained = describeTrend(
      trend({ direction: "undetermined", name: "New" })
    );
    const flat = describeTrend(trend({ direction: "flat", name: "Steady" }));

    assert.equal(abstained.glyph, "unknown");
    assert.equal(flat.glyph, "steady");
    assert.notEqual(abstained.glyph, flat.glyph);
    assert.notEqual(abstained.label, flat.label);
  });

  it("never lets an abstaining line print a measured figure", () => {
    // A slope of 0.00 beside "not enough data" is a measured zero to every
    // reader who sees it, and the sidecar stood behind nothing.
    const abstained = describeTrend(
      trend({
        direction: "undetermined",
        name: "New",
        relative_slope: null,
        slope: "0.00",
      })
    );
    assert.equal(abstained.showsFigures, false);
    assert.equal(abstained.abstained, true);
  });

  it("lets a flat line print its measured figures", () => {
    const flat = describeTrend(
      trend({ direction: "flat", name: "a", slope: "0.40" })
    );
    assert.equal(flat.showsFigures, true);
    assert.equal(flat.abstained, false);
    assert.equal(flat.label, "steady");
  });

  it("treats an unrecognised direction as abstention, never as a verdict", () => {
    // The cautious end: an unknown value cannot manufacture a claim about
    // someone's spending.
    const unknown = describeTrend(trend({ direction: "sideways", name: "a" }));
    assert.equal(unknown.abstained, true);
    assert.equal(unknown.showsFigures, false);
  });
});

describe("partitionTrends", () => {
  it("separates unjudged envelopes into their own group", () => {
    const { judged, abstained } = partitionTrends([
      trend({ direction: "rising", name: "a" }),
      trend({ direction: "undetermined", name: "b" }),
      trend({ direction: "flat", name: "c" }),
      trend({ direction: "falling", name: "d" }),
      trend({ direction: "undetermined", name: "e" }),
    ]);

    assert.deepEqual(
      judged.map((row) => row.trend.name),
      ["a", "c", "d"]
    );
    assert.deepEqual(
      abstained.map((row) => row.trend.name),
      ["b", "e"]
    );
  });
});

describe("abstentionReason", () => {
  it("prefers the sidecar's own reason, which knows the threshold", () => {
    assert.equal(
      abstentionReason(
        trend({
          direction: "undetermined",
          name: "a",
          reason: "Needs 3 periods; 1 has spending.",
        })
      ),
      "Needs 3 periods; 1 has spending."
    );
  });

  it("falls back to the count so the reader learns how far short it fell", () => {
    assert.match(
      abstentionReason(
        trend({
          direction: "undetermined",
          name: "a",
          periods_observed: 1,
          reason: "   ",
        })
      ),
      /1 period/
    );
  });
});

describe("formatRelativeSlope", () => {
  it("keeps the sign visible", () => {
    assert.equal(formatRelativeSlope("0.25"), "+25% per period");
    assert.equal(formatRelativeSlope("-0.08"), "-8% per period");
  });

  it("has nothing to say when the sidecar could not form one", () => {
    // A mean of zero has no relative anything; null must not become 0%.
    assert.equal(formatRelativeSlope(null), null);
    assert.equal(formatRelativeSlope("not-a-number"), null);
  });
});

describe("outlierCoverage — 'nothing unusual' is only true of what was checked", () => {
  it("counts what was judged against what was not", () => {
    const coverage = outlierCoverage([
      assessment({ envelope: "a" }),
      assessment({ envelope: "b", judged: false, sample_size: 2 }),
      assessment({ envelope: "c", judged: false, sample_size: 1 }),
    ]);
    assert.equal(coverage.judgedCount, 1);
    assert.equal(coverage.unjudgedCount, 2);
    assert.equal(coverage.nothingJudged, false);
  });

  it("flags the case where nothing could be examined at all", () => {
    // The strongest caveat, and the one that stops the card asserting the
    // unfalsifiable "nothing looks unusual" over a ledger it never checked.
    const coverage = outlierCoverage([
      assessment({ envelope: "a", judged: false }),
      assessment({ envelope: "b", judged: false }),
    ]);
    assert.equal(coverage.nothingJudged, true);
    assert.equal(coverage.judgedCount, 0);
  });

  it("does not claim nothing was judged when there was nothing to judge", () => {
    // No envelopes at all is "no envelopes exist", not "none could be
    // checked" — the two must not produce the same sentence.
    assert.equal(outlierCoverage([]).nothingJudged, false);
  });
});

describe("outlierStrip — the flag has to be interrogable", () => {
  it("places a flagged score outside the band that would not be flagged", () => {
    const strip = outlierStrip(outlier({ score: "6.4", threshold: "3.5" }));
    assert.ok(strip.markerFraction > strip.bandEnd);
    assert.ok(strip.bandStart < strip.bandEnd);
  });

  it("draws the band symmetrically, because the threshold is", () => {
    // An unusually small charge is as much an outlier as a large one; drawing
    // only the upper bound would imply otherwise.
    const strip = outlierStrip(outlier({ score: "6.4", threshold: "3.5" }));
    assert.ok(Math.abs(strip.bandStart + strip.bandEnd - 1) < 1e-9);
  });

  it("places a negative score below the band", () => {
    const strip = outlierStrip(outlier({ score: "-6.4", threshold: "3.5" }));
    assert.ok(strip.markerFraction < strip.bandStart);
  });

  it("keeps every fraction inside the strip", () => {
    for (const score of ["0", "3.5", "-3.5", "200", "-200", "6.4"]) {
      const strip = outlierStrip(outlier({ score }));
      for (const fraction of [
        strip.bandStart,
        strip.bandEnd,
        strip.markerFraction,
      ]) {
        assert.ok(
          fraction >= 0 && fraction <= 1,
          `score ${score} produced ${fraction}`
        );
      }
    }
  });

  it("collapses rather than inventing a position when there is no rule", () => {
    // Without a usable threshold there is no line to be past, so the marker
    // must not be drawn somewhere that implies there was.
    const strip = outlierStrip(outlier({ score: "5", threshold: "0" }));
    assert.equal(strip.markerFraction, 0.5);
    assert.equal(strip.bandStart, strip.bandEnd);
  });

  it("does not produce NaN from an unreadable score", () => {
    const strip = outlierStrip(outlier({ score: "not-a-number" }));
    assert.ok(Number.isFinite(strip.markerFraction));
    assert.ok(Number.isFinite(strip.bandStart));
  });
});
