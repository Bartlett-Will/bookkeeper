import type { NextRequest } from "next/server";
import { startSync } from "@/lib/sidecar/client";
import type { SyncStartRequest } from "@/lib/sidecar/contract";
import {
  readJsonBody,
  respondWithBadRequest,
  respondWithSidecarResult,
} from "@/lib/sidecar/respond";

/**
 * Kicks off a SimpleFIN sync and returns a job handle immediately.
 *
 * Background, not synchronous, per PLAN.md §5.3 rule 3: categorizing 43
 * transactions at ~1–2s each would stall a chat turn for a minute. The caller
 * polls `/api/bookkeeper/sync/status/{jobId}`.
 *
 * The SimpleFIN Access URL never appears here. It lives only in the sidecar's
 * `0600` secrets file (§9), which is why this route carries no credential of
 * any kind — it names a job, nothing more.
 *
 * This is a real financial write: it hits the live SimpleFIN bridge, spends
 * against the ~24 requests/day budget (§3.1), and commits to `ledger/`. It is
 * not a smoke-test endpoint.
 */

function validate(body: unknown): SyncStartRequest | string {
  if (body === undefined || body === null) {
    return { demo: false };
  }
  if (typeof body !== "object") {
    return "expected a JSON object";
  }
  const { demo, since } = body as Record<string, unknown>;

  if (since !== undefined && since !== null && typeof since !== "string") {
    return "expected `since` to be an ISO date string when present";
  }
  if (demo !== undefined && typeof demo !== "boolean") {
    return "expected `demo` to be a boolean when present";
  }

  // `demo` is stated rather than omitted because the generated type marks it
  // required; false matches the sidecar's own default.
  return {
    demo: demo ?? false,
    ...(since === undefined ? {} : { since }),
  };
}

export async function POST(request: NextRequest) {
  // An empty body is a valid "sync everything since last time", so a missing
  // or unparseable body is treated as empty rather than rejected.
  const parsed = await readJsonBody(request);
  const validated = validate(parsed.ok ? parsed.value : undefined);
  if (typeof validated === "string") {
    return respondWithBadRequest(validated);
  }

  return respondWithSidecarResult(
    await startSync(validated, { signal: request.signal })
  );
}
