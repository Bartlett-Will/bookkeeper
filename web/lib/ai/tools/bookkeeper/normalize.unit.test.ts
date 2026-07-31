import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  toAllocation,
  toReviewQueue,
  toSpendingReport,
  toSyncStarted,
  toTransactionSearch,
  unwrap,
} from "./normalize";

// The bodies below are copied from the sidecar's `to_dict()` methods
// (`categorize/review.py`, `reports/spending.py`, `reports/search.py`,
// `envelope/allocate.py`, `jobs.py`), which is the only contract these four
// endpoints have until they appear in the OpenAPI schema.
//
// The recurring assertion in this file is that **amounts come out as the exact
// strings that went in**. Pydantic serializes `Decimal` to a string precisely
// so nobody's books round-trip through a float, and this mapping layer is
// where that would be undone.

describe("unwrap", () => {
  it("descends into the sidecar's {ok, summary, <key>} envelope", () => {
    assert.deepEqual(unwrap({ ok: true, queue: { total: 3 }, summary: "x" }), {
      total: 3,
    });
  });

  it("names the key nothing in particular", () => {
    // /review-queue calls it `queue`, /categorize calls it `result`.
    assert.deepEqual(unwrap({ ok: true, result: { a: 1 }, summary: "" }), {
      a: 1,
    });
  });

  it("passes a flat body through untouched", () => {
    const flat = { ok: true, shown: 1, total: 2 };
    assert.deepEqual(unwrap(flat), flat);
  });

  it("does not mistake a top-level array for the payload", () => {
    // A flattened body has `entries` at the top level; descending into it
    // would return the first entry as if it were the whole queue.
    const flat = { entries: [{ amount: "1.00" }], ok: true, total: 1 };
    assert.deepEqual(unwrap(flat), flat);
  });

  it("survives a body that is not an object", () => {
    assert.deepEqual(unwrap(null), {});
    assert.deepEqual(unwrap("nope"), {});
    assert.deepEqual(unwrap(undefined), {});
  });
});

describe("toSyncStarted", () => {
  it("reads a bare job handle", () => {
    assert.deepEqual(toSyncStarted({ job_id: "abc123" }), { job_id: "abc123" });
  });

  it("reads job_id off a full JobSnapshot without descending into `result`", () => {
    const snapshot = {
      error: null,
      finished_at: null,
      job_id: "abc123",
      kind: "sync",
      progress: 0,
      result: { imported: 43 },
      started_at: 1.0,
      state: "running",
      step: "",
      total: 43,
    };
    assert.deepEqual(toSyncStarted(snapshot), { job_id: "abc123" });
  });

  it("yields an empty id rather than throwing on a body it does not recognise", () => {
    assert.deepEqual(toSyncStarted({}), { job_id: "" });
    assert.deepEqual(toSyncStarted(null), { job_id: "" });
  });
});

describe("toReviewQueue", () => {
  const entry = {
    amount: "-163.36",
    asset_account: "Assets:Bank:Checking",
    confidence: 0.82,
    currency: "USD",
    current_account: "Expenses:Unknown",
    description: "WHOLEFDS MKT 10259",
    mcc: "5411",
    payee: "Whole Foods",
    posted_date: "2026-07-14",
    rationale: "matched memory",
    simplefin_id: "sf-1",
    suggested_account: "Expenses:Food:Groceries",
    tier: "memory",
  };

  it("maps the sidecar's ReviewQueue.to_dict() through the envelope", () => {
    const queue = toReviewQueue({
      ok: true,
      queue: {
        entries: [entry],
        errors: [],
        ok: true,
        shown: 1,
        total: 43,
        warnings: [],
      },
      summary: "43 transaction(s) awaiting review; showing 1.",
    });

    assert.equal(queue.ok, true);
    assert.equal(queue.total, 43);
    assert.equal(queue.shown, 1);
    assert.equal(queue.entries.length, 1);
    assert.deepEqual(queue.entries[0], entry);
  });

  it("keeps the amount as the exact string the ledger holds", () => {
    const queue = toReviewQueue({
      queue: { entries: [{ ...entry, amount: "-1000000.005" }] },
    });
    assert.equal(queue.entries[0].amount, "-1000000.005");
  });

  it("defaults total and shown to the number of entries", () => {
    const queue = toReviewQueue({ queue: { entries: [entry, entry] } });
    assert.equal(queue.total, 2);
    assert.equal(queue.shown, 2);
  });

  it("yields an empty queue rather than throwing on a malformed body", () => {
    const queue = toReviewQueue({ queue: { entries: "not an array" } });
    assert.deepEqual(queue.entries, []);
    assert.equal(queue.total, 0);
  });

  it("normalises a missing rationale to the empty string the UI expects", () => {
    const queue = toReviewQueue({
      queue: { entries: [{ ...entry, rationale: null }] },
    });
    assert.equal(queue.entries[0].rationale, "");
  });

  it("preserves the null-vs-value distinction on a suggestion", () => {
    const queue = toReviewQueue({
      queue: {
        entries: [{ ...entry, confidence: null, suggested_account: null }],
      },
    });
    assert.equal(queue.entries[0].suggested_account, null);
    assert.equal(queue.entries[0].confidence, null);
  });
});

describe("toSpendingReport", () => {
  const requested = { from: "2026-05-01", to: "2026-07-30" };

  const body = {
    ok: true,
    report: {
      currency: "USD",
      envelopes: [
        {
          name: "Groceries",
          points: [
            { amount: "412.37", period: "2026-05" },
            { amount: "388.10", period: "2026-06" },
          ],
          total: "800.47",
        },
        {
          name: "Transport",
          points: [{ amount: "64.00", period: "2026-05" }],
          total: "64.00",
        },
      ],
      errors: [],
      from_date: "2026-05-01",
      ok: true,
      period: "month",
      periods: ["2026-05", "2026-06", "2026-07"],
      to_date: "2026-07-30",
      total: "864.47",
      unmapped_accounts: ["Expenses:Unknown"],
      unmapped_total: "1204.11",
      warnings: [],
    },
    summary: "Spending by envelope…",
  };

  it("flattens the per-envelope series into one row per cell", () => {
    const report = toSpendingReport(body, requested);
    assert.deepEqual(report.points, [
      { amount: "412.37", envelope: "Groceries", period: "2026-05" },
      { amount: "388.10", envelope: "Groceries", period: "2026-06" },
      { amount: "64.00", envelope: "Transport", period: "2026-05" },
    ]);
  });

  it("renames from_date/to_date, which are reserved words in Python", () => {
    const report = toSpendingReport(body, requested);
    assert.equal(report.from, "2026-05-01");
    assert.equal(report.to, "2026-07-30");
  });

  it("carries the unmapped total rather than folding it into the total", () => {
    // With auto-apply off almost everything is still Expenses:Unknown, so a
    // chart that silently dropped this would look empty for the wrong reason.
    const report = toSpendingReport(body, requested);
    assert.equal(report.total, "864.47");
    assert.equal(report.unmapped_total, "1204.11");
  });

  it("keeps trailing zeros, which a float round-trip would eat", () => {
    const report = toSpendingReport(body, requested);
    const june = report.points.find((p) => p.period === "2026-06");
    assert.equal(june?.amount, "388.10");
  });

  it("falls back to the requested window when the sidecar omits it", () => {
    const report = toSpendingReport({ report: { envelopes: [] } }, requested);
    assert.equal(report.from, "2026-05-01");
    assert.equal(report.to, "2026-07-30");
    assert.equal(report.granularity, "month");
  });

  it("yields no points rather than throwing on a malformed body", () => {
    assert.deepEqual(toSpendingReport({}, requested).points, []);
    assert.deepEqual(toSpendingReport(null, requested).points, []);
  });
});

describe("toTransactionSearch", () => {
  const match = {
    account: "Assets:Bank:Checking",
    amount: "-163.36",
    categorized_account: "Expenses:Food:Groceries",
    currency: "USD",
    description: "WHOLEFDS MKT 10259",
    envelope: "Groceries",
    memo: null,
    payee: "Whole Foods",
    posted_date: "2026-07-14",
    simplefin_id: "sf-1",
  };

  it("maps TransactionSearch.to_dict() through the envelope", () => {
    const search = toTransactionSearch(
      {
        ok: true,
        results: {
          errors: [],
          limit: 20,
          matches: [match],
          ok: true,
          query: "whole foods",
          shown: 1,
          total: 1,
          truncated: false,
          warnings: [],
        },
        summary: "1 match",
      },
      "whole foods"
    );

    assert.equal(search.query, "whole foods");
    assert.equal(search.total, 1);
    assert.equal(search.truncated, false);
    assert.deepEqual(search.matches[0], match);
  });

  it("keeps a null simplefin_id, which ledger-native entries have", () => {
    const search = toTransactionSearch(
      { results: { matches: [{ ...match, simplefin_id: null }] } },
      "q"
    );
    assert.equal(search.matches[0].simplefin_id, null);
  });

  it("reports truncation so the UI can say the list is partial", () => {
    const search = toTransactionSearch(
      { results: { matches: [match], shown: 1, total: 87, truncated: true } },
      "q"
    );
    assert.equal(search.truncated, true);
    assert.equal(search.total, 87);
    assert.equal(search.shown, 1);
  });

  it("echoes the requested query when the sidecar omits it", () => {
    const search = toTransactionSearch({ results: { matches: [] } }, "pg&e");
    assert.equal(search.query, "pg&e");
  });
});

describe("toAllocation", () => {
  const requested = { currency: "USD", envelope: "Groceries" };

  it("maps AllocateResult.to_dict() for an accepted allocation", () => {
    const allocation = toAllocation(
      {
        ok: true,
        result: {
          allocated_on: "2026-07-30",
          amount: "250.00",
          available: "1312.44",
          currency: "USD",
          directive:
            '2026-07-30 custom "envelope" "allocate" "Groceries" 250.00 USD',
          envelope: "Groceries",
          errors: [],
          known_envelopes: ["Groceries", "Transport"],
          ok: true,
          over_allocated: false,
          path: "ledger/budget.beancount",
          warnings: [],
        },
        summary: "allocated 250.00 USD to Groceries",
      },
      requested
    );

    assert.equal(allocation.ok, true);
    assert.equal(allocation.amount, "250.00");
    assert.equal(allocation.available, "1312.44");
    assert.equal(allocation.allocated_on, "2026-07-30");
  });

  it("carries ok:false and the reason for a refused allocation", () => {
    // The sidecar answers 200 with ok:false for an unknown envelope, the same
    // way /verify reports a failing ledger. This is the payload the tool turns
    // into a message the model can relay.
    const allocation = toAllocation(
      {
        ok: false,
        result: {
          amount: "0",
          envelope: "Food",
          errors: ['no envelope named "Food"'],
          known_envelopes: ["Groceries", "Transport"],
          ok: false,
        },
        summary: "allocation failed",
      },
      { currency: "USD", envelope: "Food" }
    );

    assert.equal(allocation.ok, false);
    assert.deepEqual(allocation.errors, ['no envelope named "Food"']);
    assert.deepEqual(allocation.known_envelopes, ["Groceries", "Transport"]);
  });

  it("treats an unrecognised body as a refusal, never as a write", () => {
    // The safe default on the one tool that writes: if we cannot tell that it
    // succeeded, we must not say it did.
    assert.equal(toAllocation({}, requested).ok, false);
    assert.equal(toAllocation(null, requested).ok, false);
    assert.equal(toAllocation({ result: {} }, requested).ok, false);
  });

  it("flags an over-allocation without refusing it", () => {
    const allocation = toAllocation(
      { result: { available: "-40.00", ok: true, over_allocated: true } },
      requested
    );
    assert.equal(allocation.ok, true);
    assert.equal(allocation.over_allocated, true);
    assert.equal(allocation.available, "-40.00");
  });
});
