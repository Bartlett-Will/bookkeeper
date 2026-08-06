/**
 * Measures tool-selection accuracy at the full tool surface — six in Phase 4,
 * seven since Phase 5 added `get_month_end_report`.
 *
 * PLAN.md §5.3's amendment measured `qwen3:8b` picking correctly when given
 * **two** plausible tools, and concluded "the shallow single-step bet holds at
 * 8B". Phase 4 shipped six, and §7's stated fallback if that degrades is to
 * shrink the tool surface — so the number has to be measured rather than
 * assumed. This script is the measurement.
 *
 * **Re-run it whenever the surface changes.** §7's top risk is that an 8B
 * degrades as the surface grows, and a tool being added is the moment that
 * would show. Phase 5 added one and re-ran this rather than carrying the Phase
 * 4 number forward; the new tool also sits *between* two existing ones, so the
 * thing to watch is not only whether it is picked but whether its neighbours
 * still are.
 *
 * It drives the *real* tool definitions and the *real* system prompt, because
 * the tool descriptions are the thing under test.
 *
 * **Basis change, 2026-08-06 — earlier figures are not directly comparable.**
 * Until now this script sent `regularPrompt` alone, while the route sends
 * `systemPrompt({...})`, which appends `getDatePrompt` — so every number
 * measured before this date, including Phase 4's 22/22 and 10/10 and the first
 * seven-tool run's 25/25 and 14/15, was measured against a prompt the app never
 * sends. That is not a small difference: without the date line `qwen3:8b` was
 * observed answering "how did July go" with `2023-07`, the year from its
 * training data. The harness now composes the prompt the way the route does,
 * so the numbers describe what ships. Preserving the old basis would only have
 * kept new figures comparable with wrong ones. It binds them to a recording
 * stub instead of the sidecar: selection happens before execution, so a stub
 * measures the same thing, and it guarantees `allocate_to_envelope` — the one
 * write tool — cannot touch the ledger during a benchmark.
 *
 *   pnpm exec tsx scripts/measure-tool-selection.ts
 */

import { generateText } from "ai";
import { systemPrompt } from "@/lib/ai/prompts";
import { getLanguageModel, withThinking } from "@/lib/ai/providers";
import { bookkeeperTools } from "@/lib/ai/tools/bookkeeper";
import type { BookkeeperClient } from "@/lib/ai/tools/bookkeeper/client";

/**
 * Exactly what `app/(chat)/api/chat/route.ts` passes as `instructions`.
 *
 * `includeArtifacts` is false because `ARTIFACT_TOOLS_ACTIVE` is false there;
 * if that flag ever flips, this must follow it or the harness stops measuring
 * production again. Composed once rather than per turn so every case in a run
 * sees an identical prompt — `getDatePrompt` reads the clock, and a run
 * spanning midnight would otherwise change its own basis half way through.
 */
const SYSTEM = systemPrompt({ includeArtifacts: false });

/** `null` means "should answer in prose without calling anything". */
type Expected = string | null;

const CASES: ReadonlyArray<{ prompt: string; expected: Expected }> = [
  // sync_accounts
  { expected: "sync_accounts", prompt: "pull in my latest bank activity" },
  { expected: "sync_accounts", prompt: "can you import new transactions" },
  { expected: "sync_accounts", prompt: "grab whatever is new from the bank" },

  // get_review_queue
  { expected: "get_review_queue", prompt: "what still needs my approval?" },
  { expected: "get_review_queue", prompt: "anything uncategorized?" },
  { expected: "get_review_queue", prompt: "show me what needs reviewing" },

  // get_envelope_status
  { expected: "get_envelope_status", prompt: "how am I doing on groceries?" },
  {
    expected: "get_envelope_status",
    prompt: "how much is left for dining out",
  },
  { expected: "get_envelope_status", prompt: "am I over budget anywhere?" },

  // get_spending_report
  {
    expected: "get_spending_report",
    prompt: "where did my money go between March and June?",
  },
  {
    expected: "get_spending_report",
    prompt: "compare my spending last quarter to the one before",
  },
  {
    expected: "get_spending_report",
    prompt: "chart what I spent by category over the past year",
  },

  // get_month_end_report (Phase 5)
  { expected: "get_month_end_report", prompt: "how did July go?" },
  { expected: "get_month_end_report", prompt: "close out June for me" },
  {
    expected: "get_month_end_report",
    prompt: "give me a month-end report for March",
  },

  // search_transactions
  {
    expected: "search_transactions",
    prompt: "did I ever shop at Whole Foods?",
  },
  {
    expected: "search_transactions",
    prompt: "find my transactions at the coffee place",
  },
  { expected: "search_transactions", prompt: "look up my PG&E charges" },

  // allocate_to_envelope
  { expected: "allocate_to_envelope", prompt: "put $300 into groceries" },
  {
    expected: "allocate_to_envelope",
    prompt: "budget 150 dollars for dining out this month",
  },
  {
    expected: "allocate_to_envelope",
    prompt: "allocate 75 to the travel envelope",
  },

  // No tool: chit-chat and meta-questions. These matter as much as the
  // positives — §3.3 says small models are unreliable at deciding *whether* a
  // tool is needed at all, and a spurious call here is a wasted turn that
  // renders an irrelevant card.
  { expected: null, prompt: "thanks, that's helpful" },
  { expected: null, prompt: "what is envelope budgeting?" },
  { expected: null, prompt: "hello" },
  { expected: null, prompt: "who are you?" },
];

/**
 * The hard set: phrasings that sit on a boundary between two tools.
 *
 * `CASES` above is a fair test of whether the descriptions work, but it is not
 * an *independent* one — the same person wrote both, so it partly measures its
 * own tuning. These are the cases where two descriptions genuinely compete, and
 * they are where a six-tool surface would degrade first if it were going to.
 * Run with `--hard`.
 *
 * Where a phrasing is honestly ambiguous to a human, `expected` lists every
 * defensible answer rather than pretending there is one right one.
 */
const HARD_CASES: ReadonlyArray<{
  prompt: string;
  expected: readonly Expected[];
  note: string;
}> = [
  {
    expected: ["get_spending_report", "get_envelope_status"],
    note: "past period vs current state — the sharpest overlap in the set",
    prompt: "how much did I spend on groceries last month?",
  },
  {
    expected: ["get_envelope_status", "get_spending_report"],
    note: "bare category name, no verb",
    prompt: "groceries",
  },
  {
    expected: ["search_transactions", "get_spending_report"],
    note: "a named merchant plus a period — search and report both fit",
    prompt: "what did I buy at Target in June?",
  },
  {
    expected: ["get_envelope_status"],
    note: "'left' is envelope language; must not become a spending report",
    prompt: "how much do I have left?",
  },
  {
    expected: ["allocate_to_envelope"],
    note: "an allocation with no allocate/budget verb in it",
    prompt: "move another 50 into dining out",
  },
  {
    expected: ["allocate_to_envelope", null],
    note: "amount and envelope present but phrased as a wish, not an order",
    prompt: "I want groceries to be 400 a month",
  },
  {
    expected: ["sync_accounts", "get_review_queue"],
    note: "chaining pressure — must pick one, not attempt both",
    prompt: "sync and then show me what needs reviewing",
  },
  {
    expected: [null, "get_review_queue"],
    note: "diagnostic question about a past run, not a command to sync",
    prompt: "why did the sync fail yesterday",
  },
  {
    expected: ["search_transactions"],
    note: "'spent' is report language but a single merchant is a search",
    prompt: "how much did I spend at Whole Foods",
  },
  {
    expected: ["get_envelope_status", "get_review_queue"],
    note: "maximally vague — any read tool is defensible, a write is not",
    prompt: "show me everything",
  },

  // The five boundaries `get_month_end_report` created. A seventh tool that
  // sits *between* two existing ones is the worst case for §7's degradation
  // risk, so these test the neighbours as much as the newcomer: a month-end
  // report that swallows every question containing a month name has made the
  // surface worse, not better.
  {
    expected: ["get_spending_report", "get_month_end_report"],
    note: "a month named, but the question is spend — the sharpest new overlap",
    prompt: "how much did I spend in July?",
  },
  {
    expected: ["get_month_end_report"],
    note: "month-end intent with no month named; the tool defaults, the model should not invent one",
    prompt: "how did last month go",
  },
  {
    expected: ["get_envelope_status", "get_month_end_report"],
    note: "'this month' is in progress — envelope status is the better read, month-end is defensible",
    prompt: "how am I doing this month?",
  },
  {
    expected: ["get_spending_report"],
    note: "several months compared — explicitly outside the month-end tool's one-month scope",
    prompt: "compare June and July for me",
  },
  {
    expected: ["get_spending_report", null],
    note: "a year, not a month; must not be answered by a month-end report",
    prompt: "summarise how 2026 went",
  },
];

/** The envelopes the stub pretends exist; `allocate_to_envelope` resolves names against these. */
const ENVELOPE_NAMES = ["Groceries", "Dining Out", "Travel"];

/** A successful port result. Synchronous underneath — nothing here does I/O. */
function served<T>(data: T): Promise<{ ok: true; data: T }> {
  return Promise.resolve({ data, ok: true as const });
}

/** Records what was asked for and returns plausible-shaped data. Never touches the sidecar. */
function recordingClient(calls: string[]): BookkeeperClient {
  return {
    allocateToEnvelope: (input) => {
      calls.push("allocate_to_envelope");
      return served({
        allocated_on: input.allocated_on ?? "2026-07-31",
        amount: input.amount,
        available: "0.00",
        commit: null,
        currency: input.currency,
        directive: "",
        envelope: input.envelope,
        errors: [],
        known_envelopes: ENVELOPE_NAMES,
        ok: true,
        over_allocated: false,
        path: "",
        warnings: [],
      });
    },
    getEnvelopes: () => {
      calls.push("get_envelope_status");
      return served({
        asof: "2026-07-31",
        available: "0.00",
        budgeted_cash: "0.00",
        envelopes: ENVELOPE_NAMES.map((name) => ({
          allocated: "0.00",
          balance: "0.00",
          name,
          overspend: "0.00",
          overspent: false,
          spent: "0.00",
        })),
        summary: "",
        total_envelope_balance: "0.00",
        total_overspend: "0.00",
      });
    },
    getMonthEndReport: (input) => {
      calls.push("get_month_end_report");
      const month = input.month ?? "2026-07";
      return served({
        allocated_total: "0.00",
        asof: `${month}-28`,
        available: "0.00",
        budgeted_cash: "0.00",
        categorization: "none",
        categorized_count: 0,
        categorized_share: "0",
        closing_total: "0.00",
        complete: true,
        coverage: "complete",
        currency: "USD",
        data_through: null,
        days_elapsed: 31,
        days_in_month: 31,
        envelopes: ENVELOPE_NAMES.map((name) => ({
          allocated: "0.00",
          closing_balance: "0.00",
          consumed_ratio: null,
          direction: "flat",
          direction_reason: "",
          name,
          opening_balance: "0.00",
          over_budget: false,
          overspend: "0.00",
          overspent: false,
          periods_observed: 3,
          periods_required: 3,
          remaining: "0.00",
          spent: "0.00",
          status: "unused",
        })),
        errors: [],
        from: `${month}-01`,
        label: month,
        month,
        ok: true,
        opening_total: "0.00",
        outliers: [],
        spent_total: "0.00",
        summary: "",
        through: `${month}-28`,
        to: `${month}-28`,
        total_overspend: "0.00",
        total_spend: "0.00",
        transactions: 0,
        trend_from: null,
        trend_to: null,
        uncategorized_count: 0,
        unjudged: ENVELOPE_NAMES,
        unmapped_accounts: [],
        unmapped_total: "0.00",
        warnings: [],
      });
    },
    getReviewQueue: () => {
      calls.push("get_review_queue");
      return served({
        entries: [],
        errors: [],
        ok: true,
        shown: 0,
        total: 0,
        warnings: [],
      });
    },
    getSpendingReport: (input) => {
      calls.push("get_spending_report");
      return served({
        budget: [],
        currency: "USD",
        errors: [],
        from: input.from,
        granularity: "month",
        ok: true,
        periods: [],
        points: [],
        to: input.to,
        total: "0.00",
        total_allocated: "0.00",
        unmapped_accounts: [],
        unmapped_total: "0.00",
        warnings: [],
      });
    },
    searchTransactions: (input) => {
      calls.push("search_transactions");
      return served({
        amount_totals: [],
        errors: [],
        limit: 20,
        matches: [],
        mixed_currency: false,
        ok: true,
        query: input.q,
        shown: 0,
        total: 0,
        truncated: false,
        warnings: [],
      });
    },
    startSync: () => {
      calls.push("sync_accounts");
      return served({ job_id: "bench", started: true });
    },
  };
}

/** One turn. Returns the tool the model chose, or `null` for prose. */
async function choose(
  model: ReturnType<typeof getLanguageModel>,
  tools: ReturnType<typeof bookkeeperTools>,
  prompt: string
): Promise<{ chosen: string | null; elapsed: number }> {
  const started = Date.now();
  try {
    const result = await generateText({
      model,
      prompt,
      providerOptions: withThinking(false),
      system: SYSTEM,
      tools,
    });
    return {
      chosen: result.toolCalls[0]?.toolName ?? null,
      elapsed: Date.now() - started,
    };
  } catch (error) {
    return {
      chosen: `ERROR: ${error instanceof Error ? error.message : String(error)}`,
      elapsed: Date.now() - started,
    };
  }
}

async function runHard(
  model: ReturnType<typeof getLanguageModel>,
  tools: ReturnType<typeof bookkeeperTools>
) {
  let defensible = 0;
  for (const { prompt, expected, note } of HARD_CASES) {
    // Sequential on purpose: one local Ollama instance serves every turn, so
    // running these concurrently would only move the queue inside the server
    // and would make the per-turn latency printed below meaningless.
    // biome-ignore lint/performance/noAwaitInLoops: measuring per-turn latency against a single local model
    const { chosen, elapsed } = await choose(model, tools, prompt);
    const pass = expected.includes(chosen);
    if (pass) {
      defensible += 1;
    }
    console.log(
      `${pass ? "OK  " : "MISS"}  ${String(elapsed).padStart(5)}ms  ` +
        `${(chosen ?? "(none)").padEnd(22)} ${prompt}`
    );
    if (!pass) {
      console.log(
        `        expected one of [${expected.map((e) => e ?? "(no tool)").join(", ")}] — ${note}`
      );
    }
  }
  console.log(
    `\n${defensible}/${HARD_CASES.length} defensible on the hard set (${((defensible / HARD_CASES.length) * 100).toFixed(1)}%)`
  );
}

async function main() {
  // Printed because the prompt carries today's date, so a run is only
  // reproducible against the day it was made.
  console.log(`system prompt in use:\n${SYSTEM}\n`);

  const calls: string[] = [];
  const tools = bookkeeperTools(recordingClient(calls));
  const model = getLanguageModel("chat-model");

  if (process.argv.includes("--hard")) {
    await runHard(model, tools);
    return;
  }

  let correct = 0;
  const wrong: string[] = [];

  for (const { prompt, expected } of CASES) {
    calls.length = 0;
    // Sequential for the same reason as `runHard` above.
    // biome-ignore lint/performance/noAwaitInLoops: measuring per-turn latency against a single local model
    const { chosen, elapsed } = await choose(model, tools, prompt);

    const pass = chosen === expected;
    if (pass) {
      correct += 1;
    } else {
      wrong.push(
        `  ${JSON.stringify(prompt)}\n    expected ${expected ?? "(no tool)"}, got ${chosen ?? "(no tool)"}`
      );
    }
    console.log(
      `${pass ? "PASS" : "FAIL"}  ${String(elapsed).padStart(5)}ms  ` +
        `${(chosen ?? "(none)").padEnd(22)} ${prompt}`
    );
  }

  console.log(
    `\n${correct}/${CASES.length} correct (${((correct / CASES.length) * 100).toFixed(1)}%)`
  );
  if (wrong.length > 0) {
    console.log(`\nMisses:\n${wrong.join("\n")}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
