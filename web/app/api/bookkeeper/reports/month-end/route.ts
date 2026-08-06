import type { NextRequest } from "next/server";
import { getMonthEndReport } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * The composite month-end report for one `YYYY-MM`.
 *
 * `month` is passed straight through, including when it is absent: omitting it
 * selects the sidecar's default, which is the month of the ledger's last
 * transaction rather than the wall-clock month. Substituting today's month
 * here would look like a helpful default and would quietly disagree with both
 * the CLI and the chat tool.
 *
 * No validation of the value either — an unparseable month is a 422 from the
 * sidecar, which `respondWithSidecarResult` passes on with its detail intact.
 * The chat tool validates before calling because a round trip costs a model a
 * turn; this proxy has no such reason to duplicate the rule.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;

  return respondWithSidecarResult(
    await getMonthEndReport(
      { month: searchParams.get("month") },
      { signal: request.signal }
    )
  );
}
