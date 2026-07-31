import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  barPath,
  buildLayout,
  buildSeries,
  centsToAmount,
  MARGIN,
  MAX_SERIES,
  niceTicks,
  OTHER_LABEL,
  PLOT_HEIGHT,
  toCents,
} from "./chart-scales";
import type { SpendingPoint, SpendingReportData } from "./types";

function point(
  period: string,
  envelope: string,
  amount: string
): SpendingPoint {
  return { amount, envelope, period };
}

function report(overrides: Partial<SpendingReportData>): SpendingReportData {
  return {
    currency: "USD",
    errors: [],
    from: "2026-05-01",
    granularity: "month",
    ok: true,
    periods: [],
    points: [],
    to: "2026-07-31",
    total: "0.00",
    unmapped_total: "0.00",
    warnings: [],
    ...overrides,
  };
}

describe("toCents", () => {
  it("converts decimal strings exactly", () => {
    assert.equal(toCents("19.96"), 1996);
    assert.equal(toCents("0.1"), 10);
    assert.equal(toCents("-4.05"), -405);
  });

  it("reads unparseable amounts as zero rather than NaN", () => {
    // A NaN would propagate into every downstream coordinate and blank the
    // chart; zero degrades to a missing segment.
    assert.equal(toCents("not-a-number"), 0);
    assert.equal(toCents(""), 0);
  });

  it("round-trips through centsToAmount", () => {
    assert.equal(centsToAmount(toCents("1234.56")), "1234.56");
  });
});

describe("buildSeries", () => {
  it("ranks envelopes by total spend across the window", () => {
    const series = buildSeries([
      point("2026-05-01", "Food", "10.00"),
      point("2026-06-01", "Food", "10.00"),
      point("2026-05-01", "Rent", "15.00"),
    ]);
    assert.deepEqual(
      series.map((s) => s.envelope),
      ["Food", "Rent"]
    );
    assert.equal(series[0].totalCents, 2000);
    assert.deepEqual(
      series.map((s) => s.slot),
      [0, 1]
    );
  });

  it("breaks ties by name so slots do not shuffle between renders", () => {
    const points = [
      point("2026-05-01", "Zebra", "5.00"),
      point("2026-05-01", "Apple", "5.00"),
    ];
    const first = buildSeries(points).map((s) => s.envelope);
    const second = buildSeries([...points].reverse()).map((s) => s.envelope);
    assert.deepEqual(first, ["Apple", "Zebra"]);
    assert.deepEqual(first, second);
  });

  it("folds the tail into Other past the series cap", () => {
    const points: SpendingPoint[] = [];
    for (let index = 0; index < 12; index += 1) {
      // Descending amounts, so the smallest envelopes are the ones folded.
      points.push(point("2026-05-01", `Env${index}`, `${100 - index}.00`));
    }
    const series = buildSeries(points);
    assert.equal(series.length, MAX_SERIES);
    assert.equal(series.at(-1)?.envelope, OTHER_LABEL);
    assert.equal(series.at(-1)?.isOther, true);

    // Nothing is dropped: Other carries the whole tail's spend.
    const total = points.reduce((sum, p) => sum + toCents(p.amount), 0);
    assert.equal(
      series.reduce((sum, s) => sum + s.totalCents, 0),
      total
    );
  });

  it("does not fold when the count exactly equals the cap", () => {
    const points = Array.from({ length: MAX_SERIES }, (_, index) =>
      point("2026-05-01", `Env${index}`, "10.00")
    );
    const series = buildSeries(points);
    assert.equal(series.length, MAX_SERIES);
    assert.ok(series.every((s) => !s.isOther));
  });
});

describe("niceTicks", () => {
  it("lands on clean round values", () => {
    const ticks = niceTicks(18_700);
    assert.deepEqual(
      ticks.map((t) => t.value),
      [0, 5000, 10_000, 15_000, 20_000]
    );
  });

  it("puts zero on the baseline and the bound at the top of the plot", () => {
    const ticks = niceTicks(10_000);
    assert.equal(ticks[0].y, MARGIN.top + PLOT_HEIGHT);
    assert.equal(ticks.at(-1)?.y, MARGIN.top);
  });

  it("degrades to a single baseline tick when there is no spend", () => {
    assert.deepEqual(niceTicks(0), [{ value: 0, y: MARGIN.top + PLOT_HEIGHT }]);
  });
});

describe("buildLayout", () => {
  it("keeps periods that have no spend, so the time axis is not compressed", () => {
    const layout = buildLayout(
      report({
        periods: ["2026-05-01", "2026-06-01", "2026-07-01"],
        points: [point("2026-05-01", "Food", "40.00")],
      })
    );
    assert.deepEqual(
      layout.columns.map((c) => c.period),
      ["2026-05-01", "2026-06-01", "2026-07-01"]
    );
    assert.equal(layout.columns[1].segments.length, 0);
    assert.equal(layout.columns[1].totalCents, 0);
  });

  it("totals a column exactly, in cents", () => {
    const layout = buildLayout(
      report({
        periods: ["2026-05-01"],
        points: [
          point("2026-05-01", "Food", "0.10"),
          point("2026-05-01", "Rent", "0.20"),
        ],
      })
    );
    // The float sum of 0.10 and 0.20 is 0.30000000000000004; the column total
    // has to agree with its own printed label to the cent.
    assert.equal(layout.columns[0].totalCents, 30);
    assert.equal(centsToAmount(layout.columns[0].totalCents), "0.30");
  });

  it("gives an envelope the same slot in every column", () => {
    const layout = buildLayout(
      report({
        periods: ["2026-05-01", "2026-06-01"],
        points: [
          point("2026-05-01", "Food", "10.00"),
          point("2026-05-01", "Rent", "90.00"),
          // Food outspends Rent in June; its colour must not follow that rank.
          point("2026-06-01", "Food", "80.00"),
          point("2026-06-01", "Rent", "5.00"),
        ],
      })
    );
    const slotOf = (columnIndex: number, envelope: string) =>
      layout.columns[columnIndex].segments.find((s) => s.envelope === envelope)
        ?.slot;
    assert.equal(slotOf(0, "Food"), slotOf(1, "Food"));
    assert.equal(slotOf(0, "Rent"), slotOf(1, "Rent"));
    assert.notEqual(slotOf(0, "Food"), slotOf(0, "Rent"));
  });

  it("stacks segments contiguously from the baseline", () => {
    const layout = buildLayout(
      report({
        periods: ["2026-05-01"],
        points: [
          point("2026-05-01", "Food", "50.00"),
          point("2026-05-01", "Rent", "50.00"),
        ],
      })
    );
    const [lower, upper] = layout.columns[0].segments;
    // Laid out bottom-up: the first segment sits on the baseline.
    assert.equal(
      Math.round(lower.y + lower.height + 2),
      MARGIN.top + PLOT_HEIGHT
    );
    assert.ok(upper.y < lower.y, "the second segment stacks above the first");
    assert.equal(upper.roundedTop, true);
    assert.equal(lower.roundedTop, false);
  });

  it("reports emptiness rather than drawing a zero-height chart", () => {
    const layout = buildLayout(report({ periods: ["2026-05-01"], points: [] }));
    assert.equal(layout.isEmpty, true);
    assert.equal(layout.maxCents, 0);
  });

  it("falls back to the periods present in the points", () => {
    // `periods` is empty when the sidecar omits it; the chart should still draw.
    const layout = buildLayout(
      report({
        periods: [],
        points: [
          point("2026-06-01", "Food", "10.00"),
          point("2026-05-01", "Food", "10.00"),
        ],
      })
    );
    assert.deepEqual(
      layout.columns.map((c) => c.period),
      ["2026-05-01", "2026-06-01"]
    );
  });
});

describe("barPath", () => {
  it("returns nothing for a zero-height segment", () => {
    assert.equal(barPath(0, 0, 24, 0, true), "");
  });

  it("squares the baseline end and rounds only the data end", () => {
    const rounded = barPath(10, 20, 24, 60, true);
    const square = barPath(10, 20, 24, 60, false);
    assert.ok(rounded.includes("a4,4"), "the data end carries a radius");
    assert.ok(!square.includes("a"), "an interior segment has no arcs at all");
  });

  it("never lets the radius exceed the segment it is rounding", () => {
    // A 2px-tall segment with a 4px radius would invert the path.
    const path = barPath(0, 0, 24, 2, true);
    assert.ok(path.includes("a2,2"));
  });
});
