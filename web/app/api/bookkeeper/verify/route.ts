import type { NextRequest } from "next/server";
import { getVerify } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * Ledger and envelope integrity checks.
 *
 * A ledger that fails its checks is a 200 with `ok: false`, not an error
 * response — the request succeeded and the findings are the payload. This
 * matters right now: auto-apply is off, so every transaction sits in
 * `Expenses:Unknown` and `verify` reports it as unmapped by design. Rendering
 * that as a transport failure would be wrong.
 */
export async function GET(request: NextRequest) {
  return respondWithSidecarResult(await getVerify({ signal: request.signal }));
}
