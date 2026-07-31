import type { NextRequest } from "next/server";
import { getSpendingReport } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * Spend by envelope over a date range.
 *
 * Note the asymmetry, which comes from the sidecar and is not a bug here: the
 * query params are `from` and `to`, but the response body names them
 * `from_date` and `to_date`. `from` is a Python keyword, so the sidecar
 * aliases it on the way in and cannot on the way out.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;

  return respondWithSidecarResult(
    await getSpendingReport(
      { from: searchParams.get("from"), to: searchParams.get("to") },
      { signal: request.signal }
    )
  );
}
