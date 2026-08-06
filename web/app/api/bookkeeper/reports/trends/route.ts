import type { NextRequest } from "next/server";
import { getTrendsReport } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * Direction per envelope, plus the transactions judged unusual for theirs.
 *
 * Passed through whole, including `assessments` — the per-envelope record of
 * what was judged and what was declined for want of history. It is tempting to
 * treat that as noise next to `outliers`, and it is the opposite: an empty
 * `outliers` list means "nothing was found" only for the envelopes that were
 * actually examined, and `assessments` is the only thing that says which those
 * were. A client that dropped it could render "nothing unusual" over a report
 * that looked at nothing.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;

  return respondWithSidecarResult(
    await getTrendsReport(
      { from: searchParams.get("from"), to: searchParams.get("to") },
      { signal: request.signal }
    )
  );
}
