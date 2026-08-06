import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  budgetDomainMax,
  budgetRow,
  buildBudgetLayout,
  FULL_PERCENT,
  formatConsumed,
  PERCENT_DOMAIN_CAP,
  parsePercent,
} from "./budget-scales";
import type { BudgetLine } from "./types";

function line(overrides: Partial<BudgetLine> & { name: string }): BudgetLine {
  return {
    allocated: "100.00",
    balance: "50.00",
    carried_in: "0.00",
    overspend: "0",
    percent_consumed: "50.00",
    remaining: "50.00",
    spent: "50.00",
    status: "within",
    ...overrides,
  };
}

describe("parsePercent", () => {
  it("keeps null as null rather than letting it become zero", () => {
    // The absence is the signal. A zero here would render as "untouched",
    // which is one of the two readings the sidecar sends null to prevent.
    assert.equal(parsePercent(null), null);
  });

  it("reads a decimal percentage string", () => {
    assert.equal(parsePercent("83.33"), 83.33);
    assert.equal(parsePercent("240"), 240);
    assert.equal(parsePercent("0"), 0);
  });

  it("treats an unreadable value as absent, not as zero", () => {
    assert.equal(parsePercent("not-a-number"), null);
    assert.equal(parsePercent("-5"), null);
  });
});

describe("budgetDomainMax", () => {
  it("stays at 100 when nothing is over budget", () => {
    // With every envelope under its allocation the rule sits at the track's
    // end, which is the reading a reader expects: the track *is* the budget.
    assert.equal(
      budgetDomainMax([
        line({ name: "a", percent_consumed: "20" }),
        line({ name: "b", percent_consumed: "95" }),
      ]),
      FULL_PERCENT
    );
  });

  it("stretches past 100 so an overspend has somewhere to go", () => {
    assert.equal(
      budgetDomainMax([
        line({ name: "a", percent_consumed: "40" }),
        line({ name: "b", percent_consumed: "160", status: "over" }),
      ]),
      160
    );
  });

  it("caps the axis so one runaway envelope cannot crush the rest", () => {
    assert.equal(
      budgetDomainMax([
        line({ name: "a", percent_consumed: "50" }),
        line({ name: "b", percent_consumed: "3000", status: "over" }),
      ]),
      PERCENT_DOMAIN_CAP
    );
  });

  it("ignores a null percentage rather than reading it as zero", () => {
    assert.equal(
      budgetDomainMax([line({ name: "a", percent_consumed: null })]),
      FULL_PERCENT
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
        name: "Groceries",
        overspend: "50.00",
        percent_consumed: "150",
        status: "over",
      }),
      150
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
      line({ name: "a", percent_consumed: "800", status: "over" }),
      PERCENT_DOMAIN_CAP
    );
    assert.equal(row.clipped, true);
    assert.equal(row.withinFraction + row.overFraction, 1);
  });

  it("gives a row inside its budget no overspend segment", () => {
    const row = budgetRow(line({ name: "a", percent_consumed: "50" }), 100);
    assert.equal(row.overFraction, 0);
    assert.equal(row.withinFraction, 0.5);
    assert.equal(row.clipped, false);
    assert.equal(row.isOver, false);
  });

  it("reads `over` from the sidecar's status, never from a comparison here", () => {
    // A row over by a cent must not round its way back into the clear. The
    // verdict was reached in `Decimal`; this layer only reports it.
    const row = budgetRow(
      line({ name: "a", percent_consumed: "100.004", status: "over" }),
      100
    );
    assert.equal(row.isOver, true);
  });
});

describe("budgetRow — spending against a zero allocation", () => {
  it("is its own kind, not a percentage of zero or one hundred", () => {
    // The state the sidecar reports honestly and a careless chart flattens.
    // There is no denominator, so there is no bar.
    const row = budgetRow(
      line({
        allocated: "0.00",
        name: "Dining",
        percent_consumed: null,
        spent: "80.00",
        status: "unbudgeted",
      }),
      100
    );

    assert.equal(row.kind, "unallocated");
    assert.equal(row.percent, null);
    assert.equal(row.withinFraction, 0);
    assert.equal(row.overFraction, 0);
  });

  it("has no percentage string a caller could print by accident", () => {
    // `formatConsumed` returns null rather than a string, so "0%" cannot be
    // rendered for this state — the compiler makes the caller handle it.
    assert.equal(formatConsumed(null), null);
    assert.equal(formatConsumed("42.4"), "42%");
    assert.equal(formatConsumed("240"), "240%");
  });

  it("keeps `unused` distinct from `unbudgeted`", () => {
    // An allocation nothing was spent against is a different state from no
    // allocation at all, and it still has a percentage: zero.
    const unused = budgetRow(
      line({
        name: "a",
        percent_consumed: "0",
        spent: "0.00",
        status: "unused",
      }),
      100
    );
    assert.equal(unused.kind, "measured");
    assert.equal(unused.isUnused, true);
    assert.equal(unused.percent, 0);
  });
});

describe("buildBudgetLayout", () => {
  it("puts what needs acting on first: over budget, then unallocated", () => {
    const layout = buildBudgetLayout([
      line({ name: "Zebra", percent_consumed: "10" }),
      line({ name: "Dining", percent_consumed: null, status: "unbudgeted" }),
      line({ name: "Groceries", percent_consumed: "140", status: "over" }),
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
      line({ name: "a", percent_consumed: "50" }),
      line({ name: "b", percent_consumed: "200", status: "over" }),
    ]);
    assert.equal(layout.domainMax, 200);
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
      line({ name: "b", percent_consumed: "50" }),
      line({ name: "a", percent_consumed: "50" }),
    ];
    assert.deepEqual(
      buildBudgetLayout(rows).rows.map((row) => row.name),
      buildBudgetLayout([...rows].reverse()).rows.map((row) => row.name)
    );
  });
});
