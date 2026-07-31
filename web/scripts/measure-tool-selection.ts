/**
 * Measures tool-selection accuracy at the full six-tool surface.
 *
 * PLAN.md §5.3's amendment measured `qwen3:8b` picking correctly when given
 * **two** plausible tools, and concluded "the shallow single-step bet holds at
 * 8B". Phase 4 ships six, and §7's stated fallback if that degrades is to
 * shrink the tool surface — so the number has to be measured rather than
 * assumed. This script is the measurement.
 *
 * It drives the *real* tool definitions and the *real* system prompt, because
 * the tool descriptions are the thing under test. It binds them to a recording
 * stub instead of the sidecar: selection happens before execution, so a stub
 * measures the same thing, and it guarantees `allocate_to_envelope` — the one
 * write tool — cannot touch the ledger during a benchmark.
 *
 *   pnpm exec tsx scripts/measure-tool-selection.ts
 */

import { generateText } from "ai";
import { regularPrompt } from "@/lib/ai/prompts";
import { getLanguageModel, withThinking } from "@/lib/ai/providers";
import { bookkeeperTools } from "@/lib/ai/tools/bookkeeper";
import type { BookkeeperClient } from "@/lib/ai/tools/bookkeeper/client";

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
  { expected: "get_envelope_status", prompt: "how much is left for dining out" },
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

  // search_transactions
  { expected: "search_transactions", prompt: "did I ever shop at Whole Foods?" },
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

/** Records what was asked for and returns plausible-shaped data. Never touches the sidecar. */
function recordingClient(calls: string[]): BookkeeperClient {
  return {
    allocateToEnvelope: async (input) => {
      calls.push("allocate_to_envelope");
      return {
        data: {
          amount: input.amount,
          available_after: "0.00",
          committed: true,
          currency: input.currency,
          date: input.date ?? "2026-07-31",
          directive: "",
          envelope: input.envelope,
        },
        ok: true,
      };
    },
    getEnvelopes: async () => {
      calls.push("get_envelope_status");
      return {
        data: {
          asof: "2026-07-31",
          available: "0.00",
          budgeted_cash: "0.00",
          envelopes: [
            {
              allocated: "0.00",
              balance: "0.00",
              name: "Groceries",
              overspend: "0.00",
              overspent: false,
              spent: "0.00",
            },
            {
              allocated: "0.00",
              balance: "0.00",
              name: "Dining Out",
              overspend: "0.00",
              overspent: false,
              spent: "0.00",
            },
            {
              allocated: "0.00",
              balance: "0.00",
              name: "Travel",
              overspend: "0.00",
              overspent: false,
              spent: "0.00",
            },
          ],
          summary: "",
          total_envelope_balance: "0.00",
          total_overspend: "0.00",
        },
        ok: true,
      };
    },
    getReviewQueue: async () => {
      calls.push("get_review_queue");
      return { data: { entries: [], ok: true, summary: "" }, ok: true };
    },
    getSpendingReport: async (input) => {
      calls.push("get_spending_report");
      return {
        data: {
          buckets: [],
          currency: "USD",
          from: input.from,
          to: input.to,
        },
        ok: true,
      };
    },
    searchTransactions: async (input) => {
      calls.push("search_transactions");
      return {
        data: { matches: [], query: input.q, truncated: false },
        ok: true,
      };
    },
    startSync: async () => {
      calls.push("sync_accounts");
      return { data: { job_id: "bench" }, ok: true };
    },
  };
}

async function main() {
  const calls: string[] = [];
  const tools = bookkeeperTools(recordingClient(calls));
  const model = getLanguageModel("chat-model");

  let correct = 0;
  const wrong: string[] = [];

  for (const { prompt, expected } of CASES) {
    calls.length = 0;
    const started = Date.now();
    let chosen: string | null = null;
    try {
      const result = await generateText({
        model,
        prompt,
        providerOptions: withThinking(false),
        system: regularPrompt,
        tools,
      });
      chosen = result.toolCalls[0]?.toolName ?? null;
    } catch (error) {
      chosen = `ERROR: ${error instanceof Error ? error.message : String(error)}`;
    }
    const elapsed = Date.now() - started;

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
