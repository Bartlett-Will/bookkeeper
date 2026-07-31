import type { NextRequest } from "next/server";
import { getReviewQueue } from "@/lib/sidecar/client";
import {
  respondWithBadRequest,
  respondWithSidecarResult,
} from "@/lib/sidecar/respond";

/**
 * Transactions awaiting human categorization. Read-only.
 *
 * With auto-apply off (Phase 3, measured) this is the *primary* surface of the
 * app rather than an edge case — nothing reaches the ledger without someone
 * confirming it here first.
 */
export async function GET(request: NextRequest) {
  const rawLimit = request.nextUrl.searchParams.get("limit");

  // Parsed rather than forwarded, because `?limit=abc` would otherwise reach
  // the sidecar as a 422 the UI has to interpret. A bad limit is our bug.
  let limit: number | null = null;
  if (rawLimit !== null) {
    limit = Number.parseInt(rawLimit, 10);
    if (!Number.isFinite(limit) || limit < 1) {
      return respondWithBadRequest(
        `limit must be a positive integer, got ${JSON.stringify(rawLimit)}`
      );
    }
  }

  return respondWithSidecarResult(
    await getReviewQueue({ limit }, { signal: request.signal })
  );
}
