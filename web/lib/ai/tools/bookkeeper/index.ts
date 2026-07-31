import { allocateToEnvelope } from "./allocate-to-envelope";
import type { BookkeeperClient } from "./client";
import { getEnvelopeStatus } from "./get-envelope-status";
import { getReviewQueue } from "./get-review-queue";
import { getSpendingReport } from "./get-spending-report";
import { searchTransactions } from "./search-transactions";
import { syncAccounts } from "./sync-accounts";

// The six tools of PLAN.md §5.3, and no more.
//
// The count is a design constraint, not an accident of scope: §3.3 measures
// small models as worst at deciding *whether* and *which* to call, and every
// additional tool widens exactly that decision. If a seventh is ever wanted,
// the question to answer first is which of these six it replaces.
//
// Names are snake_case because that is what the model sees and what the tool
// descriptions and the system prompt refer to. Keep them in sync.

/**
 * The one operation missing from this list on purpose.
 *
 * `POST /review/confirm` is reachable only from a button click in
 * `ReviewCard`, straight to the API. §5.3 rule 2: approving 40 transactions
 * must be 40 deterministic HTTP calls and zero model calls. A
 * `confirm_categorization` tool would put an 8B model in the middle of the
 * hottest path in the app, which is the one thing the whole phase is designed
 * to avoid.
 */
export const CONFIRM_IS_NOT_A_TOOL = "POST /review/confirm";

export function bookkeeperTools(client: BookkeeperClient) {
  return {
    allocate_to_envelope: allocateToEnvelope(client),
    get_envelope_status: getEnvelopeStatus(client),
    get_review_queue: getReviewQueue(client),
    get_spending_report: getSpendingReport(client),
    search_transactions: searchTransactions(client),
    sync_accounts: syncAccounts(client),
  };
}

export type BookkeeperToolSet = ReturnType<typeof bookkeeperTools>;
export type BookkeeperToolName = keyof BookkeeperToolSet;

export const BOOKKEEPER_TOOL_NAMES = [
  "allocate_to_envelope",
  "get_envelope_status",
  "get_review_queue",
  "get_spending_report",
  "search_transactions",
  "sync_accounts",
] as const satisfies readonly BookkeeperToolName[];

export type { BookkeeperClient } from "./client";
