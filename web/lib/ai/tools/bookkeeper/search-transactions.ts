import { tool } from "ai";
import { z } from "zod";
import type { BookkeeperClient, TransactionSearchResult } from "./client";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type SearchTransactionsResult = BookkeeperToolResult<
  "transactions",
  TransactionSearchResult
>;

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 100;
const MAX_QUERY_LENGTH = 100;

/**
 * Free-text search over transactions, backed by beanquery in the sidecar.
 *
 * Kept strictly to *finding rows*, never totalling them. "How much did I
 * spend at X" is `get_spending_report`'s job; leaving summation out of this
 * tool's contract is what stops the model from being handed a list and asked
 * to add it up — the arithmetic §5.3 rule 1 exists to prevent.
 */
export function searchTransactions(client: BookkeeperClient) {
  return tool({
    description:
      "Find individual transactions whose description or payee matches a search term, such as a shop or merchant name. Use when the user asks whether they bought something, or asks to see their transactions at a particular place. Returns the matching transactions themselves — it does not add them up or produce a total.",
    execute: async ({ query, limit }): Promise<SearchTransactionsResult> => {
      const q = query.trim();
      if (q.length === 0) {
        return failed("transactions", "no search term was given.");
      }
      if (q.length > MAX_QUERY_LENGTH) {
        return failed(
          "transactions",
          `the search term is too long (${q.length} characters; the limit is ${MAX_QUERY_LENGTH}).`
        );
      }

      const result = await client.searchTransactions({
        limit: Math.min(limit ?? DEFAULT_LIMIT, MAX_LIMIT),
        q,
      });
      if (!result.ok) {
        return failed("transactions", result.error);
      }
      return ok("transactions", result.data);
    },
    inputSchema: z.object({
      limit: z
        .number()
        .int()
        .positive()
        .max(MAX_LIMIT)
        .optional()
        .describe(
          `How many transactions to return. Omit unless the user asked for a specific number; the default is ${DEFAULT_LIMIT}.`
        ),
      query: z
        .string()
        .describe(
          "The words to search for, usually a merchant or shop name — for example 'Whole Foods' or 'PG&E'. Use the user's own words; do not invent a merchant."
        ),
    }),
    toModelOutput: renderedByTheUi(
      "The matching transactions have been displayed."
    ),
  });
}
