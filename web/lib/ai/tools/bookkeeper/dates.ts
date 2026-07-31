// Date handling for the tool layer.
//
// The sidecar speaks ISO `YYYY-MM-DD` everywhere (see `coerce_asof` in
// envelope/compute.py). An 8B model asked for a date range will sometimes
// produce "last month", "2026-13-01", or a plausible-looking date that does
// not exist. None of those may reach the ledger, so they are rejected here
// rather than turned into a 422 the model then has to interpret.

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

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
