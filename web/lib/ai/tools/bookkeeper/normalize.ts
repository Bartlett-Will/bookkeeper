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
  CurrencyTotal,
  MonthEndReport,
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

/**
 * A FastAPI 422 body, rewritten as one sentence naming the offending fields.
 *
 * The sidecar sets `extra="forbid"` on its request models, so a field this
 * layer should not be sending is now a 422 rather than a silently dropped
 * value — which is what turned a wrong date in a financial record into an
 * error at the first request. The detail arrives as
 * `[{type, loc: ["body", "date"], msg, input}]`, and `lib/sidecar` hands it on
 * as raw JSON because its other consumers (the proxies, the logs) want the
 * structure.
 *
 * The model does not. A JSON array in an 8B's context is precisely the loose
 * error handling PLAN.md §3.3 names as the cause of invocation loops, so the
 * field path is what gets extracted and the rest is dropped. `loc` keeps its
 * array index for nested bodies — `["body","confirmations",0,"simplefin_id"]`
 * becomes `confirmations[0].simplefin_id`, which tells you *which* of forty
 * confirmations is wrong.
 *
 * Returns null when the body is not a recognisable validation error, so the
 * caller falls back to the transport's own message rather than inventing one.
 */
export function describeValidationFailure(detail: unknown): string | null {
  const items = objList(detail);
  if (items.length === 0) {
    return null;
  }

  const fields = items
    .map((item) => {
      const loc = Array.isArray(item.loc) ? item.loc : [];
      // Drop the leading "body"/"query" segment: it names where the value came
      // from, which the model can do nothing with.
      const path = loc
        .slice(1)
        .map((part) => (typeof part === "number" ? `[${part}]` : `.${part}`))
        .join("")
        .replace(/^\./, "");
      return path;
    })
    .filter((path) => path.length > 0);

  if (fields.length === 0) {
    return null;
  }
  const label = fields.length === 1 ? "field" : "fields";
  return `the request was rejected because the ${label} ${fields.join(", ")} ${
    fields.length === 1 ? "is" : "are"
  } not accepted by the ledger service.`;
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

/**
 * `CurrencyTotalModel[]`, defended against being absent.
 *
 * An older sidecar has no `amount_totals` at all, and the safe reading of that
 * is an empty list — "no total was computed" — which the card renders as no
 * total. The dangerous alternative would be synthesising a zero, which is
 * indistinguishable on screen from a real total of nothing.
 */
function toCurrencyTotals(value: unknown): CurrencyTotal[] {
  return objList(value).map((total) => ({
    accounts: strList(total.accounts),
    currency: str(total.currency),
    net: decimal(total.net),
    receipt_count: num(total.receipt_count),
    received: decimal(total.received),
    spend_count: num(total.spend_count),
    spent: decimal(total.spent),
    transfer_count: num(total.transfer_count),
    transferred: decimal(total.transferred),
  }));
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
    amount_totals: toCurrencyTotals(data.amount_totals),
    errors: strList(data.errors),
    limit: num(data.limit, matches.length),
    matches,
    // Absent reads as "not mixed". The field only exists on a sidecar that
    // computes `amount_totals`, and on one that does not there are no totals
    // to be ambiguous about.
    mixed_currency: bool(data.mixed_currency),
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

/**
 * `MonthEndReportResponse` → the port's `MonthEndReport`.
 *
 * Flat, like the other report endpoints, with the same `from_date`/`to_date`
 * naming as `/reports/spending` — the query param is `month` but the day
 * bounds come back suffixed, because `from` is a Python keyword and the
 * sidecar can alias it going in and not coming out. That asymmetry has now cost
 * one bug; reading it off a live body is what caught it then.
 *
 * The defaults matter more here than elsewhere, because this is the report a
 * user reads as final. `coverage` falls back to `"no-data"` and
 * `categorization` to `"none"`: both are the *cautious* end of their enum, and
 * both are what the model is handed as fact by `sayableFacts`. A missing field
 * defaulting to `"complete"` or `"full"` would turn a body we failed to
 * understand into a confident statement that the month is finished and fully
 * categorized, which is the one direction this must never fail in.
 */
export function toMonthEndReport(
  body: unknown,
  requestedMonth?: string
): MonthEndReport {
  const data = record(body);
  const envelopes = objList(data.envelopes).map((envelope) => ({
    allocated: decimal(envelope.allocated),
    closing_balance: decimal(envelope.closing_balance),
    direction: str(envelope.direction),
    direction_reason: str(envelope.direction_reason),
    name: str(envelope.name),
    opening_balance: decimal(envelope.opening_balance),
    // Absent reads as false for both, and for once that is not the cautious
    // choice — it is the only honest one. These two drive a sentence the model
    // is told is true, and defaulting them to true would manufacture an alarm
    // out of a field we could not read.
    over_budget: bool(envelope.over_budget),
    overspend: decimal(envelope.overspend),
    overspent: bool(envelope.overspent),
    // Null, not "0". Nothing allocated means the percentage is undefined, and
    // a zero here would render as "0% used" — a plausible figure standing in
    // for a line nobody has budgeted.
    percent_consumed: nullableStr(envelope.percent_consumed),
    remaining: decimal(envelope.remaining),
    spent: decimal(envelope.spent),
    status: str(envelope.status),
  }));
  // An *absent* `unjudged` is not an empty one, and defaulting it to `[]` was
  // a real bug here for a few minutes: empty means "every envelope was
  // examined", which is precisely what licenses the model to say nothing looks
  // unusual. A body without the field cannot support that claim, so absence
  // fills in as "none of them were examined" — the reading that withholds the
  // reassurance rather than manufacturing it.
  const unjudged = Array.isArray(data.unjudged)
    ? strList(data.unjudged)
    : envelopes.map((envelope) => envelope.name);
  const outliers = objList(data.outliers).map((outlier) => ({
    amount: decimal(outlier.amount),
    description: str(outlier.description),
    envelope: str(outlier.envelope),
    median: decimal(outlier.median),
    posted_date: str(outlier.posted_date),
    scale: decimal(outlier.scale),
    scale_method: str(outlier.scale_method),
    score: decimal(outlier.score),
    threshold: decimal(outlier.threshold),
  }));
  return {
    allocated_total: decimal(data.allocated_total),
    asof: str(data.asof),
    available: decimal(data.available),
    budgeted_cash: decimal(data.budgeted_cash),
    categorization: str(data.categorization, "none"),
    categorized_share: decimal(data.categorized_share),
    closing_total: decimal(data.closing_total),
    coverage: str(data.coverage, "no-data"),
    currency: str(data.currency, "USD"),
    data_through: nullableStr(data.data_through),
    envelopes,
    errors: strList(data.errors),
    from: str(data.from_date),
    label: str(data.label),
    month: str(data.month, requestedMonth ?? ""),
    ok: bool(data.ok, true),
    opening_total: decimal(data.opening_total),
    outliers,
    spent_total: decimal(data.spent_total),
    summary: str(data.summary),
    to: str(data.to_date),
    total_overspend: decimal(data.total_overspend),
    total_spend: decimal(data.total_spend),
    transactions: num(data.transactions),
    trend_from: nullableStr(data.trend_from),
    trend_to: nullableStr(data.trend_to),
    unjudged,
    unmapped_accounts: strList(data.unmapped_accounts),
    unmapped_total: decimal(data.unmapped_total),
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
