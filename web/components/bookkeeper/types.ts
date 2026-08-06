/**
 * The sidecar's shapes, as this UI consumes them.
 *
 * These are the narrower, prop-shaped views the components actually read,
 * declared here so a card can be rendered — and reasoned about — without a
 * running sidecar or a registered tool. The *contract* lives upstream: the
 * sidecar publishes OpenAPI, `lib/sidecar/contract.ts` types the HTTP layer,
 * and `lib/ai/tools/bookkeeper/client.ts` declares the six-operation port the
 * tools call. What a card receives is whatever that port returned.
 *
 * The assertions at the bottom of this file are what keep the two in step, and
 * they have already earned their place: this file and the port disagreed about
 * the review queue and the spending report for a while, and nothing failed to
 * compile, because no code had yet been written that assigned one to the
 * other. The drift would have surfaced as `undefined` inside a render. The
 * `Assert` rows turn it into a build error at the moment either side moves.
 *
 * Money is a `string`, matching the sidecar, which serializes `Decimal` as a
 * string rather than a float. Parsing it into a `number` here would reintroduce
 * exactly the representation error the Python side went to the trouble of
 * avoiding, and this is a ledger. `format.ts` converts for display only.
 */

import type {
  EnvelopeReport as PortEnvelopeReport,
  MonthEndReport as PortMonthEndReport,
  ReviewQueue as PortReviewQueue,
  SpendingReport as PortSpendingReport,
} from "@/lib/ai/tools/bookkeeper/client";

/** One transaction awaiting a human decision. Mirrors `ReviewEntry.to_dict()`. */
export type ReviewEntry = {
  simplefin_id: string;
  asset_account: string;
  posted_date: string;
  description: string;
  /** Signed asset-side amount: negative for spending, per ingest's convention. */
  amount: string;
  currency: string;
  current_account: string;
  suggested_account: string | null;
  confidence: number | null;
  /** Which cascade tier produced the suggestion (`memory`, `rule`, `mcc`, …). */
  tier: string | null;
  rationale: string;
  mcc: string | null;
  payee: string | null;
};

/** Mirrors `ReviewQueue.to_dict()`. */
export type ReviewQueueData = {
  ok: boolean;
  /** Size of the whole queue, which routinely exceeds `entries.length`. */
  total: number;
  /** How much of the queue came back — the tool caps the request. */
  shown: number;
  entries: ReviewEntry[];
  warnings: string[];
  errors: string[];
};

/** One human decision, as `POST /review/confirm` takes it. */
export type Confirmation = {
  asset_account: string;
  simplefin_id: string;
  account: string;
};

/** Mirrors `ConfirmResult.to_dict()`. */
export type ConfirmResult = {
  ok: boolean;
  confirmed: number;
  learned: number;
  files_written: string[];
  warnings: string[];
  errors: string[];
};

/** Mirrors `EnvelopeBalanceModel`. */
export type EnvelopeBalance = {
  name: string;
  allocated: string;
  spent: string;
  balance: string;
  overspent: boolean;
  /** The overspend as a positive figure; `0` when not overspent. */
  overspend: string;
};

/** Mirrors `EnvelopeReportResponse`. */
export type EnvelopeReportData = {
  asof: string;
  envelopes: EnvelopeBalance[];
  budgeted_cash: string;
  total_envelope_balance: string;
  total_overspend: string;
  /**
   * Cash not committed to any envelope. Withholds overspend rather than
   * netting it, so an envelope in the red cannot inflate this number — see
   * `envelope/compute.py`'s module docstring.
   */
  available: string;
  summary: string;
};

/** One envelope's spend in one period. */
export type SpendingPoint = {
  /** Bucket start, ISO date. Buckets are uniform within a report. */
  period: string;
  envelope: string;
  /** Spend in the period as a positive figure. */
  amount: string;
};

/** Mirrors the spend-by-envelope-over-time report. */
export type SpendingReportData = {
  ok: boolean;
  from: string;
  to: string;
  /** `"month"` today; the axis formats itself from this. */
  granularity: string;
  currency: string;
  /** Every period in the window, including ones with no spend. */
  periods: string[];
  points: SpendingPoint[];
  total: string;
  /**
   * Spend in the window belonging to no envelope. With auto-apply off, this is
   * usually most of it, so the chart states it rather than looking empty for a
   * reason the reader cannot see.
   */
  unmapped_total: string;
  warnings: string[];
  errors: string[];
};

/**
 * One envelope's allocation against its actual spending. Mirrors
 * `BudgetLineModel` in `api.py`.
 *
 * Every field is **server-computed and read verbatim**. The temptation is to
 * derive `status` or a percentage here from `allocated` and `spent` — resist
 * it. Phase 3 fixed a defect where overspent envelopes credited their negative
 * balances back into available cash, and these figures have that fix baked in;
 * a browser-side `Number(spent) / Number(allocated)` would quietly undo it in
 * a float.
 */
export type BudgetLine = {
  name: string;
  allocated: string;
  /** Spend in the window as a positive figure. */
  spent: string;
  /** `allocated - spent`, signed. Negative means more was spent than budgeted. */
  remaining: string;
  /**
   * Fraction of the allocation consumed — `0.8333` is 83.33% — or **`null`
   * when nothing is allocated**.
   *
   * A genuine JSON `number`, not a string, and the asymmetry with the money
   * fields around it is deliberate: a ratio is not money, so it does not need
   * `Decimal` and does not get the string treatment. Everything beside it does.
   *
   * The null is the load-bearing part and must survive to the screen. Spending
   * against a zero allocation has no ratio, and both substitutes are wrong in
   * opposite directions — `0` reads as untouched, `1` as exhausted.
   * `budget-scales.ts` routes those rows to a different mark entirely rather
   * than drawing either.
   *
   * Not clamped at `1`: an envelope at 240% of its allocation reports `2.4`.
   */
  consumed_ratio: number | null;
  /**
   * `within` | `over` | `unbudgeted` | `unused`.
   *
   * The sidecar's verdict, not a threshold applied here. `unbudgeted` is the
   * zero-allocation case and is why `consumed_ratio` is null; `unused` is an
   * allocation nothing was spent against, which is a different thing from
   * being under budget.
   */
  status: string;
  /**
   * Server-computed, so the UI reads a flag rather than deriving one by
   * comparing two decimal strings. A browser cannot do `Decimal` arithmetic
   * and must not try; this mirrors `EnvelopeBalance`, which carries the same
   * pair for the same reason after the Phase 3 `available` fix.
   */
  overspent: boolean;
  /** The overspend as a positive figure; `"0"` when not over. */
  overspend: string;
  /** Balance carried into the window from earlier months. */
  carried_in: string;
  balance: string;
};

/** Mirrors `BudgetReportResponse`. */
export type BudgetReportData = {
  ok: boolean;
  summary: string;
  from: string;
  to: string;
  currency: string;
  envelopes: BudgetLine[];
  total_allocated: string;
  total_spent: string;
  total_remaining: string;
  total_overspend: string;
  /**
   * Spending in the window that belongs to no envelope, and therefore to no
   * budget line. With auto-apply off this is usually most of it, so the chart
   * states it rather than letting every bar read short for an invisible
   * reason.
   */
  unmapped_total: string;
  unmapped_accounts: string[];
  warnings: string[];
  errors: string[];
};

/**
 * One envelope's direction. Mirrors `EnvelopeTrendModel`.
 *
 * `direction` is `up` | `down` | `flat` | **`insufficient_data`**, and the
 * fourth is abstention rather than a fourth magnitude. The sidecar is explicit
 * that it "is *not* `flat`: `flat` means the slope was measured and is small,
 * which is a finding a user acts on differently from 'we don't know yet'". It
 * is a fourth enum value rather than a null `direction` precisely so a client
 * is never asked to infer abstention from an absence.
 *
 * `slope`, `mean` and `relative_slope` come back as the values the verdict was
 * actually read from, so the classification can be checked rather than taken
 * on faith. They are `Decimal` and therefore strings: a statistic derived from
 * money must not be finished in a float in the browser.
 */
export type EnvelopeTrend = {
  name: string;
  direction: string;
  periods_observed: number;
  periods_required: number;
  total: string;
  mean: string;
  slope: string;
  relative_slope: string | null;
  /** Which rule produced the verdict — shown verbatim when it abstained. */
  reason: string;
  points: SpendingPoint[];
};

/**
 * A transaction unusual for its envelope. Mirrors `OutlierModel`.
 *
 * Deliberately self-contained: `median`, `scale` and `threshold` travel with
 * the flag so `(amount - median) / scale` can be recomputed from this one
 * record. An outlier a user cannot interrogate is worse than none — it is an
 * unfalsifiable claim, which §5.3's amendment names as this phase's most
 * likely regression.
 */
export type OutlierTransaction = {
  envelope: string;
  posted_date: string;
  description: string;
  /** Signed asset-side amount, per ingest's convention. */
  amount: string;
  /** How far out, in units of `scale`. */
  score: string;
  median: string;
  scale: string;
  /** How `scale` was derived, e.g. a median absolute deviation. */
  scale_method: string;
  /** The `score` past which a transaction is flagged. */
  threshold: string;
};

/**
 * Whether an envelope was examined for outliers at all. Mirrors
 * `OutlierAssessmentModel`.
 *
 * Present for every envelope so "nothing unusual" stays distinguishable from
 * "not enough data to look". Collapsing the two is how a report ends up
 * asserting the unfalsifiable "nothing looks unusual" — the exact sentence
 * §5.3's amendment left unsolved.
 */
export type OutlierAssessment = {
  envelope: string;
  sample_size: number;
  judged: boolean;
  reason: string;
  median: string | null;
  scale: string | null;
  scale_method: string;
  outliers_found: number;
};

/** Mirrors `TrendsReportResponse`. */
export type TrendsReportData = {
  ok: boolean;
  summary: string;
  from: string;
  to: string;
  currency: string;
  periods: string[];
  envelopes: EnvelopeTrend[];
  outliers: OutlierTransaction[];
  assessments: OutlierAssessment[];
  unmapped_total: string;
  unmapped_accounts: string[];
  unmapped_transactions: number;
  /**
   * The thresholds this run applied, echoed so the response describes its own
   * method. Rendered, not just carried: a finding stated without the rule that
   * produced it is the unfalsifiable claim §5.3 warns about.
   */
  min_periods: number;
  flat_band: string;
  min_transactions: number;
  outlier_threshold: string;
  warnings: string[];
  errors: string[];
};

/**
 * One envelope's month. Mirrors `MonthEndEnvelopeModel`.
 *
 * **`overspent` and `over_budget` are two different failures** and the sidecar
 * keeps them apart, so nothing here may collapse them. Overspent means the
 * running balance went negative — money already spent with nothing behind it.
 * Over budget means this month's spend exceeded this month's allocation, which
 * an envelope carrying a healthy balance can do without ever going negative.
 * Treating either as the other raises a false alarm or hides a real one, and
 * both reach the user as a sentence about their money.
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
  /** Fraction consumed, or null when nothing was allocated. See `BudgetLine`. */
  consumed_ratio: number | null;
  /** `within` | `over` | `unbudgeted` | `unused`. */
  status: string;
  /**
   * `up` | `down` | `flat` | `insufficient_data`, read over a trailing window
   * rather than this month alone. The fourth is abstention, not a magnitude —
   * see `EnvelopeTrend`.
   */
  direction: string;
  direction_reason: string;
  /** The two counts that make an abstention checkable rather than merely stated. */
  periods_observed: number;
  periods_required: number;
  overspent: boolean;
  over_budget: boolean;
};

/**
 * The composite month-end report. Mirrors `MonthEndReportResponse`.
 *
 * Two string enums here are the most important fields on the type, and both
 * exist to stop the card making a claim the ledger does not support.
 *
 * `coverage` — `future` | `in-progress` | `no-data` | `partial` | `complete` —
 * says what period this is actually about. "So far this month" and "for July"
 * are different reports, and rendering them with the same words makes a
 * partial month look like a finished one.
 *
 * `categorization` — `none` | `partial` | `full` | `no-spend` — says how much
 * of the month's spending reached an envelope at all. With auto-apply off that
 * is routinely almost none, so a card that omitted it would be a clean-looking
 * summary of a ledger nobody has finished categorizing.
 */
export type MonthEndReportData = {
  ok: boolean;
  summary: string;
  /** `YYYY-MM`. Echoed so the card cannot mislabel itself. */
  month: string;
  /** The month in words, e.g. `"July 2026"`. Rendered, never parsed. */
  label: string;
  from: string;
  to: string;
  /**
   * What the closing figures were computed at: the month's last day, or today
   * when the month is not over. Never a future date.
   */
  asof: string;
  coverage: string;
  /**
   * The last day the ledger actually has a transaction on, or null for an
   * empty month. This is what makes `"partial"` legible: a month whose data
   * stops on the 12th is not a month of no spending after the 12th, it is a
   * month nobody has synced since.
   */
  data_through: string | null;
  /** The last day this report covers, which for a running month is today. */
  through: string | null;
  /** True once the month is over. Redundant with `coverage`, and cheaper to read. */
  complete: boolean;
  days_elapsed: number;
  days_in_month: number;
  transactions: number;
  /** How many of the month's transactions reached an envelope, and how many did not. */
  categorized_count: number;
  uncategorized_count: number;
  currency: string;
  envelopes: MonthEndEnvelope[];
  opening_total: string;
  allocated_total: string;
  spent_total: string;
  closing_total: string;
  unmapped_total: string;
  unmapped_accounts: string[];
  /**
   * `spent_total + unmapped_total` — the month's actual spending, and the
   * denominator the per-envelope table has to be read against. Summed in the
   * sidecar; adding the two strings here would be the arithmetic this layer
   * exists to keep out.
   */
  total_spend: string;
  categorization: string;
  /** Share of the month's spending that reached an envelope, 0–1, as a string. */
  categorized_share: string;
  budgeted_cash: string;
  available: string;
  total_overspend: string;
  /** Unusual transactions dated within the month, judged against a trailing window. */
  outliers: OutlierTransaction[];
  trend_from: string | null;
  trend_to: string | null;
  /**
   * Envelopes with too little history to judge for outliers. Present so the
   * card can say "nothing unusual among the N we could check" instead of the
   * unfalsifiable "nothing looks unusual".
   */
  unjudged: string[];
  warnings: string[];
  errors: string[];
};

/**
 * `Assert` fails to compile unless its argument is exactly `true`.
 *
 * The constraint is what does the work: an alias that merely *resolves* to an
 * error-shaped type reports nothing, because no value ever depends on it.
 * `extends true` is a constraint the checker must discharge at the point of
 * use, so a broken row below is a build failure.
 */
type Assert<T extends true> = T;
type Satisfies<Port, Prop> = Port extends Prop ? true : false;

/**
 * Each row reads "the port's type supplies everything this file's prop type
 * asks for". A row that goes red means the sidecar contract moved and the
 * component reading that shape is about to receive `undefined` where it
 * expects a field — fix the prop type here, not the component.
 */
export type ContractAssertions = [
  Assert<Satisfies<PortReviewQueue, ReviewQueueData>>,
  Assert<Satisfies<PortEnvelopeReport, EnvelopeReportData>>,
  Assert<Satisfies<PortSpendingReport, SpendingReportData>>,
  Assert<Satisfies<PortMonthEndReport, MonthEndReportData>>,
];

/**
 * `BudgetReportData` and `TrendsReportData` have no rows, and the absence is
 * load-bearing rather than an oversight.
 *
 * `/reports/budget` and `/reports/trends` exist on the sidecar and are fully
 * specified, but `BookkeeperClient` declares no method for either — the tool
 * surface was capped at seven and neither got one, so no port type exists to
 * assert against. A row pointing at `unknown` would pass unconditionally and
 * be worse than nothing, because it would look like coverage.
 *
 * The practical consequence: `BudgetChart` and `TrendCard` are reachable today
 * only through `MonthEndCard`, which feeds them from `MonthEndReport` — and
 * that path is checked, by the row above plus `monthEndBudgetLines`. If either
 * endpoint is later given a tool, add its row here at the same time.
 */
