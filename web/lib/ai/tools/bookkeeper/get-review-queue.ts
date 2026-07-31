import { tool } from "ai";
import { z } from "zod";
import type { BookkeeperClient, ReviewQueue } from "./client";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type GetReviewQueueResult = BookkeeperToolResult<"review", ReviewQueue>;

/** Enough to work through in one sitting; keeps one turn from rendering hundreds of cards. */
const DEFAULT_LIMIT = 25;
const MAX_LIMIT = 100;

/**
 * The transactions waiting on a human decision, with the cascade's guess.
 *
 * This is the *primary* surface of the app, not an edge case: auto-apply is
 * off on measured evidence (Phase 3), so everything lands in
 * `Expenses:Unknown` until someone confirms it.
 *
 * The model's whole job here is to summon the cards. Accepting or correcting
 * one is a button click straight to `POST /review/confirm` (§5.3 rule 2) —
 * there is deliberately no confirm tool, so approving 40 transactions costs
 * zero further model calls.
 */
export function getReviewQueue(client: BookkeeperClient) {
  return tool({
    description:
      "List the transactions that are waiting for the user to confirm a category, each with the system's suggested account. Use when the user asks what needs reviewing, what is uncategorized, what needs approving, or asks to see the review queue. Read-only: it shows the queue, it does not approve anything.",
    execute: async ({ limit }): Promise<GetReviewQueueResult> => {
      const result = await client.getReviewQueue({
        limit: Math.min(limit ?? DEFAULT_LIMIT, MAX_LIMIT),
      });
      if (!result.ok) {
        return failed("review", result.error);
      }
      return ok("review", result.data);
    },
    inputSchema: z.object({
      limit: z
        .number()
        .int()
        .positive()
        .max(MAX_LIMIT)
        .optional()
        .describe(
          `How many transactions to show. Omit this unless the user asked for a specific number; the default is ${DEFAULT_LIMIT}.`
        ),
    }),
    toModelOutput: renderedByTheUi("The review queue has been displayed."),
  });
}
