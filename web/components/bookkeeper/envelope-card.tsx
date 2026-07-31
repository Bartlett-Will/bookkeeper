"use client";

/**
 * Envelope balances: allocated, spent, remaining — and overspend, said out
 * loud.
 *
 * Every figure here is read from the report, never derived. That is a
 * correctness constraint, not a style preference. The sidecar computes these
 * in `Decimal`; this component has only IEEE floats, so any total it worked
 * out itself would eventually disagree with the ledger by a cent. More
 * pointedly, Phase 3 fixed a real defect where overspent envelopes credited
 * their negative balances back into available cash and inflated the headroom
 * the user thought they had. `available` and `total_overspend` already have
 * that fix baked in; recomputing them here would quietly undo it.
 *
 * So: `Number(...)` appears only to decide whether something is negative or
 * how wide a bar should be. Nothing displayed is arithmetic done in the
 * browser.
 */

import { formatAmount, formatDate, pluralize } from "./format";
import type { EnvelopeBalance, EnvelopeReportData } from "./types";

/** Currency the envelope report is denominated in. */
const LEDGER_CURRENCY = "USD";

function isPositive(amount: string): boolean {
  const value = Number(amount);
  return Number.isFinite(value) && value > 0;
}

/**
 * How full the meter reads, 0–1.
 *
 * Presentation only — it decides a bar width and nothing else. An overspent
 * envelope clamps at full rather than overflowing its track; the overspend is
 * carried by the badge and the figure beside it, which say the amount exactly.
 */
function meterFraction(envelope: EnvelopeBalance): number {
  const allocated = Number(envelope.allocated);
  const spent = Number(envelope.spent);
  if (
    !(Number.isFinite(allocated) && Number.isFinite(spent)) ||
    allocated <= 0
  ) {
    return spent > 0 ? 1 : 0;
  }
  return Math.min(1, Math.max(0, spent / allocated));
}

export function EnvelopeCard({ report }: { report: EnvelopeReportData }) {
  const hasOverspend = isPositive(report.total_overspend);
  const overspentCount = report.envelopes.filter(
    (envelope) => envelope.overspent
  ).length;

  return (
    <section className="w-full rounded-xl border border-border/60 bg-card p-4 shadow-[var(--shadow-card)]">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h3 className="font-medium text-[13px] text-foreground">Envelopes</h3>
          <p className="text-[12px] text-muted-foreground">
            as of {formatDate(report.asof)} ·{" "}
            {pluralize(report.envelopes.length, "envelope")}
          </p>
        </div>
        <div className="text-right">
          <p className="font-semibold text-[20px] text-foreground leading-tight">
            {formatAmount(report.available, LEDGER_CURRENCY)}
          </p>
          <p className="text-[12px] text-muted-foreground">
            available to allocate
          </p>
        </div>
      </header>

      {/*
        Stated before the list, because it is the number a reader is most
        likely to be wrong about. `available` above already withholds this —
        it is not headroom, and the two figures side by side are what make
        that legible.
      */}
      {hasOverspend && (
        <p className="mb-3 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-md bg-destructive/10 px-2.5 py-2 text-[12px] text-destructive">
          <OverspentIcon />
          <span className="font-medium">
            {formatAmount(report.total_overspend, LEDGER_CURRENCY)} overspent
          </span>
          <span className="text-destructive/80">
            across {pluralize(overspentCount, "envelope")}. It is held back from
            available cash, not netted against it.
          </span>
        </p>
      )}

      {report.envelopes.length === 0 ? (
        <p className="py-6 text-center text-[13px] text-muted-foreground">
          No envelopes are defined yet.
        </p>
      ) : (
        <ul className="space-y-2.5">
          {report.envelopes.map((envelope) => (
            <EnvelopeRow envelope={envelope} key={envelope.name} />
          ))}
        </ul>
      )}

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 border-border/60 border-t pt-3 text-[12px] sm:grid-cols-3">
        <SummaryFigure label="Budgeted cash" value={report.budgeted_cash} />
        <SummaryFigure
          label="In envelopes"
          value={report.total_envelope_balance}
        />
        <SummaryFigure
          label="Overspent"
          tone={hasOverspend ? "bad" : "neutral"}
          value={report.total_overspend}
        />
      </dl>
    </section>
  );
}

function SummaryFigure({
  label,
  tone = "neutral",
  value,
}: {
  label: string;
  tone?: "neutral" | "bad";
  value: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 sm:flex-col sm:items-start sm:gap-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={
          tone === "bad"
            ? "font-medium tabular-nums text-destructive"
            : "tabular-nums text-foreground"
        }
      >
        {formatAmount(value, LEDGER_CURRENCY)}
      </dd>
    </div>
  );
}

function EnvelopeRow({ envelope }: { envelope: EnvelopeBalance }) {
  const fraction = meterFraction(envelope);
  const { overspent } = envelope;

  return (
    <li>
      <div className="flex items-baseline justify-between gap-3">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate font-medium text-[13px] text-foreground">
            {envelope.name}
          </span>
          {/*
            Icon and label, not colour alone: the red bar is the glance, but a
            reader who cannot separate red from grey still gets the word.
          */}
          {overspent ? (
            <span className="flex shrink-0 items-center gap-1 rounded bg-destructive/10 px-1.5 py-0.5 font-medium text-[11px] text-destructive">
              <OverspentIcon />
              over by {formatAmount(envelope.overspend, LEDGER_CURRENCY)}
            </span>
          ) : null}
        </span>
        <span
          className={
            overspent
              ? "shrink-0 font-medium tabular-nums text-[13px] text-destructive"
              : "shrink-0 tabular-nums text-[13px] text-foreground"
          }
        >
          {formatAmount(envelope.balance, LEDGER_CURRENCY)}
        </span>
      </div>

      <div
        aria-hidden="true"
        className={
          overspent
            ? "mt-1 h-1.5 w-full overflow-hidden rounded-full bg-destructive/15"
            : "mt-1 h-1.5 w-full overflow-hidden rounded-full bg-primary/15"
        }
      >
        <div
          className={
            overspent
              ? "h-full rounded-full bg-destructive"
              : "h-full rounded-full bg-primary"
          }
          style={{ width: `${fraction * 100}%` }}
        />
      </div>

      <p className="mt-1 text-[12px] text-muted-foreground tabular-nums">
        {formatAmount(envelope.spent, LEDGER_CURRENCY)} spent of{" "}
        {formatAmount(envelope.allocated, LEDGER_CURRENCY)} allocated
      </p>
    </li>
  );
}

function OverspentIcon() {
  return (
    <svg
      aria-hidden="true"
      className="size-3 shrink-0"
      fill="none"
      viewBox="0 0 12 12"
    >
      <path
        d="M6 1.5 11 10.5H1L6 1.5Z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.2"
      />
      <path
        d="M6 5v2.2"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.2"
      />
      <circle cx="6" cy="9" fill="currentColor" r="0.6" />
    </svg>
  );
}
