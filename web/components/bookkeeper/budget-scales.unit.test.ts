import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  budgetDomainMax,
  budgetRow,
  buildBudgetLayout,
  cleanRatio,
  FULL_RATIO,
  formatConsumed,
  monthEndBudgetLines,
  RATIO_DOMAIN_CAP,
} from "./budget-scales";
import type { BudgetLine, MonthEndEnvelope } from "./types";

function line(overrides: Partial<BudgetLine> & { name: string }): BudgetLine {
  return {
    allocated: "100.00",
    balance: "50.00",
    carried_in: "0.00",
    consumed_ratio: 0.5,
    overspend: "0",
    overspent: false,
    remaining: "50.00",
    spent: "50.00",
    status: "within",
    ...overrides,
  };
}

describe("cleanRatio", () => {
  it("keeps null as null rather than letting it become zero", () => {
    // The absence is the signal. A zero here renders as "untouched", which is
    // one of the two readings the sidecar sends null to prevent.
    assert.equal(cleanRatio(null), null);
  });

  it("passes a real ratio through", () => {
    assert.equal(cleanRatio(0.8333), 0.8333);
    assert.equal(cleanRatio(2.4), 2.4);
    assert.equal(cleanRatio(0), 0);
  });

  it("treats a nonsense ratio as absent, not as zero", () => {
    // A NaN would propagate into every downstream coordinate and blank the row.
    assert.equal(cleanRatio(Number.NaN), null);
    assert.equal(cleanRatio(Number.POSITIVE_INFINITY), null);
    assert.equal(cleanRatio(-1), null);
  });
});

describe("budgetDomainMax", () => {
  it("stays at 1 when nothing is over budget", () => {
    // With every envelope under its allocation the rule sits at the track's
    // end, which is the reading a reader expects: the track *is* the budget.
    assert.equal(
      budgetDomainMax([
        line({ consumed_ratio: 0.2, name: "a" }),
        line({ consumed_ratio: 0.95, name: "b" }),
      ]),
      FULL_RATIO
    );
  });

  it("stretches past 1 so an overspend has somewhere to go", () => {
    assert.equal(
      budgetDomainMax([
        line({ consumed_ratio: 0.4, name: "a" }),
        line({ consumed_ratio: 1.6, name: "b", overspent: true }),
      ]),
      1.6
    );
  });

  it("caps the axis so one runaway envelope cannot crush the rest", () => {
    assert.equal(
      budgetDomainMax([
        line({ consumed_ratio: 0.5, name: "a" }),
        line({ consumed_ratio: 30, name: "b", overspent: true }),
      ]),
      RATIO_DOMAIN_CAP
    );
  });

  it("ignores a null ratio rather than reading it as zero", () => {
    assert.equal(
      budgetDomainMax([line({ consumed_ratio: null, name: "a" })]),
      FULL_RATIO
    );
  });
});

describe("budgetRow — an overspent bar is not a full bar", () => {
  it("does NOT clamp an over-budget bar at the end of its track", () => {
    // The defect this guards: a bar pinned at the track's end reads as "100%
    // consumed", which is exactly the misreading Phase 3 fixed in the
    // arithmetic. The overspend must occupy its own visible length.
    const row = budgetRow(
      line({
        consumed_ratio: 1.5,
        name: "Groceries",
        overspend: "50.00",
        overspent: true,
        status: "over",
      }),
      1.5
    );

    assert.equal(row.kind, "measured");
    assert.equal(row.isOver, true);
    assert.ok(row.overFraction > 0, "the overspend must have visible length");
    assert.equal(row.withinFraction + row.overFraction, 1);
    assert.ok(Math.abs(row.withinFraction - 2 / 3) < 1e-9);
    assert.ok(Math.abs(row.overFraction - 1 / 3) < 1e-9);
  });

  it("marks a bar past the cap as clipped rather than as ending there", () => {
    const row = budgetRow(
      line({ consumed_ratio: 8, name: "a", overspent: true, status: "over" }),
      RATIO_DOMAIN_CAP
    );
    assert.equal(row.clipped, true);
    assert.equal(row.withinFraction + row.overFraction, 1);
  });

  it("gives a row inside its budget no overspend segment", () => {
    const row = budgetRow(line({ consumed_ratio: 0.5, name: "a" }), 1);
    assert.equal(row.overFraction, 0);
    assert.equal(row.withinFraction, 0.5);
    assert.equal(row.clipped, false);
    assert.equal(row.isOver, false);
  });

  it("reads `over` from the sidecar's status, never from comparing to 1", () => {
    // A row over by a cent must not round its way back into the clear. The
    // verdict was reached in `Decimal`; this layer only reports it.
    const row = budgetRow(
      line({ consumed_ratio: 1, name: "a", status: "over" }),
      1
    );
    assert.equal(row.isOver, true);
  });

  it("does not call an envelope over budget just because it is overspent", () => {
    // Two different failures, and the sidecar keeps them apart: `overspent` is
    // a negative running balance (money gone, possibly months ago),
    // `status: "over"` is this window's spend exceeding this window's
    // allocation. Live sandbox data returned exactly this row — Transport,
    // `overspent: true` at 24% of its allocation — and collapsing the two
    // painted a comfortably-under bar red and badged it "over by $13.00".
    const row = budgetRow(
      line({
        consumed_ratio: 0.24,
        name: "Transport",
        overspend: "13.00",
        overspent: true,
        status: "within",
      }),
      1
    );

    assert.equal(row.isOver, false);
    assert.equal(row.overFraction, 0, "no bar may cross the allocation rule");
    // The balance overspend is not lost — it is `EnvelopeCard`'s to report,
    // and the flag is still on the line for it to read.
    assert.equal(row.line.overspent, true);
  });

  it("calls an envelope over budget without it being overspent", () => {
    // The converse, also live: Groceries at 137% of its allocation with a
    // healthy carried balance, so nothing went negative.
    const row = budgetRow(
      line({
        consumed_ratio: 1.375,
        name: "Groceries",
        overspend: "150.00",
        overspent: false,
        status: "over",
      }),
      1.375
    );
    assert.equal(row.isOver, true);
    assert.ok(row.overFraction > 0);
  });
});

describe("budgetRow — spending against a zero allocation", () => {
  it("is its own kind, not a ratio of zero or one", () => {
    // The state the sidecar reports honestly and a careless chart flattens.
    // There is no denominator, so there is no bar.
    const row = budgetRow(
      line({
        allocated: "0.00",
        consumed_ratio: null,
        name: "Dining",
        spent: "80.00",
        status: "unbudgeted",
      }),
      1
    );

    assert.equal(row.kind, "unallocated");
    assert.equal(row.ratio, null);
    assert.equal(row.withinFraction, 0);
    assert.equal(row.overFraction, 0);
  });

  it("has no percentage string a caller could print by accident", () => {
    // `formatConsumed` returns null rather than a string, so "0%" cannot be
    // rendered for this state — the compiler makes the caller handle it.
    assert.equal(formatConsumed(null), null);
    assert.equal(formatConsumed(0.424), "42%");
    assert.equal(formatConsumed(2.4), "240%");
  });

  it("keeps `unused` distinct from `unbudgeted`", () => {
    // An allocation nothing was spent against is a different state from no
    // allocation at all, and it still has a ratio: zero.
    const unused = budgetRow(
      line({
        consumed_ratio: 0,
        name: "a",
        spent: "0.00",
        status: "unused",
      }),
      1
    );
    assert.equal(unused.kind, "measured");
    assert.equal(unused.isUnused, true);
    assert.equal(unused.ratio, 0);
  });
});

describe("buildBudgetLayout", () => {
  it("puts what needs acting on first: over budget, then unallocated", () => {
    const layout = buildBudgetLayout([
      line({ consumed_ratio: 0.1, name: "Zebra" }),
      line({ consumed_ratio: null, name: "Dining", status: "unbudgeted" }),
      line({
        consumed_ratio: 1.4,
        name: "Groceries",
        overspent: true,
        status: "over",
      }),
    ]);

    assert.deepEqual(
      layout.rows.map((row) => row.name),
      ["Groceries", "Dining", "Zebra"]
    );
    assert.equal(layout.overCount, 1);
    assert.equal(layout.unallocatedCount, 1);
  });

  it("places the allocation rule at the same x for every row", () => {
    // One shared bound means one vertical line, so "past the line" means the
    // same thing on every row and the eye can sweep the chart.
    const layout = buildBudgetLayout([
      line({ consumed_ratio: 0.5, name: "a" }),
      line({ consumed_ratio: 2, name: "b", overspent: true, status: "over" }),
    ]);
    assert.equal(layout.domainMax, 2);
    assert.equal(layout.allocationFraction, 0.5);
    // The under-budget row reads at a quarter of the track, not at half — it
    // is measured against the same axis as the over-budget one.
    assert.equal(layout.rows.at(-1)?.withinFraction, 0.25);
  });

  it("reports emptiness rather than drawing an axis with no rows", () => {
    assert.equal(buildBudgetLayout([]).isEmpty, true);
  });

  it("orders stably so a poll does not repaint the list", () => {
    const rows = [
      line({ consumed_ratio: 0.5, name: "b" }),
      line({ consumed_ratio: 0.5, name: "a" }),
    ];
    assert.deepEqual(
      buildBudgetLayout(rows).rows.map((row) => row.name),
      buildBudgetLayout([...rows].reverse()).rows.map((row) => row.name)
    );
  });
});

describe("monthEndBudgetLines", () => {
  it("maps a month-end envelope onto the budget shape without inventing", () => {
    // The month-end report carries its own per-envelope figures. Mapping them
    // means one implementation of "how an overspend is drawn" rather than two.
    const envelope: MonthEndEnvelope = {
      allocated: "100.00",
      closing_balance: "-20.00",
      consumed_ratio: 1.2,
      direction: "up",
      direction_reason: "",
      name: "Groceries",
      opening_balance: "0.00",
      over_budget: true,
      overspend: "20.00",
      overspent: true,
      periods_observed: 3,
      periods_required: 3,
      remaining: "-20.00",
      spent: "120.00",
      status: "over",
    };

    const [mapped] = monthEndBudgetLines([envelope]);
    assert.equal(mapped.name, "Groceries");
    assert.equal(mapped.consumed_ratio, 1.2);
    assert.equal(mapped.overspent, true);
    assert.equal(mapped.status, "over");
    assert.equal(mapped.carried_in, "0.00");
    assert.equal(mapped.balance, "-20.00");
  });

  it("carries a null ratio through rather than defaulting it", () => {
    const [mapped] = monthEndBudgetLines([
      {
        allocated: "0.00",
        closing_balance: "0.00",
        consumed_ratio: null,
        direction: "insufficient_data",
        direction_reason: "",
        name: "Dining",
        opening_balance: "0.00",
        over_budget: false,
        overspend: "0",
        overspent: false,
        periods_observed: 1,
        periods_required: 3,
        remaining: "0.00",
        spent: "0.00",
        status: "unbudgeted",
      },
    ]);
    assert.equal(mapped.consumed_ratio, null);
    assert.equal(budgetRow(mapped, 1).kind, "unallocated");
  });
});
