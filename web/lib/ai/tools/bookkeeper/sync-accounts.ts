import { tool } from "ai";
import { z } from "zod";
import type { BookkeeperClient, SyncStarted } from "./client";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type SyncAccountsResult = BookkeeperToolResult<"sync", SyncStarted>;

/**
 * Kick off a background sync. Returns a job id; it does not wait.
 *
 * PLAN.md §5.3 rule 3: categorizing ~43 transactions at 1–2s each would stall
 * the chat turn for a minute. The job runs in the sidecar and the UI polls
 * `/api/bookkeeper/sync/status/{job_id}` — the progress bar is not the
 * model's problem.
 *
 * Deliberately **zero parameters**. `POST /sync/start` accepts an optional
 * `since`, but exposing it would ask an 8B model to do date arithmetic on the
 * one tool that spends a request against SimpleFIN's ~24/day budget (§3.1).
 * Backfilling from a chosen date is a Phase 6 CLI job, not a chat turn.
 */
export function syncAccounts(client: BookkeeperClient) {
  return tool({
    description:
      "Download new transactions from the user's bank and start categorizing them. Use this only when the user asks to sync, refresh, import, or pull in new bank activity. It starts a background job and returns immediately — it does not return any transactions, balances, or totals.",
    execute: async (): Promise<SyncAccountsResult> => {
      const result = await client.startSync({});
      if (!result.ok) {
        return failed("sync", result.error);
      }
      return ok("sync", result.data);
    },
    inputSchema: z.object({}),
    toModelOutput: renderedByTheUi("A sync has started in the background."),
  });
}
