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
  AllocationConfirmation,
  ReviewQueue,
  SpendingReport,
  SyncStarted,
  TransactionSearchResult,
} from "./client";

/**
 * Sidecar payloads arrive inside a `{ok, summary, <key>: {...}}` envelope —
 * `/review-queue` names the key `queue`, `/categorize` names it `result`.
 * The inner `to_dict()` already carries its own `ok`, `errors` and `warnings`,
 * so the envelope holds nothing the tools need.
 *
 * Taking the first object-valued property that is not `ok` or `summary`
 * handles a wrapped payload and a flat one with the same code, which matters
 * while the endpoints are still being written. Arrays are skipped so a
 * top-level `entries`/`matches` on a flattened body does not get mistaken for
 * the envelope's payload.
 */
export function unwrap(body: unknown): Record<string, unknown> {
  if (typeof body !== "object" || body === null) {
    return {};
  }
  const record = body as Record<string, unknown>;
  for (const [key, value] of Object.entries(record)) {
    if (key === "ok" || key === "summary") {
      continue;
    }
    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
  }
  return record;
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

/** `POST /sync/start` answers with `{job_id}` or a whole `JobSnapshot`; both put `job_id` at the top level. */
export function toSyncStarted(body: unknown): SyncStarted {
  // Deliberately not `unwrap`ped: a `JobSnapshot` has a nested `result` object
  // that `unwrap` would descend into, losing the `job_id` beside it.
  const record = (body ?? {}) as Record<string, unknown>;
  return { job_id: str(record.job_id) };
}

export function toReviewQueue(body: unknown): ReviewQueue {
  const data = unwrap(body);
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
    shown: num(data.shown, entries.length),
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
  const data = unwrap(body);
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
    // `from_date`/`to_date` in the Python — `from` is a reserved word there.
    // Falling back to the requested window keeps the chart labelled rather
    // than blank when the sidecar omits them.
    from: str(data.from_date, requested.from),
    granularity: str(data.period, "month"),
    ok: bool(data.ok, true),
    periods: strList(data.periods),
    points,
    to: str(data.to_date, requested.to),
    total: decimal(data.total),
    unmapped_total: decimal(data.unmapped_total),
    warnings: strList(data.warnings),
  };
}

export function toTransactionSearch(
  body: unknown,
  requestedQuery: string
): TransactionSearchResult {
  const data = unwrap(body);
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
    shown: num(data.shown, matches.length),
    total: num(data.total, matches.length),
    truncated: bool(data.truncated),
    warnings: strList(data.warnings),
  };
}

export function toAllocation(
  body: unknown,
  requested: { envelope: string; currency: string }
): AllocationConfirmation {
  const data = unwrap(body);
  return {
    allocated_on: nullableStr(data.allocated_on),
    amount: decimal(data.amount),
    available: nullableStr(data.available),
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
