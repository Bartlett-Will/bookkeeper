/**
 * The typed surface of the Python sidecar, plus the result type every call
 * through `client.ts` returns.
 *
 * Every type below is an alias into `./types`, which is generated from the
 * sidecar's OpenAPI schema by `pnpm run sidecar:types`. Nothing here restates
 * a shape by hand — PLAN.md §5.1 buys "typed contract, checked end to end"
 * with the sidecar's complexity, and a hand-maintained second copy of a
 * financial API would spend it again on drift. If a field looks wrong, fix the
 * pydantic model and regenerate; do not patch it here.
 *
 * This module is deliberately free of `server-only`: client components need
 * these types to render envelope and review cards, and type-only imports are
 * erased at compile time. The *functions* that talk to the sidecar live in
 * `client.ts`, which is server-only. Types cross the boundary; network access
 * does not.
 */

import type { components } from "./types";

type Schemas = components["schemas"];

// ---------------------------------------------------------------------------
// Result model
// ---------------------------------------------------------------------------

/**
 * Why a call did not produce a payload. The sidecar draws a line this
 * preserves: "the books are wrong" is a 200 with `ok: false` and is *not* a
 * failure here — it is data. These four are failures of the transport or the
 * service itself.
 */
export type SidecarFailureKind =
  /** No process answered. The sidecar is probably not running. */
  | "unreachable"
  /** It answered too slowly and we gave up, so a chat turn would not hang. */
  | "timeout"
  /** It answered with a non-2xx status. Reachable, but the request failed. */
  | "http"
  /** It answered 2xx with a body that was not JSON. */
  | "malformed";

export type SidecarFailure = {
  outcome: "failure";
  kind: SidecarFailureKind;
  /**
   * A plain sentence, safe to hand to an 8B model or drop into the UI. The
   * tool layer surfaces this string to the model verbatim, so it must read as
   * prose rather than as a stack trace.
   */
  message: string;
  /** Present only when `kind` is `"http"`. */
  status?: number;
  /** The parsed error body, when the sidecar sent one (FastAPI's `detail`). */
  detail?: unknown;
};

export type SidecarSuccess<T> = {
  outcome: "success";
  data: T;
};

/**
 * Errors are values, not thrown surprises: nothing in `client.ts` throws for a
 * failed call. The discriminant is `outcome` rather than `ok` because almost
 * every sidecar payload *also* has an `ok` field with an unrelated meaning —
 * `/verify` returns `ok: false` for a ledger that fails its checks, over a
 * perfectly successful HTTP call. Two nested `ok`s would be a standing
 * invitation to check the wrong one.
 */
export type SidecarResult<T> = SidecarSuccess<T> | SidecarFailure;

export function isSidecarSuccess<T>(
  result: SidecarResult<T>
): result is SidecarSuccess<T> {
  return result.outcome === "success";
}

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

/**
 * Money crosses this boundary as a *string*, never a number.
 *
 * Pydantic serializes `Decimal` to a string, so `"-163.36"` arrives verbatim
 * with no float rounding applied to anyone's books. Nothing in the client or
 * the proxy layer coerces these in transit. Parse at the render/format edge,
 * where the formatting decision is visible to a human reading the component —
 * not silently on the way through.
 */
export type DecimalString = string;

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

export type CategorizeRequest = Schemas["CategorizeRequest"];
export type ConfirmRequest = Schemas["ConfirmRequest"];
export type AllocateRequest = Schemas["AllocateRequest"];
export type SyncStartRequest = Schemas["SyncStartRequest"];

/**
 * One human decision about one transaction.
 *
 * `simplefin_id` *and* `asset_account` together are the key — `simplefin_id`
 * alone is not unique across accounts. Getting this wrong is the failure that
 * looks like success: a batch keyed on the wrong field matches nothing, and
 * "confirmed 0 of 40" reads like an empty queue rather than like a bug.
 */
export type Confirmation = Schemas["ConfirmationModel"];

// ---------------------------------------------------------------------------
// Responses
// ---------------------------------------------------------------------------

export type HealthResponse = Schemas["HealthResponse"];
export type VerifyResponse = Schemas["VerifyResponse"];
export type AccountsResponse = Schemas["AccountsResponse"];

/**
 * The accounts a transaction may be recategorized *into* — open `Expenses:*`
 * and `Income:*`, minus the `Unknown` catch-alls. Distinct from
 * `AccountsResponse`, which is the `Assets:` side and is the wrong set for a
 * correction dropdown.
 *
 * The review UI pre-validates against this before confirming, because
 * `POST /review/confirm` rejects the whole batch if any account is not open —
 * without the check, one bad account costs the user their other 39 approvals.
 */
export type CategorizableAccountsResponse =
  Schemas["CategorizableAccountsResponse"];

export type EnvelopeReportResponse = Schemas["EnvelopeReportResponse"];
export type EnvelopeBalance = Schemas["EnvelopeBalanceModel"];

export type ReviewQueueResponse = Schemas["ReviewQueueResponse"];
export type ReviewQueue = Schemas["ReviewQueueModel"];
export type ReviewEntry = Schemas["ReviewEntryModel"];

export type ConfirmResponse = Schemas["ConfirmResponse"];
export type CommitInfo = Schemas["CommitModel"];

export type AllocateResponse = Schemas["AllocateResponse"];

export type SyncStartResponse = Schemas["SyncStartResponse"];
export type SyncStatusResponse = Schemas["SyncStatusResponse"];
export type SyncJobResult = Schemas["SyncJobResult"];

export type TransactionSearchResponse = Schemas["TransactionSearchResponse"];
export type TransactionMatch = Schemas["TransactionMatchModel"];

export type SpendingReportResponse = Schemas["SpendingReportResponse"];
export type EnvelopeSeries = Schemas["EnvelopeSeriesModel"];
export type SpendPoint = Schemas["SpendPointModel"];

/**
 * What the matches of a transaction search add up to, per currency.
 *
 * Note the name: `total` on `TransactionSearchResponse` is a **count** and
 * `amount_totals` is the money. The sidecar named them apart deliberately —
 * `total` and `totals` one keystroke apart in generated TypeScript is a bug
 * waiting to be written — and that is worth preserving on this side too.
 */
export type CurrencyTotal = Schemas["CurrencyTotalModel"];

export type BudgetReportResponse = Schemas["BudgetReportResponse"];
export type BudgetLine = Schemas["BudgetLineModel"];

export type TrendsReportResponse = Schemas["TrendsReportResponse"];
export type EnvelopeTrend = Schemas["EnvelopeTrendModel"];
export type Outlier = Schemas["OutlierModel"];
/**
 * Why an envelope was or was not judged for outliers.
 *
 * Returned for *every* envelope, judged or not, which is the point: it keeps
 * "nothing unusual was found" distinguishable from "there was not enough data
 * to look", and those are a finding and the absence of one.
 */
export type OutlierAssessment = Schemas["OutlierAssessmentModel"];

export type MonthEndReportResponse = Schemas["MonthEndReportResponse"];
export type MonthEndEnvelope = Schemas["MonthEndEnvelopeModel"];

export type CategorizeResponse = Schemas["CategorizeResponse"];
export type CategorizeResult = Schemas["CategorizeResultModel"];

// ---------------------------------------------------------------------------
// The callable surface
// ---------------------------------------------------------------------------

export type CallOptions = {
  /**
   * Cancels the request early — pass the chat turn's signal so an abandoned
   * turn does not leave the sidecar working on a result nobody will read.
   */
  signal?: AbortSignal;
};

/**
 * What `client.ts` implements, stated as a type so callers can declare a port
 * against it instead of restating the signatures.
 *
 * `confirmReview` is present here but is **not** part of the tool surface. It
 * is reachable only from `POST /api/bookkeeper/review/confirm`, which the
 * browser calls directly on a button click (PLAN.md §5.3 rule 2): approving
 * forty transactions must be forty deterministic HTTP calls and zero LLM
 * calls. Exposing it as a tool would lose the design.
 */
export type SidecarPort = {
  getHealth: (options?: CallOptions) => Promise<SidecarResult<HealthResponse>>;
  getVerify: (options?: CallOptions) => Promise<SidecarResult<VerifyResponse>>;
  getCategorizableAccounts: (
    options?: CallOptions
  ) => Promise<SidecarResult<CategorizableAccountsResponse>>;
  getEnvelopes: (
    params?: { asof?: string | null },
    options?: CallOptions
  ) => Promise<SidecarResult<EnvelopeReportResponse>>;
  getReviewQueue: (
    params?: { limit?: number | null },
    options?: CallOptions
  ) => Promise<SidecarResult<ReviewQueueResponse>>;
  searchTransactions: (
    params: { q: string; limit?: number | null },
    options?: CallOptions
  ) => Promise<SidecarResult<TransactionSearchResponse>>;
  /**
   * The query params are `from` and `to`; the *response* names them
   * `from_date` and `to_date`. `from` is a Python keyword, so the sidecar
   * aliases it on the way in and cannot on the way out.
   */
  getSpendingReport: (
    params?: { from?: string | null; to?: string | null },
    options?: CallOptions
  ) => Promise<SidecarResult<SpendingReportResponse>>;
  /**
   * `month` is `YYYY-MM`. Omitting it selects the sidecar's default — the
   * month of the ledger's last transaction, not the wall-clock month.
   */
  getMonthEndReport: (
    params?: { month?: string | null },
    options?: CallOptions
  ) => Promise<SidecarResult<MonthEndReportResponse>>;
  getBudgetReport: (
    params?: { from?: string | null; to?: string | null },
    options?: CallOptions
  ) => Promise<SidecarResult<BudgetReportResponse>>;
  getTrendsReport: (
    params?: { from?: string | null; to?: string | null },
    options?: CallOptions
  ) => Promise<SidecarResult<TrendsReportResponse>>;
  startSync: (
    body: SyncStartRequest,
    options?: CallOptions
  ) => Promise<SidecarResult<SyncStartResponse>>;
  getSyncStatus: (
    jobId: string,
    options?: CallOptions
  ) => Promise<SidecarResult<SyncStatusResponse>>;
  allocateToEnvelope: (
    body: AllocateRequest,
    options?: CallOptions
  ) => Promise<SidecarResult<AllocateResponse>>;
  confirmReview: (
    body: ConfirmRequest,
    options?: CallOptions
  ) => Promise<SidecarResult<ConfirmResponse>>;
  categorize: (
    body: CategorizeRequest,
    options?: CallOptions
  ) => Promise<SidecarResult<CategorizeResponse>>;
};
