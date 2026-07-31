import type { NextRequest } from "next/server";
import { getCategorizableAccounts } from "@/lib/sidecar/client";
import { respondWithSidecarResult } from "@/lib/sidecar/respond";

/**
 * The accounts a transaction may be recategorized *into* — open `Expenses:*`
 * and `Income:*`, minus the `Unknown` catch-alls.
 *
 * Deliberately not `/accounts`, which is the `Assets:` side and the wrong set
 * for a correction dropdown. The sidecar derives this from the same
 * `build_ledger_context` the categorizer uses, so the accounts offered here
 * are exactly the accounts the cascade can predict — if the two could drift,
 * the UI would let a user pick something the categorizer would never suggest.
 *
 * The review UI fetches this to pre-validate before confirming, because
 * `POST /review/confirm` rejects the whole batch when any account is not open.
 * Without the check, one bad account costs the user every other approval in
 * the batch.
 */
export async function GET(request: NextRequest) {
  return respondWithSidecarResult(
    await getCategorizableAccounts({ signal: request.signal })
  );
}
