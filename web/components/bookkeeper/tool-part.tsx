"use client";

/**
 * Renders a bookkeeper tool result inside a chat message.
 *
 * **The blank-bubble case is the normal case.** PLAN.md §5.3's amendment
 * measured it as unconditional: a successful tool call comes back with empty
 * `content` and `finish_reason: tool_calls`, so there is no prose beside the
 * card and there is not going to be. Every tool here also carries a
 * `toModelOutput` that strips the figures before the model ever sees them, so
 * the model *cannot* narrate the result even when it tries. The card must
 * therefore stand alone and read as finished — a spinner that never resolves,
 * or an empty bordered box, is the failure this component exists to avoid.
 *
 * **The dispatch is structural, not nominal.** These tools are registered in
 * `lib/types.ts` by a different part of the phase, so `part.type` may not be
 * in `ChatTools`' union yet and comparing it to a literal would be a type
 * error today and silently correct tomorrow. Narrowing on the shape works
 * either way — and it earns its keep regardless, because a tool result arrives
 * over a stream: validating that `output.kind` is one we render is a real
 * check, where a cast would only be a promise.
 */

import { EnvelopeCard } from "./envelope-card";
import { ReportChart } from "./report-chart";
import { ReviewCard } from "./review-card";
import type {
  EnvelopeReportData,
  ReviewQueueData,
  SpendingReportData,
} from "./types";

/** The tool names of PLAN.md §5.3 whose results this file renders. */
const RENDERED_TOOLS = new Set([
  "tool-get_review_queue",
  "tool-get_envelope_status",
  "tool-get_spending_report",
]);

/** `kind` discriminants set by `lib/ai/tools/bookkeeper/result.ts`. */
type ResultKind = "review" | "envelopes" | "spending";

const RENDERED_KINDS = new Set<string>(["review", "envelopes", "spending"]);

type ToolPartLike = {
  type: string;
  toolCallId?: string;
  state?: string;
  output?: unknown;
  errorText?: string;
};

/**
 * Whether this part is a bookkeeper tool part this file knows how to draw.
 *
 * The other three tools of §5.3 — `sync_accounts`, `search_transactions`,
 * `allocate_to_envelope` — are deliberately not claimed here. Returning false
 * leaves their branch to whoever owns those cards rather than rendering a
 * placeholder over the top of it.
 */
export function isBookkeeperToolPart(part: { type: string }): boolean {
  return RENDERED_TOOLS.has(part.type);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

export function BookkeeperToolPart({ part }: { part: { type: string } }) {
  const typed = part as ToolPartLike;
  const { state } = typed;

  if (state === "output-error") {
    return (
      <ToolError
        message={typed.errorText ?? "The tool call failed."}
        tool={typed.type}
      />
    );
  }

  if (state !== "output-available") {
    return <ToolLoading tool={typed.type} />;
  }

  const output = asRecord(typed.output);
  if (!output) {
    return (
      <ToolError
        message="The ledger service returned something this card could not read."
        tool={typed.type}
      />
    );
  }

  // Every bookkeeper tool returns errors as values rather than throwing, so a
  // reachable-but-unhappy sidecar arrives here, not in `output-error`.
  if (output.status === "error") {
    return (
      <ToolError
        message={
          typeof output.message === "string"
            ? output.message
            : "The ledger service could not answer."
        }
        tool={typed.type}
      />
    );
  }

  const kind = typeof output.kind === "string" ? output.kind : "";
  const data = asRecord(output.data);
  if (!(RENDERED_KINDS.has(kind) && data)) {
    return (
      <ToolError
        message="The ledger service returned a result this card does not know how to show."
        tool={typed.type}
      />
    );
  }

  switch (kind as ResultKind) {
    case "review":
      return <ReviewCard queue={data as unknown as ReviewQueueData} />;
    case "envelopes":
      return <EnvelopeCard report={data as unknown as EnvelopeReportData} />;
    case "spending":
      return <ReportChart report={data as unknown as SpendingReportData} />;
    default:
      return null;
  }
}

const TOOL_LABELS: Record<string, string> = {
  "tool-get_envelope_status": "envelope balances",
  "tool-get_review_queue": "review queue",
  "tool-get_spending_report": "spending report",
};

function label(tool: string): string {
  return TOOL_LABELS[tool] ?? "ledger data";
}

function ToolLoading({ tool }: { tool: string }) {
  return (
    <div className="w-full rounded-xl border border-border/60 bg-card px-4 py-3 text-[12px] text-muted-foreground shadow-[var(--shadow-card)]">
      Loading {label(tool)}…
    </div>
  );
}

/**
 * A failure has to read as a failure.
 *
 * The sidecar being down is the most likely thing to go wrong in a
 * localhost-only app with two processes, and it arrives here with no prose
 * beside it. Naming what was being fetched and what to do about it is the
 * difference between a bug report and a user starting the sidecar.
 */
function ToolError({ message, tool }: { message: string; tool: string }) {
  return (
    <div className="w-full rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 shadow-[var(--shadow-card)]">
      <p className="font-medium text-[13px] text-destructive">
        Could not load the {label(tool)}
      </p>
      <p className="mt-0.5 text-[12px] text-muted-foreground">{message}</p>
      <p className="mt-1 text-[11px] text-muted-foreground">
        If the ledger service is not running, start it with{" "}
        <code className="rounded bg-muted px-1 py-0.5">
          cd sidecar &amp;&amp; uv run bookkeeper serve
        </code>
        .
      </p>
    </div>
  );
}
