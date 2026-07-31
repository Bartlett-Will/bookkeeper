import type { NextRequest } from "next/server";
import { getSyncStatus } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * Progress of a background sync job started by `POST /sync/start`.
 *
 * The UI polls this rather than holding a chat turn open (PLAN.md §5.3 rule
 * 3). Deliberately cheap: no ledger read, no categorization, just job state.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ jobId: string }> }
) {
  const { jobId } = await params;

  return respondWithSidecarResult(
    await getSyncStatus(jobId, { signal: request.signal })
  );
}
