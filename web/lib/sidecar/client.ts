import "server-only";

import type {
  AllocateRequest,
  AllocateResponse,
  CallOptions,
  CategorizableAccountsResponse,
  CategorizeRequest,
  CategorizeResponse,
  ConfirmRequest,
  ConfirmResponse,
  EnvelopeReportResponse,
  HealthResponse,
  ReviewQueueResponse,
  SidecarFailure,
  SidecarPort,
  SidecarResult,
  SpendingReportResponse,
  SyncStartRequest,
  SyncStartResponse,
  SyncStatusResponse,
  TransactionSearchResponse,
  VerifyResponse,
} from "./contract";

/**
 * The typed client for the Python sidecar.
 *
 * `server-only` is load-bearing, not decoration. Importing this into a client
 * component would ship the sidecar's address to the browser and let the
 * browser talk to it directly, bypassing the route handlers — and PLAN.md §9
 * puts the SimpleFIN Access URL and every `.beancount` write on the far side
 * of that boundary. The import makes the mistake a build error instead of a
 * code review question. Types live in `contract.ts`, which is importable
 * anywhere.
 *
 * Every function here returns a `SidecarResult` and none of them throw. The
 * tool layer has to turn a failure into something an 8B model can respond to,
 * and the UI has to render it; neither is well served by an exception
 * escaping mid-stream.
 */

// Re-exported so a caller that imports the functions does not also have to
// reach into `contract.ts` for the types in their signatures.
export type {
  CallOptions,
  SidecarFailure,
  SidecarFailureKind,
  SidecarPort,
  SidecarResult,
  SidecarSuccess,
} from "./contract";
export { isSidecarSuccess } from "./contract";

export const SIDECAR_BASE_URL =
  process.env.SIDECAR_BASE_URL ?? "http://127.0.0.1:8000";

/**
 * Reads are on the chat hot path and the sidecar serves them from a warm
 * ledger cache (PLAN.md §5.1), so anything this slow means something is
 * wrong and waiting longer will not help.
 */
const READ_TIMEOUT_MS = 10_000;

/**
 * Writes do a ledger pass and a git commit. Still bounded — a hung sidecar
 * must not hang a chat turn — but a batch confirm of forty transactions has
 * real work to do.
 */
const WRITE_TIMEOUT_MS = 30_000;

/**
 * `POST /sync/start` and `GET /sync/status` are both meant to return
 * immediately; the work happens in a background job (PLAN.md §5.3 rule 3).
 * A short timeout here is a correctness check on that promise, not a
 * limitation: if `start` blocks for 5s it is doing the sync synchronously and
 * we want to find out.
 */
const SYNC_CONTROL_TIMEOUT_MS = 5000;

/**
 * `/categorize` runs the tiered categorizer, whose tail tier is a local LLM at
 * ~1–2s per transaction (PLAN.md §5.4). It is not a chat tool for exactly
 * that reason — it exists here for CLI parity (§9) and is allowed to be slow.
 */
const CATEGORIZE_TIMEOUT_MS = 300_000;

type QueryValue = string | number | boolean | null | undefined;

type RequestSpec = {
  path: string;
  method?: "GET" | "POST";
  query?: Record<string, QueryValue>;
  body?: unknown;
  timeoutMs: number;
};

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(path, SIDECAR_BASE_URL);
  for (const [key, value] of Object.entries(query ?? {})) {
    // Omitted rather than sent as "undefined"/"null": FastAPI's optional query
    // params default correctly on absence and reject the literal strings.
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

function describeHttpFailure(
  spec: RequestSpec,
  status: number,
  rawBody: string
): SidecarFailure {
  let detail: unknown;
  try {
    detail = JSON.parse(rawBody);
  } catch {
    detail = rawBody === "" ? undefined : rawBody;
  }

  // FastAPI puts the useful part in `detail`; surfacing it keeps the model
  // from having to guess what "HTTP 422" meant.
  const explanation =
    detail && typeof detail === "object" && "detail" in detail
      ? ` ${JSON.stringify((detail as { detail: unknown }).detail)}`
      : "";

  return {
    detail,
    kind: "http",
    message: `The bookkeeper service returned HTTP ${status} for ${
      spec.method ?? "GET"
    } ${spec.path}.${explanation}`,
    outcome: "failure",
    status,
  };
}

async function call<T>(
  spec: RequestSpec,
  options?: CallOptions
): Promise<SidecarResult<T>> {
  const url = buildUrl(spec.path, spec.query);

  // AbortController rather than AbortSignal.timeout so the timeout is
  // distinguishable from a caller-initiated cancel by construction, instead of
  // by inspecting a DOMException's name. Matches app/api/sidecar/health.
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, spec.timeoutMs);

  const cancel = () => controller.abort();
  options?.signal?.addEventListener("abort", cancel, { once: true });
  if (options?.signal?.aborted) {
    cancel();
  }

  try {
    const response = await fetch(url, {
      body: spec.body === undefined ? undefined : JSON.stringify(spec.body),
      // No caching anywhere in this client. The ledger is the source of truth
      // and a stale envelope balance is a wrong number on someone's money.
      cache: "no-store",
      headers:
        spec.body === undefined
          ? undefined
          : { "Content-Type": "application/json" },
      method: spec.method ?? "GET",
      signal: controller.signal,
    });

    if (!response.ok) {
      const rawBody = await response.text().catch(() => "");
      return describeHttpFailure(spec, response.status, rawBody);
    }

    try {
      return { data: (await response.json()) as T, outcome: "success" };
    } catch (error) {
      return {
        detail: error instanceof Error ? error.message : String(error),
        kind: "malformed",
        message: `The bookkeeper service answered ${spec.path} with a body that was not valid JSON.`,
        outcome: "failure",
      };
    }
  } catch (error) {
    if (timedOut) {
      return {
        kind: "timeout",
        message: `The bookkeeper service did not respond within ${spec.timeoutMs}ms.`,
        outcome: "failure",
      };
    }
    return {
      detail: error instanceof Error ? error.message : String(error),
      kind: "unreachable",
      message: `Could not reach the bookkeeper service at ${SIDECAR_BASE_URL}. It may not be running.`,
      outcome: "failure",
    };
  } finally {
    clearTimeout(timer);
    options?.signal?.removeEventListener("abort", cancel);
  }
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export function getHealth(
  options?: CallOptions
): Promise<SidecarResult<HealthResponse>> {
  return call<HealthResponse>(
    { path: "/health", timeoutMs: SYNC_CONTROL_TIMEOUT_MS },
    options
  );
}

/**
 * Ledger and envelope integrity checks. A failing ledger comes back as a
 *successful* call carrying `ok: false` — the request worked and the findings
 * are the payload. Phase 3 measured auto-apply off, so this currently reports
 * `Expenses:Unknown` as unmapped by design; that is not a transport failure
 * and must not be rendered as one.
 */
export function getVerify(
  options?: CallOptions
): Promise<SidecarResult<VerifyResponse>> {
  return call<VerifyResponse>(
    { path: "/verify", timeoutMs: READ_TIMEOUT_MS },
    options
  );
}

/**
 * The accounts a transaction may be recategorized *into*.
 *
 * Not `/accounts`, which returns the `Assets:` side — that is the wrong set
 * for a correction dropdown. The review UI fetches this to pre-validate
 * before confirming: `POST /review/confirm` rejects the whole batch if any
 * account is not open in the ledger, so without the check one bad account
 * costs the user every other approval in the batch.
 */
export function getCategorizableAccounts(
  options?: CallOptions
): Promise<SidecarResult<CategorizableAccountsResponse>> {
  return call<CategorizableAccountsResponse>(
    { path: "/accounts/categorizable", timeoutMs: READ_TIMEOUT_MS },
    options
  );
}

export function getEnvelopes(
  params: { asof?: string | null } = {},
  options?: CallOptions
): Promise<SidecarResult<EnvelopeReportResponse>> {
  return call<EnvelopeReportResponse>(
    {
      path: "/envelopes",
      query: { asof: params.asof },
      timeoutMs: READ_TIMEOUT_MS,
    },
    options
  );
}

export function getReviewQueue(
  params: { limit?: number | null } = {},
  options?: CallOptions
): Promise<SidecarResult<ReviewQueueResponse>> {
  return call<ReviewQueueResponse>(
    {
      path: "/review-queue",
      query: { limit: params.limit },
      timeoutMs: READ_TIMEOUT_MS,
    },
    options
  );
}

export function searchTransactions(
  params: { q: string; limit?: number | null },
  options?: CallOptions
): Promise<SidecarResult<TransactionSearchResponse>> {
  return call<TransactionSearchResponse>(
    {
      path: "/transactions/search",
      query: { limit: params.limit, q: params.q },
      timeoutMs: READ_TIMEOUT_MS,
    },
    options
  );
}

/**
 * Spend by envelope over a date range.
 *
 * The query params are `from` and `to`, but the response names them
 * `from_date` and `to_date` — `from` is a Python keyword, so the sidecar
 * aliases it on the way in and cannot on the way out. Not a bug on either
 * side; just an asymmetry worth knowing before you go looking for `from`.
 */
export function getSpendingReport(
  params: { from?: string | null; to?: string | null } = {},
  options?: CallOptions
): Promise<SidecarResult<SpendingReportResponse>> {
  return call<SpendingReportResponse>(
    {
      path: "/reports/spending",
      query: { from: params.from, to: params.to },
      timeoutMs: READ_TIMEOUT_MS,
    },
    options
  );
}

// ---------------------------------------------------------------------------
// Sync (background job)
// ---------------------------------------------------------------------------

export function startSync(
  body: SyncStartRequest,
  options?: CallOptions
): Promise<SidecarResult<SyncStartResponse>> {
  return call<SyncStartResponse>(
    {
      body,
      method: "POST",
      path: "/sync/start",
      timeoutMs: SYNC_CONTROL_TIMEOUT_MS,
    },
    options
  );
}

export function getSyncStatus(
  jobId: string,
  options?: CallOptions
): Promise<SidecarResult<SyncStatusResponse>> {
  return call<SyncStatusResponse>(
    {
      path: `/sync/status/${encodeURIComponent(jobId)}`,
      timeoutMs: SYNC_CONTROL_TIMEOUT_MS,
    },
    options
  );
}

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

/**
 * Accepts a whole batch of human decisions in one request.
 *
 * This is the click-bypass path of PLAN.md §5.3 rule 2 and it is deliberately
 * **not** exposed as a chat tool: approving forty transactions must be forty
 * deterministic HTTP calls and zero LLM calls. Keeping it batch-shaped also
 * keeps the sidecar's promise of one ledger pass and one revertible git commit
 * per batch — a per-transaction endpoint would produce forty commits and forty
 * ledger reloads.
 */
export function confirmReview(
  body: ConfirmRequest,
  options?: CallOptions
): Promise<SidecarResult<ConfirmResponse>> {
  return call<ConfirmResponse>(
    {
      body,
      method: "POST",
      path: "/review/confirm",
      timeoutMs: WRITE_TIMEOUT_MS,
    },
    options
  );
}

export function allocateToEnvelope(
  body: AllocateRequest,
  options?: CallOptions
): Promise<SidecarResult<AllocateResponse>> {
  return call<AllocateResponse>(
    {
      body,
      method: "POST",
      path: "/envelopes/allocate",
      timeoutMs: WRITE_TIMEOUT_MS,
    },
    options
  );
}

/**
 * Runs the tiered categorizer. `apply` defaults to false on the sidecar and
 * this client never supplies it implicitly — a call that rewrites the ledger
 * unless you opt out is PLAN.md §7's top risk.
 */
export function categorize(
  body: CategorizeRequest,
  options?: CallOptions
): Promise<SidecarResult<CategorizeResponse>> {
  return call<CategorizeResponse>(
    {
      body,
      method: "POST",
      path: "/categorize",
      timeoutMs: CATEGORIZE_TIMEOUT_MS,
    },
    options
  );
}

/**
 * The same functions as an object, and a compile-time check that they still
 * match `SidecarPort`. Callers that declare a narrow port against the type
 * find out here — at build time — if a signature drifts out from under them.
 */
export const sidecar = {
  allocateToEnvelope,
  categorize,
  confirmReview,
  getCategorizableAccounts,
  getEnvelopes,
  getHealth,
  getReviewQueue,
  getSpendingReport,
  getSyncStatus,
  getVerify,
  searchTransactions,
  startSync,
} satisfies SidecarPort;
