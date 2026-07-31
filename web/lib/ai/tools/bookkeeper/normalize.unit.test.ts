import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  describeValidationFailure,
  toAllocation,
  toReviewQueue,
  toSpendingReport,
  toSyncStarted,
  toTransactionSearch,
} from "./normalize";

// The bodies below are the sidecar's real response shapes, confirmed against
// the live `/openapi.json` rather than inferred from the Python. All four
// Phase 4 endpoints are **flat** — the domain fields sit at the top level
// beside `ok` and `summary`. Only `/review-queue` wraps, under `queue`.
//
// The recurring assertion in this file is that **amounts come out as the exact
// strings that went in**. Pydantic serializes `Decimal` to a string precisely
// so nobody's books round-trip through a float, and this mapping layer is
// where that would be undone.

describe("toSyncStarted", () => {
  it("maps SyncStartResponse", () => {
    assert.deepEqual(
      toSyncStarted({
        job_id: "abc123",
        kind: "sync",
        started: true,
        state: "running",
      }),
      { job_id: "abc123", started: true }
    );
  });

  it("carries started:false, which means a sync was already running", () => {
    // Not an error: SimpleFIN allows ~24 requests a day, so being handed the
    // existing job's id instead of launching a second one is the point.
    const handedExisting = toSyncStarted({
      job_id: "abc123",
      kind: "sync",
      started: false,
      state: "running",
    });
    assert.equal(handedExisting.started, false);
    assert.equal(handedExisting.job_id, "abc123");
  });

  it("reads as already-running rather than newly-started when the flag is absent", () => {
    // Claiming a sync began when it did not is the misleading direction.
    assert.equal(toSyncStarted({ job_id: "abc123" }).started, false);
  });

  it("yields an empty id rather than throwing on a body it does not recognise", () => {
    assert.deepEqual(toSyncStarted({}), { job_id: "", started: false });
    assert.deepEqual(toSyncStarted(null), { job_id: "", started: false });
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

  /** `SpendingReportResponse`, flat. */
  const body = {
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
    summary: "Spending by envelope…",
    to_date: "2026-07-30",
    total: "864.47",
    unmapped_accounts: ["Expenses:Unknown"],
    unmapped_total: "1204.11",
    warnings: [],
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
    const report = toSpendingReport({ envelopes: [] }, requested);
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
        errors: [],
        limit: 20,
        matches: [match],
        ok: true,
        query: "whole foods",
        summary: "1 match",
        total: 1,
        truncated: false,
        warnings: [],
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
      { matches: [{ ...match, simplefin_id: null }] },
      "q"
    );
    assert.equal(search.matches[0].simplefin_id, null);
  });

  it("reports truncation so the UI can say the list is partial", () => {
    const search = toTransactionSearch(
      { matches: [match], total: 87, truncated: true },
      "q"
    );
    assert.equal(search.truncated, true);
    assert.equal(search.total, 87);
    assert.equal(search.shown, 1);
  });

  it("echoes the requested query when the sidecar omits it", () => {
    const search = toTransactionSearch({ matches: [] }, "pg&e");
    assert.equal(search.query, "pg&e");
  });
});

describe("toAllocation", () => {
  const requested = { currency: "USD", envelope: "Groceries" };

  it("maps AllocateResponse for an accepted allocation", () => {
    const allocation = toAllocation(
      {
        allocated_on: "2026-07-30",
        amount: "250.00",
        available: "1312.44",
        commit: {
          committed: true,
          files: ["ledger/budget.beancount"],
          message: "Allocate 250.00 USD to Groceries",
          ok: true,
          sha: "9f2c1ab",
          warnings: [],
        },
        currency: "USD",
        directive:
          '2026-07-30 custom "envelope" "allocate" "Groceries" 250.00 USD',
        envelope: "Groceries",
        errors: [],
        known_envelopes: ["Groceries", "Transport"],
        ok: true,
        over_allocated: false,
        path: "ledger/budget.beancount",
        summary: "allocated 250.00 USD to Groceries",
        warnings: [],
      },
      requested
    );

    assert.equal(allocation.ok, true);
    assert.equal(allocation.amount, "250.00");
    assert.equal(allocation.available, "1312.44");
    assert.equal(allocation.allocated_on, "2026-07-30");
  });

  it("carries the commit sha, which is how an allocation gets undone", () => {
    // Git is the undo system (PLAN.md §9), so the sha is the one field the UI
    // needs in order to tell someone what to revert.
    //
    // `commit` is also the field that broke the mapping this file previously
    // had: it is the only object-valued property on the response, and the old
    // heuristic — "the payload is the first object that is not ok/summary" —
    // returned the git commit *as* the allocation. Every amount then read 0.
    const allocation = toAllocation(
      {
        amount: "250.00",
        commit: {
          committed: true,
          files: ["ledger/budget.beancount"],
          message: "Allocate 250.00 USD to Groceries",
          ok: true,
          sha: "9f2c1ab",
          warnings: [],
        },
        ok: true,
      },
      requested
    );

    assert.equal(allocation.amount, "250.00", "the commit is not the payload");
    assert.equal(allocation.commit?.sha, "9f2c1ab");
    assert.equal(allocation.commit?.committed, true);
  });

  it("carries a null commit when nothing was written", () => {
    assert.equal(
      toAllocation({ commit: null, ok: false }, requested).commit,
      null
    );
  });

  it("carries ok:false and the reason for a refused allocation", () => {
    // The sidecar answers 200 with ok:false for an unknown envelope, the same
    // way /verify reports a failing ledger. This is the payload the tool turns
    // into a message the model can relay.
    const allocation = toAllocation(
      {
        amount: "0",
        envelope: "Food",
        errors: ['no envelope named "Food"'],
        known_envelopes: ["Groceries", "Transport"],
        ok: false,
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
    assert.equal(toAllocation({ summary: "" }, requested).ok, false);
  });

  it("flags an over-allocation without refusing it", () => {
    const allocation = toAllocation(
      { available: "-40.00", ok: true, over_allocated: true },
      requested
    );
    assert.equal(allocation.ok, true);
    assert.equal(allocation.over_allocated, true);
    assert.equal(allocation.available, "-40.00");
  });
});

describe("describeValidationFailure", () => {
  // Since the sidecar set `extra="forbid"`, a field this layer should not send
  // is a 422 rather than a silently dropped value — which is exactly what made
  // the `date`/`allocated_on` bug invisible. These are real bodies from the
  // running sidecar, not invented ones.

  it("names the offending field instead of handing the model FastAPI's JSON", () => {
    const detail = [
      {
        input: "2026-07-22",
        loc: ["body", "date"],
        msg: "Extra inputs are not permitted",
        type: "extra_forbidden",
      },
    ];
    const explained = describeValidationFailure(detail);
    assert.match(explained ?? "", /date/);
    assert.match(explained ?? "", /not accepted/);
    // §3.3: a JSON array in an 8B's context is the loose error handling that
    // produces invocation loops.
    assert.ok(!explained?.includes("{"), explained ?? "");
    assert.ok(!explained?.includes("extra_forbidden"), explained ?? "");
  });

  it("keeps the array index, so a 40-item batch says which element is wrong", () => {
    const explained = describeValidationFailure([
      {
        loc: ["body", "confirmations", 0, "simplefin_id"],
        msg: "Field required",
        type: "missing",
      },
    ]);
    assert.match(explained ?? "", /confirmations\[0\]\.simplefin_id/);
  });

  it("lists every offending field, not just the first", () => {
    const explained = describeValidationFailure([
      { loc: ["body", "date"], type: "extra_forbidden" },
      { loc: ["body", "memo"], type: "extra_forbidden" },
    ]);
    assert.match(explained ?? "", /date/);
    assert.match(explained ?? "", /memo/);
    assert.match(explained ?? "", /fields/);
  });

  it("declines anything that is not a recognisable validation error", () => {
    // The caller then falls back to the transport's own message rather than
    // inventing an explanation.
    assert.equal(describeValidationFailure(null), null);
    assert.equal(describeValidationFailure([]), null);
    assert.equal(describeValidationFailure("boom"), null);
    assert.equal(describeValidationFailure([{ loc: ["body"] }]), null);
  });
});
