import type { Tool } from "ai";

// `ToolResultOutput` is declared in `@ai-sdk/provider-utils`, which is a
// transitive dependency rather than a direct one — under pnpm's strict layout
// it is not importable by name from here. Deriving it from `Tool`, which `ai`
// re-exports, gets the same type without adding a dependency on a package we
// do not otherwise use.
type ToolResultOutput = Awaited<ReturnType<NonNullable<Tool["toModelOutput"]>>>;

// The single shape every bookkeeper tool returns, and the rule that keeps
// numbers out of the model's mouth.
//
// PLAN.md §5.3 rule 1: "Tools return structured data that React renders...
// The model never recites numbers into text, so it cannot transpose a digit."
// A tool result normally travels to two places at once — into the UI stream
// for React, *and* back into the model's context as JSON for the next step.
// The second half is what would let an 8B model read `"balance": "412.37"`
// and write "$421.37". `toModelOutput` (below) splits the two: React gets the
// full payload, the model gets a fixed acknowledgement with no figures in it.
// That turns a transposed digit from unlikely into unrepresentable, which is
// the actual claim §5.3 makes.

/** `kind` is what the UI switches on to pick a renderer. */
export type BookkeeperToolResult<Kind extends string, Data> =
  | { status: "ok"; kind: Kind; data: Data }
  | { status: "error"; kind: Kind; message: string };

export function ok<Kind extends string, Data>(
  kind: Kind,
  data: Data
): BookkeeperToolResult<Kind, Data> {
  return { data, kind, status: "ok" };
}

export function failed<Kind extends string, Data>(
  kind: Kind,
  message: string
): BookkeeperToolResult<Kind, Data> {
  return { kind, message, status: "error" };
}

/**
 * Build the `toModelOutput` for a tool, given the sentence the model should
 * see on success.
 *
 * The success text deliberately contains no data at all — not a count, not a
 * total. A count looks harmless until the model decides to elaborate on it.
 * The failure text *is* passed through, because the model does need to tell
 * the user that something broke, and that string is ours, not the user's.
 *
 * **The wording here is load-bearing, and an earlier version made things
 * worse.** It said "Do not repeat, summarise, or restate any of its numbers",
 * which reads as though the model is holding the figures and is merely being
 * asked to keep quiet about them. Measured against `qwen3:8b` on 2026-07-31,
 * that produced a *fabricated* figure on four turns out of four — "You have
 * $25.00 left in your Groceries envelope" against a real balance of 0.00, and
 * a different invented number each time. Withholding the data did not stop the
 * model answering; it just removed the only thing keeping the answer true.
 *
 * So the instruction now states the situation rather than issuing a
 * prohibition: the model is told it *was not shown* the figures and does not
 * know them. §5.3 rule 1's mechanism is sound — a withheld number cannot be
 * transposed — but withholding alone converts a transposition into an
 * invention, which is worse. The model has to be told the numbers are absent,
 * not merely off-limits. Re-measured after the change: six turns, no figure.
 *
 * **Known residue, and the thing to watch in Phase 5.** Suppressing *numbers*
 * did not suppress *claims*. The model now says things like "You're currently
 * within your groceries budget" — no figure, but still an assertion about data
 * it cannot see, and on the tree this was measured against every envelope was
 * at zero, so it was not even true. Far less dangerous than a wrong dollar
 * amount and left as-is for Phase 4.
 *
 * It gets more dangerous as reports get richer. "Your spending is trending
 * down", "nothing looks unusual" and "you're on track" are all one short
 * sentence, all unfalsifiable from the model's position, and all things a
 * user would reasonably act on. If Phase 5 adds trend or outlier detection,
 * the honest options are to give the model a *small* set of pre-computed
 * qualitative facts it may repeat, or to drop the prose reply entirely and let
 * the card speak. Do not solve it by adding more prohibitions to this string —
 * the fabrication above is what that approach produced.
 *
 * **`sayableFacts` is the first of those two options, taken (Phase 5).**
 *
 * The diagnosis above generalises past numbers: the model asserts things
 * because it has been given a subject and nothing true to say about it, and
 * silence is not an answer an instruction-tuned model reaches for. Withholding
 * produced invented figures; withholding the *judgement* produces invented
 * judgements. So the fix is the same shape as the one that worked — supply, not
 * prohibition. A tool may pass a function deriving a few short sentences from
 * its own payload, and they are handed over as the facts the model *has*.
 *
 * The constraint that makes them safe is where they come from: each sentence
 * must be readable off a boolean, an enum, or a count the **sidecar** already
 * computed in `Decimal`. Nothing here may compare, sum, or threshold an amount
 * to produce one — that would move a financial judgement into the tool layer
 * and reintroduce the arithmetic this whole file exists to prevent. If a fact
 * needs a threshold, the threshold belongs in the sidecar and the fact arrives
 * as a field.
 *
 * This narrows the residue; it does not close it. A model given three true
 * sentences can still add a fourth. What it does is remove the *reason* it was
 * reaching — and that is the mechanism that measured 6/6 the first time.
 */
export function renderedByTheUi<Kind extends string, Data>(
  successText: string,
  sayableFacts?: (data: Data) => readonly string[]
): (options: { output: BookkeeperToolResult<Kind, Data> }) => ToolResultOutput {
  return ({ output }) => {
    if (output.status === "error") {
      return {
        type: "text",
        value: `The ledger service could not answer: ${output.message} Tell the user this plainly and do not guess at the data.`,
      };
    }
    const facts = sayableFacts?.(output.data) ?? [];
    const granted =
      facts.length === 0
        ? ""
        : ` These are the only facts about it you have, and each one is true: ${facts.join(" ")} Anything more specific than these you do not know.`;
    return {
      type: "text",
      value: `${successText} The user can see it on screen. You were NOT shown the figures and do not know them, so any amount, balance, date, or account name you write would be invented. State none.${granted} Reply with at most one short sentence containing no numbers, or say nothing at all.`,
    };
  };
}
