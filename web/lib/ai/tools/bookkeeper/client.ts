// The narrow slice of the sidecar that the six chat tools of PLAN.md §5.3
// need, and nothing else.
//
// This is a *port*, not a client. The real HTTP client lives in
// lib/sidecar/client.ts; every tool here depends on this interface instead,
// for two reasons. It makes each tool unit-testable with a fake and no
// network, and it means the six tools name exactly the six operations they
// are allowed to perform — a tool cannot reach a seventh endpoint even by
// accident. Notably absent: `POST /review/confirm`. Confirmation is reached
// only by button clicks hitting the API directly (§5.3 rule 2), so it is not
// in the port at all rather than merely unused by a tool.

/**
 * Errors are values, never exceptions.
 *
 * PLAN.md §3.3: small models "fall into invocation loops when error handling
 * is loose". A thrown exception kills the turn and gives the model nothing to
 * respond to; a value it can read produces "I couldn't reach the ledger
 * service" and a stop. Every port method returns one of these.
 */
export type SidecarResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

export type SyncStarted = {
  job_id: string;
  /**
   * False when a sync was already running and this is *that* job's id rather
   * than a second one. Not an error — SimpleFIN allows only ~24 requests a day
   * (PLAN.md §3.1), so refusing to launch a duplicate is the point. Polling is
   * identical either way.
   */
  started: boolean;
};

/**
 * The payload shapes below are the sidecar's, field for field, confirmed
 * against the live `/openapi.json` (14 endpoints, no bare dicts).
 *
 * All four Phase 4 endpoints are **flat**: the domain object's fields sit at
 * the top level beside `ok` and `summary`. Only `/review-queue` keeps a
 * wrapper, under the key `queue`. `sidecar-adapter.ts` does two translations
 * the sidecar does not do for us — the spending report nests points under each
 * envelope where the chart wants them flat, and `shown` is computed because
 * the API does not emit it.
 */

/**
 * One transaction awaiting a human decision. Mirrors the sidecar's
 * `ReviewEntry` (categorize/review.py), which is deliberately primitives-only
 * so it survives JSON without a custom encoder. Amounts stay **strings**: they
 * are decimals in the ledger and turning them into IEEE floats on the way to
 * the browser would be a rounding bug in a financial record.
 */
export type ReviewEntry = {
  simplefin_id: string;
  asset_account: string;
  posted_date: string;
  description: string;
  amount: string;
  currency: string;
  current_account: string;
  suggested_account: string | null;
  confidence: number | null;
  tier: string | null;
  rationale: string;
  mcc: string | null;
  payee: string | null;
};

/**
 * Mirrors `ReviewQueueModel`, the payload under `ReviewQueueResponse.queue`.
 *
 * `total` is the size of the whole queue; `shown` is how much of it came back.
 * The tool caps the request, so those two differ routinely and the card has to
 * say so. `shown` is computed from `entries` — the API does not send it.
 */
export type ReviewQueue = {
  ok: boolean;
  total: number;
  shown: number;
  entries: ReviewEntry[];
  warnings: string[];
  errors: string[];
};

export type EnvelopeBalance = {
  name: string;
  allocated: string;
  spent: string;
  balance: string;
  overspent: boolean;
  overspend: string;
};

export type EnvelopeReport = {
  asof: string;
  envelopes: EnvelopeBalance[];
  budgeted_cash: string;
  total_envelope_balance: string;
  total_overspend: string;
  available: string;
  summary: string;
};

/** One envelope's spend in one period. Flattened from the sidecar's per-envelope series. */
export type SpendingPoint = {
  period: string;
  envelope: string;
  amount: string;
};

export type SpendingReport = {
  ok: boolean;
  from: string;
  to: string;
  /** `"month"` today. The chart's axis formats itself from this. */
  granularity: string;
  currency: string;
  /** Every period label in the window, including ones with no spend. */
  periods: string[];
  points: SpendingPoint[];
  total: string;
  /**
   * Spend in the window that belongs to no envelope. Reported rather than
   * folded into `total`: with auto-apply off almost everything is still in
   * `Expenses:Unknown`, so a chart that silently dropped it would look empty
   * for the wrong reason.
   */
  unmapped_total: string;
  warnings: string[];
  errors: string[];
};

/** Mirrors `reports/search.py`'s `TransactionMatch.to_dict()`. */
export type TransactionMatch = {
  posted_date: string;
  description: string;
  amount: string;
  currency: string;
  account: string;
  categorized_account: string | null;
  envelope: string | null;
  /** Null for ledger entries that did not come from SimpleFIN. */
  simplefin_id: string | null;
  payee: string | null;
  memo: string | null;
};

export type TransactionSearchResult = {
  ok: boolean;
  query: string;
  total: number;
  shown: number;
  limit: number;
  truncated: boolean;
  matches: TransactionMatch[];
  warnings: string[];
  errors: string[];
};

/**
 * Mirrors `AllocateResult.to_dict()`.
 *
 * `ok` is part of the payload rather than the transport: the sidecar answers
 * 200 with `ok: false` for a refused allocation (an unknown envelope name),
 * the same way `/verify` reports a failing ledger. The tool has to check it —
 * an HTTP 200 here does not mean anything was written.
 */
export type AllocationConfirmation = {
  ok: boolean;
  envelope: string;
  amount: string;
  currency: string;
  /** ISO date the directive was written under; null if nothing was written. */
  allocated_on: string | null;
  directive: string;
  path: string;
  /** Unallocated cash after this allocation, or null when it could not be computed. */
  available: string | null;
  over_allocated: boolean;
  known_envelopes: string[];
  warnings: string[];
  errors: string[];
};

export type BookkeeperClient = {
  startSync: (input: { since?: string }) => Promise<SidecarResult<SyncStarted>>;
  getReviewQueue: (input: {
    limit?: number;
  }) => Promise<SidecarResult<ReviewQueue>>;
  getEnvelopes: (input: {
    asof?: string;
  }) => Promise<SidecarResult<EnvelopeReport>>;
  getSpendingReport: (input: {
    from: string;
    to: string;
  }) => Promise<SidecarResult<SpendingReport>>;
  searchTransactions: (input: {
    q: string;
    limit?: number;
  }) => Promise<SidecarResult<TransactionSearchResult>>;
  allocateToEnvelope: (input: {
    envelope: string;
    amount: string;
    currency: string;
    date?: string;
  }) => Promise<SidecarResult<AllocationConfirmation>>;
};
