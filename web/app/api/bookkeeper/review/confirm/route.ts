import type { NextRequest } from "next/server";
import { confirmReview } from "@/lib/sidecar/client";
import type { Confirmation, ConfirmRequest } from "@/lib/sidecar/contract";
import {
  readJsonBody,
  respondWithBadRequest,
  respondWithSidecarResult,
} from "@/lib/sidecar/respond";

/**
 * Applies a batch of human categorization decisions to the ledger.
 *
 * **This route is the click-bypass path of PLAN.md §5.3 rule 2 and is
 * deliberately not reachable as a chat tool.** The browser calls it directly
 * from `ReviewCard`'s buttons. Approving forty transactions must be forty
 * deterministic HTTP calls and *zero* LLM calls; routing confirmations back
 * through the model would reintroduce the multi-turn degradation of §3.3 on
 * the hottest path in the app. Nothing in this file may ever call a model.
 *
 * It is batch-first for the same reason `confirm_categorizations` is: one
 * ledger pass and one git commit for the whole batch, so a forty-card approval
 * produces one revertible commit rather than forty. A caller that loops
 * one-at-a-time still works, but gives that up.
 */

function isConfirmation(value: unknown): value is Confirmation {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.asset_account === "string" &&
    typeof entry.simplefin_id === "string" &&
    typeof entry.account === "string"
  );
}

/**
 * Checked here rather than left to the sidecar because the failure this
 * catches is silent: a batch whose entries are missing `simplefin_id` matches
 * nothing in the ledger, and a "confirmed 0 of 40" response reads like an
 * empty queue rather than like a bug. Better to reject the request.
 */
function validate(body: unknown): ConfirmRequest | string {
  if (typeof body !== "object" || body === null) {
    return "expected a JSON object";
  }
  const { confirmations } = body as Record<string, unknown>;

  if (!Array.isArray(confirmations)) {
    return "expected `confirmations` to be an array";
  }

  const badIndex = confirmations.findIndex((entry) => !isConfirmation(entry));
  if (badIndex !== -1) {
    return `confirmations[${badIndex}] must have string \`asset_account\`, \`simplefin_id\`, and \`account\``;
  }

  return { confirmations };
}

export async function POST(request: NextRequest) {
  const parsed = await readJsonBody(request);
  if (!parsed.ok) {
    return respondWithBadRequest("request body was not valid JSON");
  }

  const validated = validate(parsed.value);
  if (typeof validated === "string") {
    return respondWithBadRequest(validated);
  }

  return respondWithSidecarResult(
    await confirmReview(validated, { signal: request.signal })
  );
}
