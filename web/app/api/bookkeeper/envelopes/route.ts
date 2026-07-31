import type { NextRequest } from "next/server";
import { getEnvelopes } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * Envelope balances as of `asof` (ISO date; the sidecar defaults to today).
 *
 * `asof` is forwarded as-is rather than validated here — the sidecar owns date
 * parsing and returns a 422 with a usable message, and duplicating the rule in
 * TypeScript is a second place for it to drift.
 */
export async function GET(request: NextRequest) {
  const asof = request.nextUrl.searchParams.get("asof");

  return respondWithSidecarResult(
    await getEnvelopes({ asof }, { signal: request.signal })
  );
}
