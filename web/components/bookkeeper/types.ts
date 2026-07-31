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
];
