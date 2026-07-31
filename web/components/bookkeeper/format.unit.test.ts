import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  accountLeaf,
  accountParent,
  confidenceBand,
  formatAmount,
  formatConfidence,
  formatMagnitude,
  formatTier,
  isNegative,
  pluralize,
  sumAmounts,
} from "./format";

describe("sumAmounts", () => {
  it("sums in cents, so a total agrees with its own rows", () => {
    // 0.1 + 0.2 in floats is 0.30000000000000004; a group header that
    // disagrees with the rows beneath it by a cent reads as a ledger bug.
    assert.equal(sumAmounts(["0.10", "0.20"]), "0.30");
  });

  it("keeps a long run of amounts exact", () => {
    const rows = new Array(47).fill("19.99");
    assert.equal(sumAmounts(rows), "939.53");
  });

  it("handles signed amounts and an empty list", () => {
    assert.equal(sumAmounts(["-19.96", "5.00"]), "-14.96");
    assert.equal(sumAmounts([]), "0.00");
  });

  it("skips unparseable amounts rather than producing NaN", () => {
    assert.equal(sumAmounts(["10.00", "oops"]), "10.00");
  });
});

describe("confidenceBand", () => {
  it("buckets coarsely, because a self-reported score is not a probability", () => {
    assert.equal(confidenceBand(0.95), "high");
    assert.equal(confidenceBand(0.9), "high");
    assert.equal(confidenceBand(0.7), "medium");
    assert.equal(confidenceBand(0.6), "medium");
    assert.equal(confidenceBand(0.42), "low");
    assert.equal(confidenceBand(null), "none");
  });
});

describe("formatConfidence", () => {
  it("renders a missing confidence as a dash, not as zero", () => {
    assert.equal(formatConfidence(null), "—");
    assert.equal(formatConfidence(0.824), "82%");
  });
});

describe("formatTier", () => {
  it("names the cascade tiers in words", () => {
    assert.equal(formatTier("mcc"), "merchant code");
    assert.equal(formatTier("memory"), "seen before");
    assert.equal(formatTier(null), "no tier answered");
  });

  it("passes an unrecognised tier through rather than hiding it", () => {
    assert.equal(formatTier("experimental"), "experimental");
  });
});

describe("formatAmount", () => {
  it("keeps the sign, which is what the bank statement shows", () => {
    assert.match(formatAmount("-19.96", "USD"), /19\.96/);
    assert.ok(formatAmount("-19.96", "USD").includes("-"));
  });

  it("drops the sign for figures whose direction is already named", () => {
    assert.ok(!formatMagnitude("-19.96", "USD").includes("-"));
  });

  it("degrades instead of throwing on an unusual commodity", () => {
    const rendered = formatAmount("12.00", "NOTACURRENCY");
    assert.ok(rendered.includes("12"));
  });

  it("passes an unparseable amount through verbatim", () => {
    assert.equal(formatAmount("n/a", "USD"), "n/a USD");
  });
});

describe("isNegative", () => {
  it("reads the ledger's sign convention", () => {
    assert.equal(isNegative("-0.01"), true);
    assert.equal(isNegative("0.00"), false);
    assert.equal(isNegative("4.00"), false);
  });
});

describe("account names", () => {
  it("splits a leaf from its parent", () => {
    assert.equal(accountLeaf("Expenses:Food:Groceries"), "Groceries");
    assert.equal(accountParent("Expenses:Food:Groceries"), "Expenses:Food");
    assert.equal(accountParent("Expenses"), "");
  });
});

describe("pluralize", () => {
  it("agrees with its count and groups thousands", () => {
    assert.equal(pluralize(1, "transaction"), "1 transaction");
    assert.equal(pluralize(332, "transaction"), "332 transactions");
    assert.equal(pluralize(1000, "row"), "1,000 rows");
  });
});
