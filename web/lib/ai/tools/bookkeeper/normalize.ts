// Sidecar JSON → the port's payload types.
//
// Split out of `sidecar-adapter.ts` because that file is `server-only` and so
// cannot be imported by a test. The mapping is the part most likely to be
// wrong — four of the six endpoints are being written concurrently and the
// shapes are read off Python `to_dict()` methods rather than a published
// schema — so it is the part that most needs to be exercised directly.
//
// Everything here is a pure function of one JSON body. No fetching, no dates,
// no `Number()`: amounts are decimal strings from pydantic's `Decimal`
// serialization and stay strings the whole way to React (PLAN.md §5.1).

import type {
  AllocationCommit,
  AllocationConfirmation,
  ReviewQueue,
  SpendingReport,
  SyncStarted,
  TransactionSearchResult,
} from "./client";

/**
 * A JSON body as a readable record, or an empty one.
 *
 * There was briefly a heuristic here that hunted for the payload — take the
 * first object-valued property that is not `ok` or `summary` — written while
 * the four endpoints were still being built and it was unknown whether they
 * would wrap their payload the way `/review-queue` does. They do not: all four
 * are flat, confirmed against the live `/openapi.json`. The heuristic is gone
 * rather than left in as tolerance, and it is worth recording why, because it
 * was already wrong: `AllocateResponse.commit` is an object, so the search
 * would have descended into the git commit and returned *that* as the
 * allocation. A guess that reads plausibly is not the same as a contract.
 */
function record(body: unknown): Record<string, unknown> {
  return typeof body === "object" && body !== null
    ? (body as Record<string, unknown>)
    : {};
}

/** The one endpoint that does wrap its payload, under a known key. */
function nested(body: unknown, key: string): Record<string, unknown> {
  return record(record(body)[key]);
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v) => typeof v === "string") : [];
}

function objList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? (value.filter((v) => typeof v === "object" && v !== null) as Record<
        string,
        unknown
      >[])
    : [];
}

/** Decimal strings stay strings. A missing one becomes `"0"`, never `NaN`. */
function decimal(value: unknown): string {
  return typeof value === "string" ? value : "0";
}

function nullableStr(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** `SyncStartResponse`: `{job_id, kind, state, started}`. */
export function toSyncStarted(body: unknown): SyncStarted {
  const data = record(body);
  return {
    job_id: str(data.job_id),
    // Absent reads as "already running" rather than "newly started". Claiming
    // a sync began when it did not is the misleading direction, and SimpleFIN
    // only allows ~24 requests a day.
    started: bool(data.started),
  };
}

export function toReviewQueue(body: unknown): ReviewQueue {
  // The only Phase 4 endpoint that wraps: `ReviewQueueResponse.queue`.
  const data = nested(body, "queue");
  const entries = objList(data.entries).map((entry) => ({
    amount: decimal(entry.amount),
    asset_account: str(entry.asset_account),
    confidence: typeof entry.confidence === "number" ? entry.confidence : null,
    currency: str(entry.currency),
    current_account: str(entry.current_account),
    description: str(entry.description),
    mcc: nullableStr(entry.mcc),
    payee: nullableStr(entry.payee),
    posted_date: str(entry.posted_date),
    rationale: str(entry.rationale),
    simplefin_id: str(entry.simplefin_id),
    suggested_account: nullableStr(entry.suggested_account),
    tier: nullableStr(entry.tier),
  }));
  return {
    entries,
    errors: strList(data.errors),
    ok: bool(data.ok, true),
    // Computed, not read: `ReviewQueueModel` has no `shown`. It is
    // `entries.length` by construction.
    shown: entries.length,
    total: num(data.total, entries.length),
    warnings: strList(data.warnings),
  };
}

/**
 * Flattens `envelopes: [{name, points: [{period, amount}]}]` into
 * `points: [{period, envelope, amount}]`.
 *
 * The sidecar's nesting suits its own text rendering; a chart wants one row
 * per cell. Doing it here rather than in the component means the tool result
 * reaches React already in the shape it renders, which is what lets §5.3 rule
 * 1 hold — the payload is displayed directly and the model never touches a
 * number.
 */
export function toSpendingReport(
  body: unknown,
  requested: { from: string; to: string }
): SpendingReport {
  const data = record(body);
  const points = objList(data.envelopes).flatMap((series) => {
    const name = str(series.name);
    return objList(series.points).map((point) => ({
      amount: decimal(point.amount),
      envelope: name,
      period: str(point.period),
    }));
  });
  return {
    currency: str(data.currency, "USD"),
    errors: strList(data.errors),
    // Asymmetric on purpose, and the likeliest thing to get wrong here: the
    // *query params* are `from`/`to`, but the *response* says
    // `from_date`/`to_date`. `from` is a Python keyword, so the sidecar can
    // alias it on the way in and cannot on the way out.
    from: str(data.from_date, requested.from),
    granularity: str(data.period, "month"),
    ok: bool(data.ok, true),
    periods: strList(data.periods),
    points,
    to: str(data.to_date, requested.to),
    total: decimal(data.total),
    unmapped_accounts: strList(data.unmapped_accounts),
    unmapped_total: decimal(data.unmapped_total),
    warnings: strList(data.warnings),
  };
}

export function toTransactionSearch(
  body: unknown,
  requestedQuery: string
): TransactionSearchResult {
  const data = record(body);
  const matches = objList(data.matches).map((match) => ({
    account: str(match.account),
    amount: decimal(match.amount),
    categorized_account: nullableStr(match.categorized_account),
    currency: str(match.currency),
    description: str(match.description),
    envelope: nullableStr(match.envelope),
    memo: nullableStr(match.memo),
    payee: nullableStr(match.payee),
    posted_date: str(match.posted_date),
    simplefin_id: nullableStr(match.simplefin_id),
  }));
  return {
    errors: strList(data.errors),
    limit: num(data.limit, matches.length),
    matches,
    ok: bool(data.ok, true),
    query: str(data.query, requestedQuery),
    // Computed: `TransactionSearchResponse` does not send `shown`.
    // `total` is the pre-limit count, so the two differ whenever `truncated`.
    shown: matches.length,
    total: num(data.total, matches.length),
    truncated: bool(data.truncated),
    warnings: strList(data.warnings),
  };
}

/** `AllocateResponse.commit` — null when nothing was written. */
function toCommit(value: unknown): AllocationCommit | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const data = value as Record<string, unknown>;
  return {
    committed: bool(data.committed),
    files: strList(data.files),
    message: str(data.message),
    ok: bool(data.ok),
    sha: str(data.sha),
    warnings: strList(data.warnings),
  };
}

export function toAllocation(
  body: unknown,
  requested: { envelope: string; currency: string }
): AllocationConfirmation {
  const data = record(body);
  return {
    allocated_on: nullableStr(data.allocated_on),
    amount: decimal(data.amount),
    available: nullableStr(data.available),
    commit: toCommit(data.commit),
    currency: str(data.currency, requested.currency),
    directive: str(data.directive),
    envelope: str(data.envelope, requested.envelope),
    errors: strList(data.errors),
    known_envelopes: strList(data.known_envelopes),
    // Absent `ok` means refused, not accepted. This is the one write in the
    // app, and the safe reading of a body we do not recognise is that nothing
    // was written — the opposite default would report success for a write that
    // never happened.
    ok: bool(data.ok),
    over_allocated: bool(data.over_allocated),
    path: str(data.path),
    warnings: strList(data.warnings),
  };
}
