import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  describeBacklog,
  describeCategorization,
  describePeriod,
} from "./month-end-model";
import type { MonthEndReportData } from "./types";

function monthEnd(overrides: Partial<MonthEndReportData>): MonthEndReportData {
  return {
    allocated_total: "1000.00",
    asof: "2026-07-31",
    available: "200.00",
    budgeted_cash: "1200.00",
    categorization: "full",
    categorized_count: 40,
    categorized_share: "1",
    closing_total: "300.00",
    complete: true,
    coverage: "complete",
    currency: "USD",
    data_through: "2026-07-31",
    days_elapsed: 31,
    days_in_month: 31,
    envelopes: [],
    errors: [],
    from: "2026-07-01",
    label: "July 2026",
    month: "2026-07",
    ok: true,
    opening_total: "0.00",
    outliers: [],
    spent_total: "700.00",
    summary: "",
    through: "2026-07-31",
    to: "2026-07-31",
    total_overspend: "0.00",
    total_spend: "700.00",
    transactions: 40,
    trend_from: "2026-02-01",
    trend_to: "2026-07-31",
    uncategorized_count: 0,
    unjudged: [],
    unmapped_accounts: [],
    unmapped_total: "0.00",
    warnings: [],
    ...overrides,
  };
}

describe("describePeriod — a partial month is not a month", () => {
  it("adds no qualifier to a finished month", () => {
    const period = describePeriod(monthEnd({ coverage: "complete" }));
    assert.equal(period.partial, false);
    assert.equal(period.qualifier, "");
    assert.equal(period.totalSuffix, "");
    assert.equal(period.empty, false);
    assert.equal(period.title, "July 2026");
  });

  it("marks a running month partial", () => {
    // A running month rendered like a finished one shows a month of
    // near-nothing and invites exactly the wrong conclusion.
    const period = describePeriod(
      monthEnd({ coverage: "in-progress", label: "August 2026" })
    );
    assert.equal(period.partial, true);
    assert.equal(period.totalSuffix, "so far");
    assert.match(period.qualifier, /in progress/);
  });

  it("keeps `partial` apart from `in-progress` — different fixes", () => {
    // In progress means the month is not over: wait. Partial means the month
    // is over but the data stops part-way: sync.
    const running = describePeriod(monthEnd({ coverage: "in-progress" }));
    const stale = describePeriod(
      monthEnd({ coverage: "partial", data_through: "2026-07-12" })
    );
    assert.notEqual(running.qualifier, stale.qualifier);
    assert.match(stale.qualifier, /2026-07-12/);
  });

  it("says a future or empty month is empty rather than reporting zeros", () => {
    // Every total in an empty month is zero, and a report of zeros reads as a
    // month of no spending rather than a month with no data.
    const future = describePeriod(monthEnd({ coverage: "future" }));
    assert.equal(future.empty, true);
    assert.match(future.emptyReason, /not started/);

    const noData = describePeriod(monthEnd({ coverage: "no-data" }));
    assert.equal(noData.empty, true);
    assert.match(noData.emptyReason, /no transactions/);
  });

  it("treats an unrecognised coverage as partial, never as complete", () => {
    // Defaulting the other way turns a body we failed to understand into a
    // confident statement that the month is finished.
    const period = describePeriod(monthEnd({ coverage: "weird" }));
    assert.equal(period.partial, true);
    assert.equal(period.totalSuffix, "so far");
  });

  it("falls back to the month code when the label is missing", () => {
    assert.equal(describePeriod(monthEnd({ label: "  " })).title, "2026-07");
  });
});

describe("describeCategorization — an uncategorized month is not a frugal one", () => {
  it("says nothing when everything is filed", () => {
    const described = describeCategorization(
      monthEnd({ categorization: "full" })
    );
    assert.equal(described.level, "full");
    assert.equal(described.warns, false);
    assert.equal(described.headline, "");
  });

  it("does not warn about a month that genuinely had no spending", () => {
    // `no-spend` is not a categorization problem and must not wear a warning
    // that says it is.
    const described = describeCategorization(
      monthEnd({ categorization: "no-spend" })
    );
    assert.equal(described.level, "no-spend");
    assert.equal(described.warns, false);
  });

  it("separates nothing-filed from partly-filed", () => {
    // Different warnings: with nothing filed every envelope figure is zero and
    // the card contradicts itself; with some, the figures are real but low.
    const none = describeCategorization(monthEnd({ categorization: "none" }));
    const partial = describeCategorization(
      monthEnd({ categorization: "partial" })
    );

    assert.equal(none.level, "none");
    assert.equal(none.severe, true);
    assert.equal(partial.level, "partial");
    assert.equal(partial.severe, false);
    assert.notEqual(none.headline, partial.headline);
    assert.notEqual(none.consequence, partial.consequence);
  });

  it("explains the consequence, not just the state", () => {
    // The state alone is a label. What a reader needs is what it does to the
    // numbers underneath it.
    const none = describeCategorization(monthEnd({ categorization: "none" }));
    assert.match(none.consequence, /zero/);
    assert.ok(none.warns);
  });

  it("warns on an unrecognised value — a missed caveat beats a spare one", () => {
    const described = describeCategorization(
      monthEnd({ categorization: "mystery" })
    );
    assert.equal(described.warns, true);
  });
});

describe("describeBacklog", () => {
  // Replaces an earlier percentage label. A share had to be parsed out of a
  // money-derived string to be rendered, and "17% filed" is a weaker warning
  // than the count and the amounts it was computed from: "$0.00 in envelopes"
  // and "87 transactions, $4,102 of $4,102 unfiled" are the same state, and
  // only the second reads as something to act on.
  const money = (amount: string) => `$${amount}`;

  it("names the count and both amounts, not a derived percentage", () => {
    const described = describeBacklog(
      monthEnd({
        total_spend: "4102.00",
        uncategorized_count: 87,
        unmapped_total: "4102.00",
      }),
      money
    );

    assert.equal(described.count, 87);
    assert.match(described.sentence, /87 transactions are not filed/);
    assert.match(described.sentence, /\$4102\.00 of the \$4102\.00/);
    assert.doesNotMatch(described.sentence, /%/);
  });

  it("says nothing at all when there is no backlog", () => {
    const described = describeBacklog(
      monthEnd({ uncategorized_count: 0, unmapped_total: "0.00" }),
      money
    );

    assert.equal(described.count, 0);
    assert.equal(described.sentence, "");
  });

  it("agrees in number with a single unfiled transaction", () => {
    const described = describeBacklog(
      monthEnd({ uncategorized_count: 1, unmapped_total: "12.00" }),
      money
    );

    assert.match(described.sentence, /1 transaction is not filed/);
  });

  it("treats a negative count as no backlog rather than rendering it", () => {
    // The sidecar cannot send one, but a count is the one field here that
    // would render as a sentence if it arrived malformed, and "-3
    // transactions are not filed" is worse than silence.
    const described = describeBacklog(
      monthEnd({ uncategorized_count: -3 }),
      money
    );

    assert.equal(described.count, 0);
    assert.equal(described.sentence, "");
  });
});
