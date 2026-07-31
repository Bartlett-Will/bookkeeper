import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { preRoute, preRouteMessage } from "./pre-route";

// The asymmetry described in `pre-route.ts` is what these tests are shaped
// around. A missed command costs one ordinary model turn — the thing that
// would have happened anyway. A *false* match hijacks a question: the user
// asks "why did the sync fail yesterday?" and the app kicks off a sync instead
// of answering. So the refusal cases below outnumber the match cases, and they
// are the ones to keep green.

describe("preRoute — commands that should route", () => {
  const commands: [string, string][] = [
    ["sync", "sync_accounts"],
    ["Sync", "sync_accounts"],
    ["sync now", "sync_accounts"],
    ["resync", "sync_accounts"],
    ["sync my accounts", "sync_accounts"],
    ["sync my accounts.", "sync_accounts"],
    ["Sync my accounts", "sync_accounts"],
    ["refresh the transactions", "sync_accounts"],
    ["refresh accounts", "sync_accounts"],
    ["sync the bank now", "sync_accounts"],

    ["review", "get_review_queue"],
    ["review queue", "get_review_queue"],
    ["show me the review queue", "get_review_queue"],
    ["open review", "get_review_queue"],
    ["list my review queue", "get_review_queue"],

    ["envelopes", "get_envelope_status"],
    ["envelope", "get_envelope_status"],
    ["budget", "get_envelope_status"],
    ["budget status", "get_envelope_status"],
    ["show my envelopes", "get_envelope_status"],
    ["view the budget", "get_envelope_status"],
  ];

  for (const [text, expected] of commands) {
    it(`routes ${JSON.stringify(text)} to ${expected}`, () => {
      const route = preRoute(text);
      assert.equal(route?.toolName, expected, `expected ${text} to route`);
      // Pre-routing never parses arguments out of prose — a mis-parsed
      // argument is a wrong answer delivered confidently.
      assert.deepEqual(route?.input, {});
    });
  }
});

describe("preRoute — questions that must NOT be hijacked", () => {
  const questions = [
    // The motivating case from the module docstring.
    "why did the sync fail yesterday?",
    "why did the sync fail yesterday",
    "did the sync work?",
    "did my sync run",
    "is the sync broken",
    "what happened to the sync",
    "when was the last sync",
    "how do I sync",
    "should I sync",
    "can you sync",
    "what is in my review queue",
    "whats in the review queue",
    "why is my budget wrong",
    "what is my budget",
    "how are my envelopes doing",
    "is the budget stuck",
    "did the review queue update",
    "explain the review queue",
    "was the sync successful",
    "the sync failed",
    "sync errors",
  ];

  for (const text of questions) {
    it(`declines ${JSON.stringify(text)}`, () => {
      assert.equal(preRoute(text), null);
    });
  }
});

describe("preRoute — near-misses that must not match", () => {
  const nonCommands = [
    // Anchored to the whole message: a command with a tail is not a command.
    "sync my accounts and then show me the review queue",
    "review the groceries envelope for me please",
    "budget more for groceries",
    "sync accounts with my other bank account too",
    // Different verbs entirely.
    "delete my review queue",
    "reset the budget",
    // Not commands at all.
    "",
    "   ",
    "thanks",
    "ok",
    // A write must never be reachable by pattern-matching a sentence.
    "allocate 200 to groceries",
    "put 50 in the envelope",
    // Search and reports need arguments parsed from prose, so they are not
    // pre-routable at all.
    "find my whole foods transactions",
    "show me spending for last month",
  ];

  for (const text of nonCommands) {
    it(`declines ${JSON.stringify(text)}`, () => {
      assert.equal(preRoute(text), null);
    });
  }
});

describe("preRoute — normalisation", () => {
  it("ignores case, trailing punctuation and repeated spaces", () => {
    assert.equal(
      preRoute("  SYNC   MY   ACCOUNTS!  ")?.toolName,
      "sync_accounts"
    );
  });

  it("treats a question mark as decisive even on an otherwise exact command", () => {
    // "sync?" is someone asking whether to sync, not telling us to.
    assert.equal(preRoute("sync?"), null);
    assert.equal(preRoute("review queue?"), null);
  });

  it("collapses apostrophes so contractions hit the veto list", () => {
    // Both spellings of the apostrophe a Mac might produce.
    assert.equal(preRoute("didn't the sync run"), null);
    assert.equal(preRoute("didn’t the sync run"), null);
  });
});

describe("preRouteMessage", () => {
  const textMessage = (text: string) => ({
    parts: [{ text, type: "text" }],
    role: "user",
  });

  it("routes a lone user text part", () => {
    assert.equal(
      preRouteMessage(textMessage("sync"))?.toolName,
      "sync_accounts"
    );
  });

  it("declines anything that is not from the user", () => {
    assert.equal(
      preRouteMessage({ ...textMessage("sync"), role: "assistant" }),
      null
    );
    assert.equal(preRouteMessage(undefined), null);
    assert.equal(preRouteMessage(null), null);
  });

  it("declines a multi-part message", () => {
    // Extra parts carry context a regex over one string cannot see.
    assert.equal(
      preRouteMessage({
        parts: [{ text: "sync", type: "text" }, { type: "file" }],
        role: "user",
      }),
      null
    );
  });

  it("declines a non-text part", () => {
    assert.equal(
      preRouteMessage({ parts: [{ type: "file" }], role: "user" }),
      null
    );
  });

  it("declines a message with no parts", () => {
    assert.equal(preRouteMessage({ role: "user" }), null);
  });
});
