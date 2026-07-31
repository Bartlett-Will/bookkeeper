import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { categorizationTargets, parseConfirmResult } from "./confirm-client";

describe("parseConfirmResult", () => {
  it("reads the payload out of the sidecar's envelope", () => {
    // Every sidecar payload arrives wrapped: `{ok, summary, <key>: {...}}`.
    const result = parseConfirmResult({
      ok: true,
      result: {
        confirmed: 40,
        errors: [],
        files_written: ["ledger/transactions/2026-07.beancount"],
        learned: 12,
        ok: true,
        warnings: [],
      },
      summary: "confirmed 40 transaction(s)",
    });
    assert.equal(result.confirmed, 40);
    assert.equal(result.learned, 12);
    assert.equal(result.ok, true);
    assert.deepEqual(result.files_written, [
      "ledger/transactions/2026-07.beancount",
    ]);
  });

  it("accepts a flattened payload too", () => {
    const result = parseConfirmResult({
      confirmed: 3,
      errors: [],
      files_written: [],
      learned: 0,
      ok: true,
      warnings: [],
    });
    assert.equal(result.confirmed, 3);
    assert.equal(result.ok, true);
  });

  it("throws rather than inventing a confirmed count", () => {
    // This is the bug the function exists to prevent. Casting the envelope
    // straight across leaves `confirmed` undefined; `keys.length - undefined`
    // is NaN, `NaN > 0` is false, so the reducer's shortfall check passes and
    // every row is marked confirmed on no evidence at all.
    assert.throws(
      () => parseConfirmResult({ ok: true, summary: "confirmed 40" }),
      /no `confirmed` count/
    );
  });

  it("rejects a non-object body", () => {
    assert.throws(() => parseConfirmResult("confirmed"), /not a JSON object/);
    assert.throws(() => parseConfirmResult(null), /not a JSON object/);
  });

  it("treats a missing ok as failure, never as success", () => {
    // The sidecar answers 200 with `ok: false` for a refused batch, so absence
    // must not read as consent.
    const result = parseConfirmResult({
      result: { confirmed: 0, errors: ["Expenses:Nope is not open"] },
    });
    assert.equal(result.ok, false);
    assert.deepEqual(result.errors, ["Expenses:Nope is not open"]);
  });

  it("tolerates missing list fields without throwing inside a render", () => {
    const result = parseConfirmResult({ result: { confirmed: 1, ok: true } });
    assert.deepEqual(result.warnings, []);
    assert.deepEqual(result.errors, []);
    assert.deepEqual(result.files_written, []);
    assert.equal(result.learned, 0);
  });
});

describe("categorizationTargets", () => {
  it("offers only expense and income accounts", () => {
    assert.deepEqual(
      categorizationTargets([
        "Assets:Checking",
        "Equity:Opening-Balances",
        "Expenses:Food:Groceries",
        "Income:Salary",
        "Liabilities:Card",
      ]),
      ["Expenses:Food:Groceries", "Income:Salary"]
    );
  });

  it("excludes the Unknown accounts, which are the absence of a decision", () => {
    assert.deepEqual(
      categorizationTargets([
        "Expenses:Unknown",
        "Income:Unknown",
        "Expenses:Food",
      ]),
      ["Expenses:Food"]
    );
  });

  it("sorts, so the dropdown order does not depend on the ledger's order", () => {
    assert.deepEqual(
      categorizationTargets(["Expenses:Rent", "Expenses:Food", "Income:Gift"]),
      ["Expenses:Food", "Expenses:Rent", "Income:Gift"]
    );
  });
});
