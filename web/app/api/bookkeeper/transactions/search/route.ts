import type { NextRequest } from "next/server";
import { searchTransactions } from "@/lib/sidecar/client";
import {
  respondWithBadRequest,
  respondWithSidecarResult,
} from "@/lib/sidecar/respond";

/** Free-text transaction search, backed by beanquery in the sidecar. */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const q = searchParams.get("q");

  if (q === null || q.trim() === "") {
    return respondWithBadRequest("`q` is required and must not be empty");
  }

  const rawLimit = searchParams.get("limit");
  let limit: number | null = null;
  if (rawLimit !== null) {
    limit = Number.parseInt(rawLimit, 10);
    if (!Number.isFinite(limit) || limit < 1) {
      return respondWithBadRequest(
        `limit must be a positive integer, got ${JSON.stringify(rawLimit)}`
      );
    }
  }

  return respondWithSidecarResult(
    await searchTransactions({ limit, q }, { signal: request.signal })
  );
}
