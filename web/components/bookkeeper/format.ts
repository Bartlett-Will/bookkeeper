/**
 * Display formatting for ledger values. Pure, so it is testable without a DOM.
 *
 * Every function here is one-way: it turns sidecar data into strings for the
 * screen. Nothing formatted here is ever parsed back and sent to the sidecar —
 * the components submit the original decimal strings and account names
 * untouched. That is the reason the `Number()` calls below are safe despite the
 * ledger's decimal discipline: a float is good enough to *show* $19.96 and
 * would not be good enough to *store* it.
 */

/** Cascade tier ids from `categorize/models.py`, plus the human decision. */
const TIER_LABELS: Record<string, string> = {
  human: "confirmed",
  llm: "model",
  mcc: "merchant code",
  memory: "seen before",
  rule: "rule",
  statistical: "statistical",
};

/**
 * How much a suggestion should be trusted at a glance.
 *
 * Deliberately coarse. §5.5 established that a model's self-reported
 * confidence is not a probability, so rendering "0.82" as though it were one
 * invites a precision the number does not have; three buckets say what the
 * reviewer actually needs to decide — read this one, skim this one, check this
 * one.
 */
export type ConfidenceBand = "high" | "medium" | "low" | "none";

export const HIGH_CONFIDENCE = 0.9;
export const MEDIUM_CONFIDENCE = 0.6;

export function confidenceBand(confidence: number | null): ConfidenceBand {
  if (confidence === null) {
    return "none";
  }
  if (confidence >= HIGH_CONFIDENCE) {
    return "high";
  }
  if (confidence >= MEDIUM_CONFIDENCE) {
    return "medium";
  }
  return "low";
}

export function formatConfidence(confidence: number | null): string {
  if (confidence === null) {
    return "—";
  }
  return `${Math.round(confidence * 100)}%`;
}

export function formatTier(tier: string | null): string {
  if (!tier) {
    return "no tier answered";
  }
  return TIER_LABELS[tier] ?? tier;
}

/**
 * A signed amount as currency, with the sign kept.
 *
 * The sign is the asset side's: negative is money leaving. It is preserved
 * rather than turned into a "spent"/"received" word because the reviewer is
 * reconciling against a bank statement, where the sign is what they will see.
 */
export function formatAmount(amount: string, currency: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) {
    return `${amount} ${currency}`;
  }
  try {
    return new Intl.NumberFormat(undefined, {
      currency,
      style: "currency",
    }).format(value);
  } catch {
    // An unrecognized currency code throws rather than degrading, and a
    // ledger with an unusual commodity should still render its numbers.
    return `${value.toFixed(2)} ${currency}`;
  }
}

/** The same, without a sign — for figures whose direction is already named. */
export function formatMagnitude(amount: string, currency: string): string {
  return formatAmount(amount.replace(/^-/, ""), currency);
}

export function isNegative(amount: string): boolean {
  return Number(amount) < 0;
}

/** `Expenses:Food:Groceries` → `Groceries`. */
export function accountLeaf(account: string): string {
  const parts = account.split(":");
  return parts.at(-1) ?? account;
}

/** `Expenses:Food:Groceries` → `Expenses:Food`. Empty for a top-level account. */
export function accountParent(account: string): string {
  const parts = account.split(":");
  return parts.slice(0, -1).join(":");
}

/**
 * An ISO date as `3 May` / `3 May 2025`.
 *
 * The year is dropped inside the current year, because a review queue is
 * overwhelmingly recent and repeating "2026" on 332 rows is noise that
 * crowds out the description.
 */
export function formatDate(iso: string, now: Date = new Date()): string {
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  const sameYear = parsed.getFullYear() === now.getFullYear();
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(parsed);
}

/**
 * A month bucket label: `May 2026`.
 *
 * Accepts both `2026-05` and `2026-05-01`. The spending report labels its
 * buckets `YYYY-MM`, which `new Date("2026-05T00:00:00")` rejects outright, so
 * a day is supplied when one is missing rather than falling through to the raw
 * string and printing `2026-05` on the axis.
 */
export function formatMonth(iso: string): string {
  const withDay = /^\d{4}-\d{2}$/.test(iso) ? `${iso}-01` : iso;
  const parsed = new Date(`${withDay}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    year: "numeric",
  }).format(parsed);
}

/** `1` → `1 transaction`; `332` → `332 transactions`. */
export function pluralize(count: number, singular: string, plural?: string) {
  return `${count.toLocaleString()} ${count === 1 ? singular : (plural ?? `${singular}s`)}`;
}

/** Sum decimal strings for display totals, keeping two-decimal precision. */
export function sumAmounts(amounts: string[]): string {
  // Summed in integer cents rather than floats: a group header adding up 47
  // rows is exactly where 0.1 + 0.2 shows itself, and a total that disagrees
  // with its own rows by a cent reads as a bug in the ledger.
  const cents = amounts.reduce((total, amount) => {
    const value = Number(amount);
    return total + (Number.isFinite(value) ? Math.round(value * 100) : 0);
  }, 0);
  return (cents / 100).toFixed(2);
}
