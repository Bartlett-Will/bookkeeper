import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  buildConfirmations,
  chosenAccount,
  entryKey,
  initialReviewState,
  reviewReducer,
  statusOf,
} from "./review-state";
import type { ConfirmResult, ReviewEntry } from "./types";

function entry(overrides: Partial<ReviewEntry> = {}): ReviewEntry {
  return {
    amount: "-19.96",
    asset_account: "Assets:Checking",
    confidence: 0.82,
    currency: "USD",
    current_account: "Expenses:Unknown",
    description: "TRADER JOES #123",
    mcc: "5411",
    payee: "Trader Joes",
    posted_date: "2026-07-04",
    rationale: "merchant code 5411",
    simplefin_id: "tx-1",
    suggested_account: "Expenses:Food:Groceries",
    tier: "mcc",
    ...overrides,
  };
}

function result(overrides: Partial<ConfirmResult> = {}): ConfirmResult {
  return {
    confirmed: 0,
    errors: [],
    files_written: [],
    learned: 0,
    ok: true,
    warnings: [],
    ...overrides,
  };
}

describe("entryKey", () => {
  it("keys on the account and id together", () => {
    // `simplefin_id` is unique per account, not globally.
    const a = entryKey({ asset_account: "Assets:A", simplefin_id: "1" });
    const b = entryKey({ asset_account: "Assets:B", simplefin_id: "1" });
    assert.notEqual(a, b);
  });

  it("separates the two components, where a bare concatenation would collide", () => {
    // Both pairs concatenate to "Assets:AB1". Only the separator keeps them
    // apart, and a collision here would confirm one transaction as another.
    const a = entryKey({ asset_account: "Assets:AB", simplefin_id: "1" });
    const b = entryKey({ asset_account: "Assets:A", simplefin_id: "B1" });
    assert.notEqual(a, b);
  });
});

describe("buildConfirmations", () => {
  it("sends the correction when one was chosen, else the suggestion", () => {
    const entries = [entry(), entry({ simplefin_id: "tx-2" })];
    const state = reviewReducer(initialReviewState, {
      account: "Expenses:Dining",
      key: entryKey(entries[1]),
      type: "choose",
    });
    const { confirmations } = buildConfirmations(
      state,
      entries,
      entries.map((e) => entryKey(e))
    );
    assert.deepEqual(
      confirmations.map((c) => c.account),
      ["Expenses:Food:Groceries", "Expenses:Dining"]
    );
  });

  it("reports rows with no account rather than silently dropping them", () => {
    // The sidecar rejects the whole batch if any account is unresolvable, so
    // one bad row would cost every other approval in the click.
    const entries = [
      entry(),
      entry({ simplefin_id: "tx-2", suggested_account: null }),
    ];
    const { confirmations, sent, skipped } = buildConfirmations(
      initialReviewState,
      entries,
      entries.map((e) => entryKey(e))
    );
    assert.equal(confirmations.length, 1);
    assert.deepEqual(sent, [entryKey(entries[0])]);
    assert.deepEqual(skipped, [entryKey(entries[1])]);
  });

  it("ignores rows that are already confirmed or in flight", () => {
    const entries = [entry()];
    const key = entryKey(entries[0]);
    const state = reviewReducer(initialReviewState, {
      keys: [key],
      type: "submit-start",
    });
    const { confirmations } = buildConfirmations(state, entries, [key]);
    assert.equal(confirmations.length, 0);
  });
});

describe("reviewReducer", () => {
  const entries = [entry(), entry({ simplefin_id: "tx-2" })];
  const keys = entries.map((e) => entryKey(e));

  it("does not mark a row confirmed until the server accounts for it", () => {
    const submitting = reviewReducer(initialReviewState, {
      keys,
      type: "submit-start",
    });
    assert.equal(statusOf(submitting, keys[0]), "submitting");

    const done = reviewReducer(submitting, {
      keys,
      result: result({ confirmed: 2, ok: true }),
      type: "submit-result",
    });
    assert.equal(statusOf(done, keys[0]), "confirmed");
    assert.equal(statusOf(done, keys[1]), "confirmed");
  });

  it("marks the whole batch uncertain when the count falls short", () => {
    // `confirm_categorizations` returns a count, not the set of keys it
    // applied, so a shortfall leaves the UI unable to say which rows landed.
    // Guessing would mean showing a still-uncategorized row as confirmed.
    const submitting = reviewReducer(initialReviewState, {
      keys,
      type: "submit-start",
    });
    const short = reviewReducer(submitting, {
      keys,
      result: result({ confirmed: 1, ok: true }),
      type: "submit-result",
    });
    assert.equal(statusOf(short, keys[0]), "uncertain");
    assert.equal(statusOf(short, keys[1]), "uncertain");
    assert.equal(short.needsReload, true);
    assert.match(short.notice ?? "", /Reload the queue/);
  });

  it("keeps the user's selections and corrections when the batch is rejected", () => {
    // The sidecar validates the whole batch before touching the ledger, so a
    // rejection means nothing was written and the user should be able to fix
    // the offending account and resubmit rather than start over.
    let state = reviewReducer(initialReviewState, { keys, type: "select" });
    state = reviewReducer(state, {
      account: "Expenses:Dining",
      key: keys[0],
      type: "choose",
    });
    state = reviewReducer(state, { keys, type: "submit-start" });
    state = reviewReducer(state, {
      keys,
      result: result({ errors: ["Expenses:Dining is not open"], ok: false }),
      type: "submit-result",
    });

    assert.equal(statusOf(state, keys[0]), "failed");
    assert.equal(state.chosen[keys[0]], "Expenses:Dining");
    assert.equal(state.selected.size, 2, "the selection survives a rejection");
    assert.equal(state.errors[keys[0]], "Expenses:Dining is not open");
    assert.equal(state.needsReload, false);
  });

  it("keeps selections through a transport failure as well", () => {
    // A 400 from a stale account list arrives here, via a thrown fetch.
    let state = reviewReducer(initialReviewState, { keys, type: "select" });
    state = reviewReducer(state, { keys, type: "submit-start" });
    state = reviewReducer(state, {
      keys,
      message: "The sidecar returned 400",
      type: "submit-error",
    });
    assert.equal(statusOf(state, keys[0]), "failed");
    assert.equal(state.selected.size, 2);
  });

  it("clears a row's failure when the user picks a different account", () => {
    let state = reviewReducer(initialReviewState, {
      keys,
      type: "submit-start",
    });
    state = reviewReducer(state, {
      keys,
      message: "rejected",
      type: "submit-error",
    });
    assert.equal(statusOf(state, keys[0]), "failed");

    state = reviewReducer(state, {
      account: "Expenses:Food",
      key: keys[0],
      type: "choose",
    });
    assert.equal(statusOf(state, keys[0]), "awaiting");
    assert.equal(state.errors[keys[0]], undefined);
    assert.equal(
      statusOf(state, keys[1]),
      "failed",
      "only the edited row resets"
    );
  });

  it("refuses to select a row that is no longer actionable", () => {
    let state = reviewReducer(initialReviewState, {
      keys,
      type: "submit-start",
    });
    state = reviewReducer(state, {
      keys,
      result: result({ confirmed: 2, ok: true }),
      type: "submit-result",
    });
    state = reviewReducer(state, { key: keys[0], type: "toggle" });
    assert.equal(state.selected.has(keys[0]), false);
  });

  it("resolves the account a row would be confirmed as", () => {
    const state = reviewReducer(initialReviewState, {
      account: "Expenses:Dining",
      key: keys[0],
      type: "choose",
    });
    assert.equal(chosenAccount(state, entries[0]), "Expenses:Dining");
    assert.equal(chosenAccount(state, entries[1]), "Expenses:Food:Groceries");
    assert.equal(
      chosenAccount(initialReviewState, entry({ suggested_account: null })),
      null
    );
  });
});
