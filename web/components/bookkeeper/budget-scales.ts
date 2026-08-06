/**
 * Geometry for the budget-vs-actual comparison. Pure, DOM-free, tested as
 * arithmetic.
 *
 * A bullet row per envelope: one bar for spend, one rule at the allocation.
 * Two states drive every decision here, because both are states the sidecar
 * reports honestly and a careless chart flattens.
 *
 * **Overspend overflows; it does not clamp.** `envelope-card.tsx`'s meter
 * clamps at full and can afford to, because the badge beside it names the
 * overspend in words. A budget-vs-actual chart cannot: a bar pinned at the end
 * of its track *is* the reading "100% consumed", and Phase 3 fixed a real
 * defect where overspent envelopes silently inflated available headroom.
 * Redrawing that defect as a full bar would reintroduce it visually for a
 * reader who trusts the picture over the footnote. So the axis stretches past
 * the allocation and the bar crosses it, in a different colour.
 *
 * **A zero allocation has no ratio.** The sidecar sends
 * `consumed_ratio: null` and `status: "unbudgeted"` for it, with the reason
 * spelled out in `budget.py`: 0 reads as untouched and 1 as exhausted, and a
 * zero budget supports neither claim. Those rows get no bar at all — a
 * proportion needs a denominator, and inventing one is the lie.
 *
 * **No money is touched here at all.** `consumed_ratio` is a genuine JSON
 * number rather than a `Decimal` string, and that asymmetry is the sidecar's:
 * a ratio is not money. The amounts beside it — `overspend`, `remaining`,
 * `allocated`, `spent` — stay strings and are never parsed in this module, and
 * `overspent` and `status` are read as flags rather than derived by comparing
 * two decimal strings, which a browser cannot do correctly.
 */

import type { BudgetLine, MonthEndEnvelope } from "./types";

/**
 * How far past the allocation the axis will stretch, as a multiple of it.
 *
 * Unbounded would let one envelope at 30x compress every other row — and the
 * allocation rule with them — into the leftmost slice of the track, destroying
 * the comparison for the rows a reader can still act on. Rows past the cap are
 * marked `clipped` and drawn with an open end, which reads as "continues"
 * rather than "ends exactly here". The precise figure is always printed, so
 * clipping costs presentation, never information.
 */
export const RATIO_DOMAIN_CAP = 2;

/** Where the allocation sits on the axis: a ratio of 1. */
export const FULL_RATIO = 1;

/**
 * The sidecar's verdicts, as `budget.py` defines them.
 *
 * Kept as a union of the known values with unknown strings passing through
 * untouched. An unrecognised status is not coerced to `within`: that would be
 * this layer inventing a verdict, and the badge simply stays off instead.
 */
export type BudgetStatus = "within" | "over" | "unbudgeted" | "unused";

export type BudgetRowKind = "measured" | "unallocated";

export type BudgetRow = {
  name: string;
  line: BudgetLine;
  /**
   * `"unallocated"` when the sidecar sent no ratio. These rows carry no bar:
   * there is no denominator to draw one against.
   */
  kind: BudgetRowKind;
  /** The sanitised ratio, or null. `0.8333` means 83.33%. */
  ratio: number | null;
  /** Track fraction up to the allocation. Zero for unallocated rows. */
  withinFraction: number;
  /**
   * Track fraction *beyond* the allocation — drawn as its own segment in the
   * status colour, so crossing the line reads as a change of material and not
   * only as a longer bar.
   */
  overFraction: number;
  /** True when the bar ran past the cap and is drawn open-ended. */
  clipped: boolean;
  /**
   * `status === "over"`: this window's spend exceeded this window's
   * allocation. **Not** `line.overspent`, which is the different and
   * independent failure of a negative running balance — see `budgetRow`.
   */
  isOver: boolean;
  /** An allocation nothing was spent against — different from being under it. */
  isUnused: boolean;
};

export type BudgetLayout = {
  rows: BudgetRow[];
  /** Upper bound of the shared axis, as a ratio; at least 1. */
  domainMax: number;
  /** Where the allocation rule sits, as a fraction of the track. */
  allocationFraction: number;
  unallocatedCount: number;
  overCount: number;
  isEmpty: boolean;
};

/**
 * A consumed ratio as a finite, non-negative number — or null.
 *
 * Null in, null out: the absence is the signal and must not become a zero on
 * its way through. A NaN or negative is also null rather than zero, because
 * "we could not read this" is much closer to "no ratio" than to "untouched" —
 * and a NaN would propagate into every downstream coordinate and blank the
 * row.
 */
export function cleanRatio(ratio: number | null): number | null {
  if (ratio === null || !Number.isFinite(ratio) || ratio < 0) {
    return null;
  }
  return ratio;
}

/**
 * The shared axis bound across every row.
 *
 * Shared rather than per-row on purpose: if each row normalized to its own
 * allocation, the allocation rule would sit at a different x in every row and
 * the eye could no longer sweep down the chart to find what crossed it. One
 * bound means one vertical line, and "past the line" means the same thing
 * everywhere.
 */
export function budgetDomainMax(lines: BudgetLine[]): number {
  let max = FULL_RATIO;
  for (const line of lines) {
    const ratio = cleanRatio(line.consumed_ratio);
    if (ratio !== null && ratio > max) {
      max = ratio;
    }
  }
  return Math.min(RATIO_DOMAIN_CAP, max);
}

export function budgetRow(line: BudgetLine, domainMax: number): BudgetRow {
  const ratio = cleanRatio(line.consumed_ratio);
  /*
   * `status`, and **not** `overspent`.
   *
   * These are two different failures and the sidecar keeps them apart:
   * `status === "over"` means this window's spend exceeded this window's
   * allocation, while `overspent` means the running balance went negative —
   * money already gone, possibly from months ago. An envelope can be either
   * without being the other.
   *
   * Collapsing them was a real bug here, caught against live data: Transport
   * came back `overspent: true` with `status: "within"` at 24% of its
   * allocation, and reading the flag painted a comfortably-under bar red and
   * badged it "over by $13.00". That is the false alarm `api.py` warns about,
   * and it would have been invisible in any fixture that set both together.
   *
   * The balance overspend is not dropped — it is `EnvelopeCard`'s to report,
   * which the month-end card renders directly above this one.
   */
  const isOver = line.status === "over";
  const isUnused = line.status === "unused";

  if (ratio === null) {
    return {
      clipped: false,
      isOver,
      isUnused,
      kind: "unallocated",
      line,
      name: line.name,
      overFraction: 0,
      ratio: null,
      withinFraction: 0,
    };
  }

  const bound = Math.max(FULL_RATIO, domainMax);
  const drawn = Math.min(ratio, bound);
  return {
    clipped: ratio > bound,
    isOver,
    isUnused,
    kind: "measured",
    line,
    name: line.name,
    overFraction: Math.max(0, drawn - FULL_RATIO) / bound,
    ratio,
    withinFraction: Math.min(drawn, FULL_RATIO) / bound,
  };
}

/**
 * Order rows so the ones a reader must act on come first.
 *
 * Over budget first, then spending with nothing allocated, then the rest by
 * how consumed they are. Alphabetical would be defensible for a lookup table;
 * this is a report, and burying an over-budget envelope at "W" is how a chart
 * ends up technically complete and practically useless. Ties break on name, so
 * the order is stable across polls and the list does not repaint itself.
 */
function rowRank(row: BudgetRow): number {
  if (row.isOver) {
    return 0;
  }
  if (row.kind === "unallocated") {
    return 1;
  }
  return 2;
}

export function buildBudgetLayout(lines: BudgetLine[]): BudgetLayout {
  const domainMax = budgetDomainMax(lines);
  const rows = lines
    .map((line) => budgetRow(line, domainMax))
    .sort(
      (a, b) =>
        rowRank(a) - rowRank(b) ||
        (b.ratio ?? 0) - (a.ratio ?? 0) ||
        a.name.localeCompare(b.name)
    );

  return {
    allocationFraction: FULL_RATIO / Math.max(FULL_RATIO, domainMax),
    domainMax,
    isEmpty: rows.length === 0,
    overCount: rows.filter((row) => row.isOver).length,
    rows,
    unallocatedCount: rows.filter((row) => row.kind === "unallocated").length,
  };
}

/**
 * A month-end envelope, read as a budget line.
 *
 * The month-end report carries its own per-envelope budget figures rather than
 * embedding a `/reports/budget` response, and the two shapes differ only in
 * naming — `opening_balance`/`closing_balance` where the budget report says
 * `carried_in`/`balance`. Mapping here lets the month-end card render through
 * exactly the same layout and the same component, which is the point: a second
 * implementation of "how do we draw an overspend" is a second chance to draw
 * it as a clamped full bar.
 *
 * `over_budget` is deliberately **not** consulted. `status` is the sidecar's
 * verdict on this window's allocation, which is what the bar is about;
 * `overspent` — a negative running balance — is a different failure and the
 * card reports it separately rather than letting it colour a budget bar.
 */
export function monthEndBudgetLines(
  envelopes: MonthEndEnvelope[]
): BudgetLine[] {
  return envelopes.map((envelope) => ({
    allocated: envelope.allocated,
    balance: envelope.closing_balance,
    carried_in: envelope.opening_balance,
    consumed_ratio: envelope.consumed_ratio,
    name: envelope.name,
    overspend: envelope.overspend,
    overspent: envelope.overspent,
    remaining: envelope.remaining,
    spent: envelope.spent,
    status: envelope.status,
  }));
}

/**
 * A consumed ratio as a percentage, for display.
 *
 * Returns `null` rather than a string when there is no ratio, so a caller
 * cannot print "0%" for "nothing allocated" by accident. Making the absence a
 * different *type* is what stops it being rendered as a number by mistake —
 * the check is the compiler's, not the reviewer's.
 */
export function formatConsumed(ratio: number | null): string | null {
  const value = cleanRatio(ratio);
  if (value === null) {
    return null;
  }
  return `${Math.round(value * 100)}%`;
}
