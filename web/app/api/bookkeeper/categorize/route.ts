import type { NextRequest } from "next/server";
import { categorize } from "@/lib/sidecar/client";
import type { CategorizeRequest } from "@/lib/sidecar/contract";
import {
  readJsonBody,
  respondWithBadRequest,
  respondWithSidecarResult,
} from "@/lib/sidecar/respond";

/**
 * Runs the tiered categorizer. Exposed for CLI/chat parity (PLAN.md §9), not
 * as a chat tool — the LLM tier runs at ~1–2s per transaction and would stall
 * a turn.
 *
 * `apply` defaults to false and must be asked for explicitly. A request that
 * rewrites the ledger unless you opt out is PLAN.md §7's top risk, and
 * auto-apply being off is a measured Phase 3 decision rather than an
 * oversight.
 */

function validate(body: unknown): CategorizeRequest | string {
  if (body === undefined || body === null) {
    return { apply: false, use_llm: true };
  }
  if (typeof body !== "object") {
    return "expected a JSON object";
  }
  const { apply, limit, use_llm } = body as Record<string, unknown>;

  if (apply !== undefined && typeof apply !== "boolean") {
    return "expected `apply` to be a boolean when present";
  }
  if (use_llm !== undefined && typeof use_llm !== "boolean") {
    return "expected `use_llm` to be a boolean when present";
  }
  if (
    limit !== undefined &&
    limit !== null &&
    (typeof limit !== "number" || !Number.isInteger(limit) || limit < 1)
  ) {
    return "expected `limit` to be a positive integer when present";
  }

  // Both flags are stated rather than omitted because the generated type marks
  // them required. The values match the sidecar's own defaults — `apply` in
  // particular must never acquire a different one here.
  return {
    apply: apply ?? false,
    use_llm: use_llm ?? true,
    ...(limit === undefined ? {} : { limit }),
  };
}

export async function POST(request: NextRequest) {
  const parsed = await readJsonBody(request);
  const validated = validate(parsed.ok ? parsed.value : undefined);
  if (typeof validated === "string") {
    return respondWithBadRequest(validated);
  }

  return respondWithSidecarResult(
    await categorize(validated, { signal: request.signal })
  );
}
