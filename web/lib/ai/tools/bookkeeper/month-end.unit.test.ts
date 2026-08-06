import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { BookkeeperClient, MonthEndReport } from "./client";
import { getMonthEndReport } from "./get-month-end-report";
import { toMonthEndReport } from "./normalize";

// Two things are under test here and only one of them is the normalizer.
//
// The other is `sayableFacts` — the short sentences handed to the model as the
// facts it *has*, which is Phase 5's answer to the residue PLAN.md §5.3 rule
// 1's amendment left open. Those sentences are the only thing standing between
// a trend report and "nothing looks unusual", so the cases that matter most
// below are the ones asserting a sentence is **absent**: a reassurance the data
// does not support must not be sayable, and an empty array is not evidence.

/**
 * A live `GET /reports/month-end` body, field for field, from a sidecar run
 * against a copy of the ledger. Not written from the schema — Phase 4's two
 * bugs both typechecked and both passed tests written from the schema, and
 * what caught them was reading a real body. Note `"0"` where a hand-written
 * fixture would say `"0.00"`. Re-captured after the sidecar renamed
 * `percent_consumed` to `consumed_ratio` and changed its type from a decimal
 * string to a JSON number — a rename that left the old normalizer reading a
 * field that no longer existed, returning null for every envelope, and
 * typechecking perfectly while doing it.
 */
const LIVE_BODY = {
  allocated_total: "0",
  asof: "2026-07-31",
  available: "137457.78",
  budgeted_cash: "137457.78",
  categorization: "none",
  categorized_count: 0,
  categorized_share: "0.0000",
  closing_total: "0",
  complete: true,
  coverage: "complete",
  currency: "USD",
  data_through: "2026-07-31",
  days_elapsed: 31,
  days_in_month: 31,
  envelopes: [
    {
      allocated: "0",
      closing_balance: "0",
      consumed_ratio: null,
      direction: "flat",
      direction_reason: "no spending in any of the 3 periods",
      name: "Dining Out",
      opening_balance: "0",
      over_budget: false,
      overspend: "0",
      overspent: false,
      periods_observed: 3,
      periods_required: 3,
      remaining: "0",
      spent: "0",
      status: "unused",
    },
  ],
  errors: [],
  from_date: "2026-07-01",
  label: "July 2026",
  month: "2026-07",
  ok: true,
  opening_total: "0",
  outliers: [],
  spent_total: "0",
  summary: "…",
  through: "2026-07-31",
  to_date: "2026-07-31",
  total_overspend: "0",
  total_spend: "17.00",
  transactions: 117,
  trend_from: "2026-05-02",
  trend_to: "2026-07-31",
  uncategorized_count: 114,
  unjudged: [
    "Dining Out",
    "Groceries",
    "Health",
    "Rent",
    "Subscriptions",
    "Transport",
    "Utilities",
  ],
  unmapped_accounts: ["Expenses:Unknown"],
  unmapped_total: "17.00",
  warnings: ["budget: 17.00 USD of spending in this window is not mapped"],
};

describe("toMonthEndReport", () => {
  it("reads the day bounds from from_date/to_date, not from/to", () => {
    // The asymmetry that already cost Phase 4 a bug on `/reports/spending`:
    // the request says `from`, the response says `from_date`, because `from`
    // is a Python keyword the sidecar can alias going in and not coming out.
    // Read the wrong one and every month-end card is dated the empty string.
    const report = toMonthEndReport(LIVE_BODY);
    assert.equal(report.from, "2026-07-01");
    assert.equal(report.to, "2026-07-31");
  });

  it("carries every amount through as the string the sidecar sent", () => {
    const report = toMonthEndReport(LIVE_BODY);
    assert.equal(report.unmapped_total, "17.00");
    assert.equal(report.total_spend, "17.00");
    assert.equal(report.available, "137457.78");
    // Carried verbatim. `Decimal` has serialized this field as both "0.0000"
    // and "0E+2" across sidecar revisions, so anything comparing it to a
    // literal zero string is wrong; parse it at the render edge or not at all.
    assert.equal(report.categorized_share, "0.0000");
  });

  it("keeps a null consumed_ratio null rather than making it zero", () => {
    // Against a zero allocation both 0 and 1 are lies, in opposite directions.
    const report = toMonthEndReport(LIVE_BODY);
    assert.equal(report.envelopes[0].consumed_ratio, null);
  });

  it("reads consumed_ratio as the number it is, not as a string", () => {
    // The regression for the rename. `consumed_ratio` is the one field in this
    // payload that is a real JSON number — a ratio is not money — and reading
    // it with a string reader yields null for every envelope, which renders as
    // "nothing was budgeted" across the whole report.
    const report = toMonthEndReport({
      ...LIVE_BODY,
      envelopes: [{ ...LIVE_BODY.envelopes[0], consumed_ratio: 1.5 }],
    });
    assert.equal(report.envelopes[0].consumed_ratio, 1.5);
  });

  it("carries the counts that make an abstention checkable", () => {
    const report = toMonthEndReport(LIVE_BODY);
    assert.equal(report.envelopes[0].periods_observed, 3);
    assert.equal(report.envelopes[0].periods_required, 3);
    assert.equal(report.uncategorized_count, 114);
    assert.equal(report.categorized_count, 0);
  });

  it("carries the trend and outlier fields a chart needs", () => {
    // These are the data path for two of the three Phase 5 views. An object
    // literal is a whitelist, so a field omitted here is a view with nothing
    // to render, and nothing downstream can tell that from an empty report.
    const report = toMonthEndReport(LIVE_BODY);
    assert.deepEqual(report.trend_from, "2026-05-02");
    assert.deepEqual(report.trend_to, "2026-07-31");
    assert.equal(report.unjudged.length, 7);
    assert.deepEqual(report.outliers, []);
    assert.equal(report.envelopes[0].direction, "flat");
    assert.equal(report.envelopes[0].direction_reason.length > 0, true);
    assert.equal(report.envelopes[0].status, "unused");
    assert.equal(report.envelopes[0].remaining, "0");
    assert.equal(report.envelopes[0].overspend, "0");
  });

  it("keeps overspent and over_budget as separate verdicts", () => {
    const report = toMonthEndReport({
      ...LIVE_BODY,
      envelopes: [
        { ...LIVE_BODY.envelopes[0], over_budget: true, overspent: false },
      ],
    });
    assert.equal(report.envelopes[0].overspent, false);
    assert.equal(report.envelopes[0].over_budget, true);
  });

  it("defaults a missing direction to abstention, not to flat", () => {
    // `flat` claims the slope was measured and came out small. A body that did
    // not carry a direction supports no such claim, and a card reading an
    // empty string could render it as a measured verdict.
    const report = toMonthEndReport({
      ...LIVE_BODY,
      envelopes: [{ ...LIVE_BODY.envelopes[0], direction: undefined }],
    });
    assert.equal(report.envelopes[0].direction, "insufficient_data");
    assert.ok(report.envelopes[0].direction_reason.length > 0);
  });

  it("defaults coverage and categorization to their cautious ends", () => {
    // A body we could not read must not claim the month is finished and fully
    // categorized — those two words are what `sayableFacts` speaks from.
    const report = toMonthEndReport({});
    assert.equal(report.coverage, "no-data");
    assert.equal(report.categorization, "none");
  });

  it("treats an absent unjudged as every envelope unjudged, not none", () => {
    // The difference between "nothing was found" and "nothing was looked at".
    // Defaulting to `[]` would mean "every envelope was examined", which is
    // what licenses a reassurance the body never supported.
    const { unjudged, ...withoutUnjudged } = LIVE_BODY;
    assert.ok(unjudged.length > 0);
    const report = toMonthEndReport(withoutUnjudged);
    assert.deepEqual(report.unjudged, ["Dining Out"]);
  });
});

/** Runs the tool against a stub and returns what the model would be told. */
async function modelText(report: MonthEndReport): Promise<string> {
  const client = {
    getMonthEndReport: () => Promise.resolve({ data: report, ok: true }),
  } as unknown as BookkeeperClient;
  const tool = getMonthEndReport(client) as unknown as {
    execute: (i: unknown, o: unknown) => Promise<unknown>;
    toModelOutput: (o: { output: unknown }) => { value: string };
  };
  const output = await tool.execute({}, {});
  return tool.toModelOutput({ output }).value;
}

const live = toMonthEndReport(LIVE_BODY);

describe("what the model is told about a month-end report", () => {
  it("still states that it was not shown the figures", async () => {
    // The wording §5.3's amendment pinned: stating the situation rather than
    // prohibiting the numbers is what stopped `qwen3:8b` inventing them.
    const text = await modelText(live);
    assert.match(text, /NOT shown the figures/);
  });

  it("contains no amount from the payload", async () => {
    const text = await modelText(live);
    for (const amount of ["17.00", "137457.78", "0E+2"]) {
      assert.ok(
        !text.includes(amount),
        `the model was handed the figure ${amount}`
      );
    }
  });

  it("says the month is largely uncategorized when it is", async () => {
    // Auto-apply is off, so this is the normal case and the most important
    // sentence in the set: without it the model summarises a month whose
    // per-envelope table describes almost none of the spending.
    const text = await modelText(live);
    assert.match(text, /None of that month's spending has been filed/);
  });

  it("does not say nothing looked unusual when nothing was examined", async () => {
    // `outliers: []` with every envelope in `unjudged` is "we did not look",
    // and it must not read as "we looked and all was well".
    const text = await modelText(live);
    assert.ok(!text.includes("flagged as unusually large"));
  });

  it("says nothing was flagged only once something was judged", async () => {
    const text = await modelText({ ...live, unjudged: [] });
    assert.match(text, /No transaction that month was flagged/);
  });

  it("reports a flagged outlier when there is one", async () => {
    const text = await modelText({
      ...live,
      outliers: [
        {
          amount: "812.40",
          description: "…",
          envelope: "Groceries",
          median: "60.00",
          posted_date: "2026-07-14",
          scale: "20.00",
          scale_method: "mad",
          score: "37.6",
          threshold: "3.5",
        },
      ],
    });
    assert.match(text, /flagged as unusually large/);
    // The finding, never its size.
    assert.ok(!text.includes("812.40"));
  });

  it("says only that there is nothing to report for a future month", async () => {
    // Every other fact would be vacuously true of a month that has not
    // happened, and "no envelope ended overspent" is a reassuring thing to say
    // about one.
    const text = await modelText({ ...live, coverage: "future" });
    assert.match(text, /has not started yet/);
    assert.ok(!text.includes("overspent"));
    assert.ok(!text.includes("filed to an envelope"));
  });

  it("distinguishes a month in progress from a finished one", async () => {
    const text = await modelText({ ...live, coverage: "in-progress" });
    assert.match(text, /not over, so these are running figures/);
  });

  it("does not call every envelope healthy when there are no envelopes", async () => {
    const text = await modelText({ ...live, envelopes: [] });
    assert.ok(!text.includes("No envelope ended that month overspent"));
  });

  it("names no envelope", async () => {
    // A named envelope invites a sentence about that envelope; the card is
    // already showing which one.
    const text = await modelText({
      ...live,
      envelopes: [{ ...live.envelopes[0], name: "Groceries", overspent: true }],
    });
    assert.match(text, /One or more envelopes ended that month overspent/);
    assert.ok(!text.includes("Groceries"));
  });
});

describe("get_month_end_report arguments", () => {
  const captured: Array<{ month?: string }> = [];
  const client = {
    getMonthEndReport: (input: { month?: string }) => {
      captured.push(input);
      return Promise.resolve({ data: live, ok: true });
    },
  } as unknown as BookkeeperClient;

  const run = (input: unknown) =>
    (
      getMonthEndReport(client) as unknown as {
        execute: (
          i: unknown,
          o: unknown
        ) => Promise<{ status: string; message: string }>;
      }
    ).execute(input, {});

  it("forwards an absent month as absent", async () => {
    // Not as a wall-clock default: the sidecar's default is the month of the
    // ledger's last transaction, and only the sidecar knows that. Measured,
    // `qwen3:8b` answers "how did July go" with `2023-07` when left to supply
    // the year itself.
    captured.length = 0;
    await run({});
    assert.deepEqual(captured, [{ month: undefined }]);
  });

  it("rejects an impossible month before it reaches the ledger service", async () => {
    const months = ["2026-13", "2026-00", "July", "2026-7", "2026"];
    const outputs = await Promise.all(months.map((month) => run({ month })));
    for (const [i, output] of outputs.entries()) {
      assert.equal(output.status, "error", `${months[i]} was accepted`);
      assert.match(output.message, /not a real calendar month/);
    }
  });

  it("accepts a well-formed month and passes it through untouched", async () => {
    captured.length = 0;
    await run({ month: "2026-06" });
    assert.deepEqual(captured, [{ month: "2026-06" }]);
  });
});
