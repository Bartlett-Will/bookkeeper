import { tool } from "ai";
import { z } from "zod";
import type { BookkeeperClient, MonthEndReport } from "./client";
import { isIsoMonth } from "./dates";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type GetMonthEndReportResult = BookkeeperToolResult<
  "month-end",
  MonthEndReport
>;

/**
 * The seventh tool, and the only one Phase 5 adds.
 *
 * It earns the slot by answering a question none of the other six can: "how did
 * July go" wants envelope balances, budget-versus-actual, what moved, and what
 * is still uncategorized — all four, for one named month. The alternative is
 * the model calling four tools and stitching the answers together, which is
 * exactly the multi-step chaining PLAN.md §3.3 measures an 8B failing at — and
 * §5.3's whole design is one tool, one step, one card. A composite in the
 * sidecar turns four chances to go wrong into one call.
 *
 * The cost is real and was measured, not assumed: every tool added widens the
 * "whether and which" decision small models are worst at, and this one sits
 * between two existing tools rather than off on its own. See
 * `scripts/measure-tool-selection.ts` for the seven-tool numbers.
 *
 * `month` is optional for the same reason `get_spending_report`'s dates are —
 * omitting it is always available and always correct — but the default applied
 * is the *sidecar's*, and that is load-bearing rather than tidy. The sidecar
 * defaults to the month of the ledger's last transaction, which this side
 * cannot compute because it depends on the ledger. A wall-clock default here
 * would answer "the month that just ended" where the sidecar answers "the last
 * month you have data for", and on a ledger nobody has synced this week those
 * are different months — the second is the one the user meant.
 *
 * A caution about the evidence, since an earlier version of this comment got it
 * wrong: without a date in its prompt `qwen3:8b` supplies the year from its
 * training data, answering "how did July go" with `2023-07`. That is real but
 * it is **not** a production failure — it was an artifact of
 * `measure-tool-selection.ts` sending `regularPrompt` alone while the route
 * sends `systemPrompt`, which appends the date. Measured both ways on
 * 2026-08-06: bare prompt gives `2023-07`, production prompt gives `2026-07`.
 * The harness now composes the prompt the route does. So the argument for
 * forwarding an absent month is the ledger-relative default above, not a
 * hallucinated year.
 */
export function getMonthEndReport(client: BookkeeperClient) {
  return tool({
    description:
      "Close out one named calendar month and show it as a report: how each envelope finished that month, what was budgeted against what was actually spent, which categories moved up or down, and what is still uncategorized. Use when the user asks how a particular month went, or asks to wrap up, close out, or summarise a month by name. Covers one whole month and nothing else — it is not for an arbitrary date range or a comparison across several months, and not for where things stand today.",
    execute: async ({ month }): Promise<GetMonthEndReportResult> => {
      // Validated here rather than forwarded, even though the sidecar answers
      // 422 for an unparseable month: a 422 costs a round trip and arrives as
      // an error the model then has to interpret, which §3.3 names as the
      // cause of invocation loops. `2026-13` is caught before it leaves.
      if (month !== undefined && !isIsoMonth(month)) {
        return failed(
          "month-end",
          `"${month}" is not a real calendar month in YYYY-MM form.`
        );
      }

      const result = await client.getMonthEndReport({ month });
      if (!result.ok) {
        return failed("month-end", result.error);
      }
      return ok("month-end", result.data);
    },
    inputSchema: z.object({
      month: z
        .string()
        .optional()
        .describe(
          "The month to close out, as YYYY-MM — for example 2026-06 for June 2026. Omit this unless the user named a specific month; the most recent month the ledger has data for is then used."
        ),
    }),
    toModelOutput: renderedByTheUi(
      "The month-end report has been displayed.",
      sayableFacts
    ),
  });
}

/**
 * The short, true things the model is given so that it has something to say.
 *
 * This is the Phase 5 answer to the residue §5.3 rule 1's amendment left open:
 * suppressing numbers did not suppress *claims*, and "you're on track" is one
 * sentence, unfalsifiable from the model's position, and actionable. The
 * mechanism is the same one that fixed the fabrication — supply, not
 * prohibition. Adding "do not say whether they are on track" is the move that
 * produced invented dollar amounts in the first place.
 *
 * **Every sentence is read off an enum or a boolean the sidecar computed in
 * `Decimal`.** Nothing here compares, sums, or thresholds an amount. That is
 * the line, and it is why `overspent` is quoted rather than derived: the
 * sidecar reached that verdict against the running balance, whereas comparing
 * `spent` to `allocated` on two strings in a chat server would be a financial
 * judgement made in the wrong place, on the wrong type.
 *
 * Envelopes are deliberately unnamed — "one or more envelopes", not
 * "Groceries". A named envelope invites a sentence about that envelope, and
 * the card is already showing which one.
 */
function sayableFacts(data: MonthEndReport): readonly string[] {
  // Read defensively even though `toMonthEndReport` guarantees these. This
  // runs inside `toModelOutput`, mid-stream, on whatever the tool produced —
  // and a `TypeError` there does not degrade the reply, it ends the turn.
  // Missing data must cost the model a sentence, never the user their answer.
  const envelopes = Array.isArray(data.envelopes) ? data.envelopes : [];
  const outliers = Array.isArray(data.outliers) ? data.outliers : [];
  // Absent means "nothing was examined", not "everything was" — same reading
  // as the normalizer's, and for the same reason: it is the one that withholds
  // a reassurance rather than manufacturing one.
  const unjudged = Array.isArray(data.unjudged) ? data.unjudged : envelopes;

  // A month that has not started, or that the ledger knows nothing about, gets
  // the coverage sentence and nothing else. The later facts would all be
  // vacuously true of an empty month — "no envelope ended overspent" is a
  // reassuring thing to say about a month with no data in it, and reassurance
  // derived from absence is the exact failure this function exists to prevent.
  if (data.coverage === "future") {
    return ["That month has not started yet, so there is nothing to report."];
  }
  if (data.coverage === "no-data") {
    return ["The ledger has no transactions recorded in that month at all."];
  }

  const facts: string[] = [];

  if (data.coverage === "in-progress") {
    facts.push(
      "That month is not over, so these are running figures rather than a final month."
    );
  } else if (data.coverage === "partial") {
    facts.push(
      "The ledger's data for that month stops part-way through, so the month is not fully recorded."
    );
  }

  // The most important sentence in the set. Auto-apply is off (Phase 3), so
  // most spend is still `Expenses:Unknown` and the per-envelope table describes
  // a fraction of the month. A model that did not know this would summarise a
  // largely uncategorized month as a tidy one.
  if (data.categorization === "none") {
    facts.push(
      "None of that month's spending has been filed to an envelope yet, so the per-envelope figures do not describe the month."
    );
  } else if (data.categorization === "partial") {
    facts.push(
      "Only part of that month's spending has been filed to an envelope, so the per-envelope figures are incomplete."
    );
  } else if (data.categorization === "no-spend") {
    facts.push("Nothing was spent in that month.");
  }

  // `overspent` and `over_budget` are different failures and the sidecar keeps
  // them apart, so this reports the more serious one without implying the
  // other is absent.
  if (envelopes.some((envelope) => envelope.overspent)) {
    facts.push("One or more envelopes ended that month overspent.");
  } else if (envelopes.some((envelope) => envelope.over_budget)) {
    facts.push(
      "No envelope ended that month overspent, but one or more spent more than was allocated to it."
    );
  } else if (envelopes.length > 0) {
    // Only sayable when there were envelopes to judge. An empty list is "no
    // envelopes exist", not "every envelope is fine", and the two must not
    // produce the same sentence.
    facts.push("No envelope ended that month overspent or over budget.");
  }

  // "Nothing looks unusual" is the sentence §5.3's amendment singles out, and
  // the reason it is dangerous is that an empty `outliers` array has two
  // meanings: nothing was found, or nothing was looked at. The sidecar
  // separates them — `unjudged` lists the envelopes it declined to examine for
  // want of history — so the reassuring reading is only reachable when
  // something was actually examined. On today's ledger every envelope is
  // unjudged, so this stays silent, which is the correct answer.
  if (outliers.length > 0) {
    facts.push(
      "At least one transaction that month was flagged as unusually large for its envelope."
    );
  } else if (unjudged.length < envelopes.length) {
    facts.push(
      "No transaction that month was flagged as unusually large, though not every envelope had enough history to judge."
    );
  }

  return facts;
}
