/**
 * The shared surface a bookkeeper report sits on.
 *
 * Two variants, one set of contents. A report tool called directly in chat
 * renders as `"card"` — its own bordered surface, because §5.3's amendment
 * measured the blank-bubble case as unconditional and the card is the entire
 * reply. The same report composed into the month-end summary renders as
 * `"section"`, dropping the border, padding and shadow so a card does not nest
 * inside a card.
 *
 * Only chrome varies. Every warning, caveat and figure is identical in both,
 * which is the point of keeping this a presentation switch rather than a
 * "compact mode": an embedded budget chart that quietly dropped its
 * unmapped-spend notice would reintroduce, one layer down, exactly the
 * omission those notices exist to prevent.
 */

export type ReportVariant = "card" | "section";

export const CARD_CHROME =
  "w-full rounded-xl border border-border/60 bg-card p-4 shadow-[var(--shadow-card)]";

export function chromeFor(variant: ReportVariant): string {
  return variant === "card" ? CARD_CHROME : "w-full";
}
