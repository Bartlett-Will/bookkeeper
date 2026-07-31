import { tool } from "ai";
import { z } from "zod";
import type { BookkeeperClient, EnvelopeReport } from "./client";
import { isIsoDate } from "./dates";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type GetEnvelopeStatusResult = BookkeeperToolResult<
  "envelopes",
  EnvelopeReport
>;

/**
 * Current envelope balances — allocated, spent, remaining, overspend.
 *
 * Returns *every* envelope rather than taking an envelope-name filter. Two
 * reasons: an envelope name the model invented would be a silent empty
 * result, and the interesting answer to "how am I doing on groceries" is
 * usually the whole board anyway. Filtering to one card is the UI's job, and
 * the UI knows the real names.
 *
 * Read-only. Kept strictly disjoint from `get_spending_report`: this is state
 * now, that one is history over a range.
 */
export function getEnvelopeStatus(client: BookkeeperClient) {
  return tool({
    description:
      "Show the current state of every budget envelope: how much was allocated to it, how much has been spent from it, and how much is left. Use for questions about how the user is doing on a category, how much is left to spend, or to show their envelopes. Read-only, and about right now — it does not cover past periods and it does not move any money.",
    execute: async ({ asof }): Promise<GetEnvelopeStatusResult> => {
      if (asof !== undefined && !isIsoDate(asof)) {
        return failed(
          "envelopes",
          `"${asof}" is not a real calendar date in YYYY-MM-DD form.`
        );
      }
      const result = await client.getEnvelopes({ asof });
      if (!result.ok) {
        return failed("envelopes", result.error);
      }
      return ok("envelopes", result.data);
    },
    inputSchema: z.object({
      asof: z
        .string()
        .optional()
        .describe(
          "Only if the user asked for balances as of a specific past date, in YYYY-MM-DD form. Omit this for the current state, which is what is almost always wanted."
        ),
    }),
    toModelOutput: renderedByTheUi(
      "The envelope balances have been displayed."
    ),
  });
}
