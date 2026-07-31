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
 * not merely off-limits.
 */
export function renderedByTheUi<Kind extends string, Data>(
  successText: string
): (options: { output: BookkeeperToolResult<Kind, Data> }) => ToolResultOutput {
  return ({ output }) => {
    if (output.status === "error") {
      return {
        type: "text",
        value: `The ledger service could not answer: ${output.message} Tell the user this plainly and do not guess at the data.`,
      };
    }
    return {
      type: "text",
      value: `${successText} The user can see it on screen. You were NOT shown the figures and do not know them, so any amount, balance, date, or account name you write would be invented. State none. Reply with at most one short sentence containing no numbers, or say nothing at all.`,
    };
  };
}
