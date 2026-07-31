import { tool } from "ai";
import { z } from "zod";
import type { BookkeeperClient, SpendingReport } from "./client";
import { daysAgo, isIsoDate, today } from "./dates";
import {
  type BookkeeperToolResult,
  failed,
  ok,
  renderedByTheUi,
} from "./result";

export type GetSpendingReportResult = BookkeeperToolResult<
  "spending",
  SpendingReport
>;

/** Default window when the user names no period. Long enough to show a trend. */
const DEFAULT_WINDOW_DAYS = 90;

/**
 * Spend per envelope over a date range (PLAN.md §6, Phase 4; Phase 5 deepens
 * the analysis and the charting).
 *
 * Both dates are **optional on purpose.** Requiring them would put date
 * arithmetic — "last month", "so far this year" — on the model, and §3.3 is
 * clear that inventing a plausible-looking wrong value is exactly what an 8B
 * does under that pressure. Omitting both is always available and always
 * correct, so the failure mode is a slightly wrong window rather than a
 * fabricated one.
 */
export function getSpendingReport(client: BookkeeperClient) {
  return tool({
    description:
      "Show how much was spent in each budget envelope over a period of time, as a chart. Use for questions about spending across a past period, where the money went, or comparing one stretch of time with another. If the user named a period, pass it as from and to in YYYY-MM-DD form; if they did not, omit both and the last three months are used.",
    execute: async ({ from, to }): Promise<GetSpendingReportResult> => {
      const now = new Date();
      const start = from ?? daysAgo(DEFAULT_WINDOW_DAYS, now);
      const end = to ?? today(now);

      for (const [label, value] of [
        ["from", start],
        ["to", end],
      ] as const) {
        if (!isIsoDate(value)) {
          return failed(
            "spending",
            `"${value}" is not a real calendar date in YYYY-MM-DD form (${label}).`
          );
        }
      }
      if (start > end) {
        // Safe as a string comparison: ISO dates sort lexicographically.
        return failed(
          "spending",
          `the range starts (${start}) after it ends (${end}).`
        );
      }

      const result = await client.getSpendingReport({ from: start, to: end });
      if (!result.ok) {
        return failed("spending", result.error);
      }
      return ok("spending", result.data);
    },
    inputSchema: z.object({
      from: z
        .string()
        .optional()
        .describe(
          "First day of the period, YYYY-MM-DD. Omit unless the user named a start."
        ),
      to: z
        .string()
        .optional()
        .describe(
          "Last day of the period, YYYY-MM-DD. Omit unless the user named an end."
        ),
    }),
    toModelOutput: renderedByTheUi("The spending report has been displayed."),
  });
}
