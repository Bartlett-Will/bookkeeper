import type { NextRequest } from "next/server";
import { getBudgetReport } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * Allocated versus actually spent, per envelope, over a window.
 *
 * Same asymmetry as `/reports/spending` and it comes from the sidecar: the
 * query params are `from` and `to`, the response body says `from_date` and
 * `to_date`, because `from` is a Python keyword that can be aliased on the way
 * in and not on the way out.
 *
 * Both bounds are forwarded as-is, absent included. Omitting them selects the
 * ledger's own first and last transaction dates rather than a wall-clock
 * window — substituting today here would make this proxy disagree with the CLI
 * about what "the budget report" means.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;

  return respondWithSidecarResult(
    await getBudgetReport(
      { from: searchParams.get("from"), to: searchParams.get("to") },
      { signal: request.signal }
    )
  );
}
