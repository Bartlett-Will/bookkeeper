import { NextResponse } from "next/server";

// Proxies the Python sidecar's health check. Next.js never talks to the
// ledger directly (PLAN.md §9: "the sidecar is the sole ledger writer") —
// every ledger operation goes through the sidecar's HTTP API, and this route
// exists to prove that boundary is reachable end to end (Phase 0 exit
// criteria). It's a thin proxy, not a cache: no ledger state is read or
// written here.
const SIDECAR_BASE_URL =
  process.env.SIDECAR_BASE_URL ?? "http://127.0.0.1:8000";
const SIDECAR_TIMEOUT_MS = 3000;

export async function GET() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SIDECAR_TIMEOUT_MS);

  try {
    const response = await fetch(`${SIDECAR_BASE_URL}/health`, {
      cache: "no-store",
      signal: controller.signal,
    });

    if (!response.ok) {
      return NextResponse.json(
        {
          error: `Sidecar responded with HTTP ${response.status}`,
          reachable: false,
        },
        { status: 502 }
      );
    }

    const body = await response.json().catch(() => null);

    return NextResponse.json({ reachable: true, sidecar: body });
  } catch (error) {
    const isAbort = error instanceof Error && error.name === "AbortError";
    return NextResponse.json(
      {
        error: isAbort
          ? `Sidecar did not respond within ${SIDECAR_TIMEOUT_MS}ms`
          : `Could not reach sidecar at ${SIDECAR_BASE_URL}: ${
              error instanceof Error ? error.message : String(error)
            }`,
        reachable: false,
      },
      { status: 503 }
    );
  } finally {
    clearTimeout(timeout);
  }
}
