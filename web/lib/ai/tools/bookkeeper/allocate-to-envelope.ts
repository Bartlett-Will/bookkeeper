import { tool } from "ai";
import { z } from "zod";
import type { AllocationConfirmation, BookkeeperClient } from "./client";
import { isIsoDate, today } from "./dates";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type AllocateToEnvelopeResult = BookkeeperToolResult<
  "allocation",
  AllocationConfirmation
>;

/**
 * Single-currency by construction. Multi-currency is an explicit non-goal
 * (PLAN.md §8), so this is a constant rather than a parameter — one fewer
 * field for the model to fill in wrongly on the only tool that writes.
 */
const CURRENCY = "USD";

/** Well past any plausible envelope allocation; a fat-fingered order of magnitude stops here. */
const MAX_AMOUNT = 1_000_000;

const CENTS = 100;
/** Floating-point slack when checking that an amount is a whole number of cents. */
const CENT_EPSILON = 1e-6;

/**
 * Format a validated amount as an exact decimal string.
 *
 * The ledger stores decimals; JSON gave us a float. Rounding through integer
 * cents first means the string we hand the sidecar is the number the user
 * said, not `50.000000000000007`.
 */
function toDecimalString(amount: number): string {
  return (Math.round(amount * CENTS) / CENTS).toFixed(2);
}

/**
 * The one write tool (PLAN.md §5.3). Everything else in this directory reads.
 *
 * Validation is deliberately heavier than the other five, and one check is
 * worth calling out: the envelope name is resolved against the envelopes that
 * actually exist before anything is written, case-insensitively, and the
 * ledger's own spelling is what gets sent. A model that answers "groceries"
 * when the envelope is "Groceries" is a near-certainty; a model that invents
 * "Food" outright is not far behind, and that one must fail loudly with the
 * real names rather than create a new envelope by side effect.
 *
 * That read-then-write is two sidecar calls inside one tool, which is not the
 * chaining §3.3 warns about — the compounding-error argument is about *model*
 * steps, and the model gets exactly one here.
 */
export function allocateToEnvelope(client: BookkeeperClient) {
  return tool({
    description:
      "Move a specific amount of money into one named budget envelope. This is the only tool that changes anything — use it only when the user explicitly asks to allocate, budget, or put a stated amount into an envelope they name. Never use it to answer a question, and never guess the amount or the envelope.",
    execute: async ({
      amount,
      date,
      envelope,
    }): Promise<AllocateToEnvelopeResult> => {
      if (!Number.isFinite(amount) || amount <= 0) {
        return failed("allocation", "the amount must be greater than zero.");
      }
      if (amount > MAX_AMOUNT) {
        return failed(
          "allocation",
          `${amount} is larger than this tool will allocate in one go (the limit is ${MAX_AMOUNT}).`
        );
      }
      if (
        Math.abs(amount * CENTS - Math.round(amount * CENTS)) > CENT_EPSILON
      ) {
        return failed(
          "allocation",
          `${amount} is not a whole number of cents.`
        );
      }
      const when = date ?? today();
      if (!isIsoDate(when)) {
        return failed(
          "allocation",
          `"${when}" is not a real calendar date in YYYY-MM-DD form.`
        );
      }

      const known = await client.getEnvelopes({});
      if (!known.ok) {
        return failed("allocation", `${known.error} Nothing was allocated.`);
      }
      const wanted = envelope.trim().toLowerCase();
      const match = known.data.envelopes.find(
        (e) => e.name.toLowerCase() === wanted
      );
      if (!match) {
        const names = known.data.envelopes.map((e) => e.name);
        return failed(
          "allocation",
          names.length > 0
            ? `there is no envelope called "${envelope}". The envelopes are: ${names.join(", ")}. Nothing was allocated.`
            : `there is no envelope called "${envelope}", and no envelopes are defined yet. Nothing was allocated.`
        );
      }

      const result = await client.allocateToEnvelope({
        amount: toDecimalString(amount),
        currency: CURRENCY,
        date: when,
        envelope: match.name,
      });
      if (!result.ok) {
        return failed("allocation", result.error);
      }
      // A refused allocation is a *successful* call carrying `ok: false` — the
      // sidecar reserves 5xx for a broken endpoint and reports its own findings
      // in the payload, the same way `/verify` does. Checking only the
      // transport here would report "allocated" for a write that never
      // happened, on the one tool in the app that writes.
      if (!result.data.ok) {
        const [reason] = result.data.errors;
        return failed(
          "allocation",
          `${reason ?? "the allocation was refused."} Nothing was allocated.`
        );
      }
      return ok("allocation", result.data);
    },
    inputSchema: z.object({
      amount: z
        .number()
        .positive()
        .describe(
          `The amount to put into the envelope, in ${CURRENCY}, as a plain number such as 250 or 37.50. It must be the amount the user actually stated.`
        ),
      date: z
        .string()
        .optional()
        .describe(
          "Only if the user asked for the allocation to be dated to a particular day, in YYYY-MM-DD form. Omit for today."
        ),
      envelope: z
        .string()
        .describe(
          "The name of the envelope, exactly as the user said it — for example 'Groceries'. Do not invent a name."
        ),
    }),
    toModelOutput: renderedByTheUi("The allocation has been recorded."),
  });
}
