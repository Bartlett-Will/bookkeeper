// Date handling for the tool layer.
//
// The sidecar speaks ISO `YYYY-MM-DD` everywhere (see `coerce_asof` in
// envelope/compute.py). An 8B model asked for a date range will sometimes
// produce "last month", "2026-13-01", or a plausible-looking date that does
// not exist. None of those may reach the ledger, so they are rejected here
// rather than turned into a 422 the model then has to interpret.

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_MONTH = /^\d{4}-\d{2}$/;

/**
 * True only for a well-formed ISO date that names a real calendar day.
 *
 * The round-trip through `Date` is the part that matters: `2026-02-30` passes
 * the regex and parses without throwing, but normalises to March 2nd, so
 * comparing the formatted result back to the input is what actually rejects
 * it.
 */
export function isIsoDate(value: string): boolean {
  if (!ISO_DATE.test(value)) {
    return false;
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) {
    return false;
  }
  return parsed.toISOString().slice(0, 10) === value;
}

export function toIsoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function today(now: Date = new Date()): string {
  return toIsoDate(now);
}

export function daysAgo(days: number, now: Date = new Date()): string {
  const then = new Date(now.getTime());
  then.setUTCDate(then.getUTCDate() - days);
  return toIsoDate(then);
}

/**
 * True only for `YYYY-MM` naming a month that exists.
 *
 * Separate from `isIsoDate` rather than derived from it, because the failure it
 * catches is different: `get_month_end_report` takes a *month*, and the mistake
 * an 8B makes there is not an impossible day but an impossible month —
 * `2026-13` for "the thirteenth month" and `2026-00` for "the month before
 * January" are both things a model produces when it does the arithmetic itself.
 * The regex alone accepts both.
 */
export function isIsoMonth(value: string): boolean {
  if (!ISO_MONTH.test(value)) {
    return false;
  }
  const month = Number(value.slice(5));
  return month >= 1 && month <= 12;
}
