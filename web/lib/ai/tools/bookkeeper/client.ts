// The narrow slice of the sidecar that the seven chat tools need — the six of
// PLAN.md §5.3 plus Phase 5's `get_month_end_report` — and nothing else.
//
// This is a *port*, not a client. The real HTTP client lives in
// lib/sidecar/client.ts; every tool here depends on this interface instead,
// for two reasons. It makes each tool unit-testable with a fake and no
// network, and it means the tools name exactly the operations they are allowed
// to perform — a tool cannot reach an endpoint outside this list even by
// accident. Notably absent: `POST /review/confirm`. Confirmation is reached
// only by button clicks hitting the API directly (§5.3 rule 2), so it is not
// in the port at all rather than merely unused by a tool.

import type {
  CurrencyTotal as WireCurrencyTotal,
  MonthEndReportResponse as WireMonthEnd,
  MonthEndEnvelope as WireMonthEndEnvelope,
} from "@/lib/sidecar/contract";

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
  /** The accounts that `unmapped_total` came from — today, mostly `Expenses:Unknown`. */
  unmapped_accounts: string[];
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

/** Mirrors `CurrencyTotalModel`. Every amount is a decimal string. */
export type CurrencyTotal = {
  currency: string;
  spent: string;
  received: string;
  net: string;
  /** Money moved between the user's own accounts. In neither `spent` nor `received`. */
  transferred: string;
  spend_count: number;
  receipt_count: number;
  transfer_count: number;
  accounts: string[];
};

export type TransactionSearchResult = {
  ok: boolean;
  query: string;
  /**
   * How many transactions matched — a **count**, and the older of the two
   * totals on this type. The name predates the money one and is not worth a
   * rename: it is what the sidecar sends.
   */
  total: number;
  /**
   * What the matches add up to — one entry per currency, never one scalar.
   *
   * This is the Phase 5 fix for the coverage gap in "how much did I spend at
   * Whole Foods": search found the rows and totalled nothing, spending grouped
   * by envelope and not by merchant, so the only route to an answer was the
   * model adding up a list — the arithmetic §5.3 rule 1 exists to prevent.
   *
   * The shape is a list because a scalar would have been a lie. A match set
   * spans currencies and both directions, so `spent` and `received` stay apart
   * (a refund must not quietly shrink the answer to "how much did I spend"),
   * and transfers between the user's own accounts are in neither — the search
   * returns both legs, and counting them would report the same dollar twice.
   *
   * Totalled over *every* match, not just the `limit`ed page in `matches`.
   */
  amount_totals: CurrencyTotal[];
  /**
   * True when the matches span more than one currency, so no combined figure
   * exists. The ledger carries no exchange rates; the card renders each
   * currency separately rather than picking one.
   */
  mixed_currency: boolean;
  shown: number;
  limit: number;
  truncated: boolean;
  matches: TransactionMatch[];
  warnings: string[];
  errors: string[];
};

/** The git commit an allocation produced. Git is the undo system (PLAN.md §9). */
export type AllocationCommit = {
  ok: boolean;
  committed: boolean;
  /** What to `git revert`. The only reason this is carried to the UI. */
  sha: string;
  message: string;
  files: string[];
  warnings: string[];
};

/**
 * Mirrors `AllocateResponse`.
 *
 * `ok` is part of the payload rather than the transport: the sidecar answers
 * 200 with `ok: false` for a refused allocation — an unknown envelope, a
 * non-positive amount, a bad currency, or a directive that would not parse —
 * the same way `/verify` reports a failing ledger. The tool has to check it;
 * an HTTP 200 here does not mean anything was written. Keeping refusals on the
 * 200 path is also what preserves `known_envelopes`, which is what lets a
 * model correct an invented envelope name without another round trip.
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
  /** Reported, never prevented — `verify` is what judges a budget. Still a successful write. */
  over_allocated: boolean;
  known_envelopes: string[];
  /** Null when nothing was written. The endpoint always commits when it does write. */
  commit: AllocationCommit | null;
  warnings: string[];
  errors: string[];
};

/**
 * One envelope's month. Mirrors `MonthEndEnvelopeModel`.
 *
 * `overspent` and `over_budget` are **two different failures** and the sidecar
 * keeps them apart, so nothing on this side may collapse them. Overspent means
 * the running balance went negative — money already spent with nothing behind
 * it. Over budget means this month's spend exceeded this month's allocation,
 * which an envelope carrying a healthy balance can do without ever going
 * negative. Treating either as the other raises a false alarm or hides a real
 * one, and both of those reach the user as a sentence about their money.
 */
export type MonthEndEnvelope = {
  name: string;
  opening_balance: string;
  allocated: string;
  spent: string;
  closing_balance: string;
  /** Allocated less spent, for this month alone. Signed by the sidecar. */
  remaining: string;
  overspend: string;
  /**
   * Fraction of the allocation consumed — `1.5` means 150%, not 150.
   *
   * **The one field in this file that is a JSON `number` rather than a decimal
   * string, and deliberately so: a ratio is not money.** It is derived from two
   * amounts but is not itself an amount, so it carries no rounding obligation
   * and nothing downstream should treat it as currency.
   *
   * Null **iff** nothing was allocated, and the null is the point: against a
   * zero allocation both `0` and `1` are lies, in opposite directions. Do not
   * coerce it. `status` carries the reason — `unbudgeted` (spent against
   * nothing) versus `unused` (mapped, untouched) — which a null alone cannot
   * distinguish.
   */
  consumed_ratio: number | null;
  /** `within` | `over` | `unbudgeted` | `unused`. */
  status: string;
  /**
   * `up` | `down` | `flat` | `insufficient_data`, read over a trailing window
   * rather than this month alone.
   *
   * The fourth value is **abstention, not a magnitude**. `flat` means the slope
   * was measured and came out small; `insufficient_data` means it was never
   * measured. Collapsing them turns "we did not look" into "we looked and all
   * was well", which is the unfalsifiable claim this phase is trying to avoid.
   */
  direction: string;
  /** How that verdict was reached, including when it abstained. */
  direction_reason: string;
  /** The two counts that make an abstention checkable rather than merely asserted. */
  periods_observed: number;
  periods_required: number;
  overspent: boolean;
  over_budget: boolean;
};

/**
 * A transaction the sidecar judged unusual for its envelope.
 *
 * Self-contained: `median`, `scale` and `threshold` travel with the flag so
 * `(amount − median) / scale` can be checked from the card alone. None of that
 * arithmetic happens on this side — the fields are carried so a human can
 * audit a finding, not so the UI can recompute one.
 */
export type MonthEndOutlier = {
  envelope: string;
  posted_date: string;
  description: string;
  amount: string;
  score: string;
  median: string;
  scale: string;
  scale_method: string;
  threshold: string;
};

/**
 * One named calendar month, closed out. Mirrors `MonthEndReportResponse`.
 *
 * A composite, and the only one. It exists as its own endpoint and its own tool
 * because "how did July go" is a real question with a real answer, and the
 * alternative — the model calling four tools and stitching them together — is
 * the multi-step chaining PLAN.md §3.3 says an 8B cannot do and §5.3 is built
 * to avoid. One call, one card, one step.
 *
 * Two fields here are enums rather than numbers, and they are the most
 * important fields on the type. `coverage` says what period the report is
 * actually about — "so far this month" and "for July" are different claims a
 * user acts on differently — and `categorization` says how much of the month's
 * spending reached an envelope at all. With auto-apply off that is routinely
 * most of it, so a report that did not say so would be a clean-looking summary
 * of a ledger nobody has finished categorizing. They are also the only fields
 * the *model* is given (see `sayableFacts` in `get-month-end-report.ts`):
 * a verdict the sidecar reached in `Decimal` is something it can repeat
 * without inventing anything.
 */
export type MonthEndReport = {
  ok: boolean;
  /** The month this covers, `YYYY-MM`. Echoed so the card cannot mislabel itself. */
  month: string;
  /** The month in words, e.g. `"July 2026"`. Rendered, never parsed. */
  label: string;
  /**
   * The month's day bounds. Named `from`/`to` here and `from_date`/`to_date`
   * on the wire — the same asymmetry `/reports/spending` has, for the same
   * reason (`from` is a Python keyword), and the same one that cost Phase 4 a
   * bug. `normalize.ts` is where the two names meet.
   */
  from: string;
  to: string;
  /**
   * What the closing figures were computed at: the month's last day, or today
   * when the month is not over. Never a future date, so an allocation dated
   * the 31st cannot appear in a report run on the 5th.
   */
  asof: string;
  /** `future` | `in-progress` | `no-data` | `partial` | `complete`. */
  coverage: string;
  /**
   * The last day in the month the ledger actually has a transaction on, or
   * null for an empty month. This is what makes `"partial"` legible: a month
   * whose data stops on the 12th is not a month of no spending after the
   * 12th, it is a month nobody has synced since.
   */
  data_through: string | null;
  /**
   * The last day this report actually speaks for — `to` for a finished month,
   * today for one still running. Distinct from `data_through`, which is where
   * the *data* stops; this is where the *report* stops.
   */
  through: string | null;
  /** True only for a month that has both ended and been fully recorded. */
  complete: boolean;
  /**
   * How much of the month has elapsed. Two integers rather than a fraction,
   * so "so far this month" is a statement the reader can check rather than a
   * proportion computed somewhere they cannot see.
   */
  days_elapsed: number;
  days_in_month: number;
  transactions: number;
  /**
   * The month's transactions split by whether they reached an envelope.
   *
   * Counts, not amounts, and the pair matters more than either alone: with
   * auto-apply off `uncategorized_count` is routinely most of the month, which
   * is what makes a tidy-looking per-envelope table misleading.
   */
  categorized_count: number;
  uncategorized_count: number;
  currency: string;
  envelopes: MonthEndEnvelope[];
  opening_total: string;
  allocated_total: string;
  spent_total: string;
  closing_total: string;
  /** Spending this month that reached no envelope. */
  unmapped_total: string;
  unmapped_accounts: string[];
  /**
   * `spent_total + unmapped_total` — the month's actual spending, and the
   * denominator the per-envelope table has to be read against. Summed in the
   * sidecar; adding the two strings here would be the arithmetic this layer
   * exists to keep out.
   */
  total_spend: string;
  /** `none` | `partial` | `full` | `no-spend`. */
  categorization: string;
  /**
   * Share of the month's spending that reached an envelope, 0–1.
   *
   * A `Decimal` string, and one that arrives in forms `parseFloat` handles but
   * a naive string check does not — a live body returned `"0E+2"`. Anything
   * rendering this must parse it, not test it against `"0"`.
   */
  categorized_share: string;
  budgeted_cash: string;
  available: string;
  total_overspend: string;
  /** Transactions in this month judged unusual for their envelope. */
  outliers: MonthEndOutlier[];
  /** The window the trend verdicts were computed over. Null when no trends ran. */
  trend_from: string | null;
  trend_to: string | null;
  /**
   * Envelopes the sidecar declined to judge for outliers, for want of enough
   * history.
   *
   * The single most useful field on this type for not lying to the user. An
   * empty `outliers` means "nothing was flagged", which is only "nothing looks
   * unusual" if something was actually examined — and when every envelope is
   * listed here, nothing was. `sayableFacts` reads exactly this distinction.
   */
  unjudged: string[];
  summary: string;
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
  /**
   * `month` is `YYYY-MM`, already validated, and **optional on purpose**.
   *
   * Omitting it is not a missing argument — it selects the sidecar's default,
   * which is the month of the ledger's *last transaction* rather than the
   * wall-clock month. That is the better default and it is not one this side
   * could compute: it depends on the ledger. It also sidesteps a measured
   * failure — asked "how did July go" with no date in its prompt, `qwen3:8b`
   * answered `2023-07`, the year from its training data.
   */
  getMonthEndReport: (input: {
    month?: string;
  }) => Promise<SidecarResult<MonthEndReport>>;
  searchTransactions: (input: {
    q: string;
    limit?: number;
  }) => Promise<SidecarResult<TransactionSearchResult>>;
  allocateToEnvelope: (input: {
    envelope: string;
    /** A decimal string, never a number — it is a `Decimal` and a JSON double would round it. */
    amount: string;
    currency: string;
    /** Named for the wire field (`AllocateRequest.allocated_on`), not "date". */
    allocated_on?: string;
  }) => Promise<SidecarResult<AllocationConfirmation>>;
};

// ---------------------------------------------------------------------------
// Conformance to the generated schema
// ---------------------------------------------------------------------------

/**
 * The port types above are hand-written, and hand-written copies of a
 * financial API drift. Twice in Phase 5 this one did, silently: the sidecar
 * renamed `percent_consumed` to `consumed_ratio` and changed it from a decimal
 * string to a JSON number, and separately added six fields the port never
 * carried. Both compiled. Both would have rendered a wrong report — every
 * envelope reading as unbudgeted in the first case, two views with no data
 * path in the second.
 *
 * The checks below turn that class of drift into a build failure naming the
 * field. They compare against `lib/sidecar/types.ts`, which is **generated**
 * from the sidecar's OpenAPI schema, so the generated file is the authority
 * and the port is what must conform.
 *
 * The assertion is deliberately on *keys* rather than full structural
 * assignability. What has actually bitten is the wire growing or renaming a
 * field the port does not carry; a key check catches exactly that and does not
 * produce false failures over the optionality differences that OpenAPI
 * generation introduces (`consumed_ratio?: number | null` on the wire against a
 * required `number | null` here, which `normalize.ts` guarantees).
 *
 * Type-only imports, erased at compile time — `contract.ts` carries no
 * `server-only`, so this adds nothing to any bundle.
 */
type Assert<T extends true> = T;

/** True when the wire type has no key the port is missing. */
type PortCovers<Wire, Port, Renamed extends string = never> =
  Exclude<keyof Wire, keyof Port | Renamed> extends never ? true : false;

/**
 * `from_date`/`to_date` are the two deliberate renames — the query params are
 * `from`/`to` and the response suffixes them, because `from` is a Python
 * keyword the sidecar can alias going in and not coming out. They are excused
 * here by name so that every *other* rename still fails the build.
 */
export type ContractAssertions = [
  Assert<PortCovers<WireMonthEnd, MonthEndReport, "from_date" | "to_date">>,
  Assert<PortCovers<WireMonthEndEnvelope, MonthEndEnvelope>>,
  Assert<PortCovers<WireCurrencyTotal, CurrencyTotal>>,
];
