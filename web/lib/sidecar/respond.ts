import "server-only";

import { NextResponse } from "next/server";
import type { SidecarFailureKind, SidecarResult } from "./contract";

/**
 * Turns a `SidecarResult` into the HTTP response the `/api/bookkeeper/*`
 * proxies return.
 *
 * The routes are thin proxies (PLAN.md §9), so a successful call passes the
 * sidecar's body through unchanged — Next.js adds no envelope, no derived
 * fields, and no ledger reads of its own.
 *
 * Failures keep the health route's discipline: an unreachable sidecar and an
 * HTTP error from a running one are different conditions and stay
 * distinguishable, both by status code and by an explicit `kind`. The status
 * codes are finer-grained than the health route's — 504 for a timeout rather
 * than folding it in with 503 — because a caller polling sync status needs to
 * tell "not running" from "slow", and it is the difference between retrying
 * and giving up.
 */
export type SidecarErrorBody = {
  error: string;
  kind: SidecarFailureKind;
  /** True when a process answered, even if it answered badly. */
  reachable: boolean;
  /** The sidecar's status code, when it produced one. */
  status?: number;
  detail?: unknown;
};

const STATUS_BY_KIND: Record<SidecarFailureKind, number> = {
  http: 502,
  malformed: 502,
  timeout: 504,
  unreachable: 503,
};

const REACHABLE_BY_KIND: Record<SidecarFailureKind, boolean> = {
  http: true,
  malformed: true,
  timeout: false,
  unreachable: false,
};

export function respondWithSidecarResult<T>(
  result: SidecarResult<T>
): NextResponse {
  if (result.outcome === "success") {
    return NextResponse.json(result.data);
  }

  const body: SidecarErrorBody = {
    detail: result.detail,
    error: result.message,
    kind: result.kind,
    reachable: REACHABLE_BY_KIND[result.kind],
    status: result.status,
  };

  return NextResponse.json(body, { status: STATUS_BY_KIND[result.kind] });
}

/**
 * A malformed request from our own browser code, rejected before it reaches
 * the sidecar. Shares the error envelope above so callers have one shape to
 * handle: `kind: "http"` with a 400 means *we* sent something wrong.
 */
export function respondWithBadRequest(message: string): NextResponse {
  const body: SidecarErrorBody = {
    error: message,
    kind: "http",
    reachable: true,
    status: 400,
  };
  return NextResponse.json(body, { status: 400 });
}

/** Parses a JSON request body without throwing on malformed input. */
export async function readJsonBody(
  request: Request
): Promise<{ ok: true; value: unknown } | { ok: false }> {
  try {
    return { ok: true, value: await request.json() };
  } catch {
    return { ok: false };
  }
}
