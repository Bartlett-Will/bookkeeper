import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type {
  AllocationConfirmation,
  BookkeeperClient,
  EnvelopeReport,
  MonthEndReport,
  SidecarResult,
} from "./client";
import { BOOKKEEPER_TOOL_NAMES, bookkeeperTools } from "./index";

// The port exists so these can run with no network and no sidecar. What is
// under test is the layer between an 8B model and a ledger: the arguments it
// refuses, and the fact that a failure comes back as a value it can talk about
// rather than an exception that kills the turn (PLAN.md §3.3).

const envelopes: EnvelopeReport = {
  asof: "2026-07-30",
  available: "1562.44",
  budgeted_cash: "2000.00",
  envelopes: [
    {
      allocated: "600.00",
      balance: "187.63",
      name: "Groceries",
      overspend: "0",
      overspent: false,
      spent: "412.37",
    },
    {
      allocated: "150.00",
      balance: "86.00",
      name: "Transport",
      overspend: "0",
      overspent: false,
      spent: "64.00",
    },
  ],
  summary: "…",
  total_envelope_balance: "273.63",
  total_overspend: "0",
};

/**
 * Copied from a live `GET /reports/month-end` against a fixture ledger, not
 * written from the schema. Phase 4 lost two bugs to fixtures that typechecked
 * and did not match a real body; this one has the field names and the shapes
 * the sidecar actually sends, including `"0"` rather than `"0.00"` for an
 * untouched envelope and a `percent_consumed` of null where nothing was
 * allocated.
 */
const monthEnd: MonthEndReport = {
  allocated_total: "0",
  asof: "2026-07-31",
  available: "137457.78",
  budgeted_cash: "137457.78",
  categorization: "none",
  categorized_count: 0,
  categorized_share: "0.0000",
  closing_total: "0",
  complete: true,
  coverage: "complete",
  currency: "USD",
  data_through: "2026-07-31",
  days_elapsed: 31,
  days_in_month: 31,
  envelopes: [
    {
      allocated: "0",
      closing_balance: "0",
      consumed_ratio: null,
      direction: "flat",
      direction_reason: "no spending in any of the 3 periods",
      name: "Dining Out",
      opening_balance: "0",
      over_budget: false,
      overspend: "0",
      overspent: false,
      periods_observed: 3,
      periods_required: 3,
      remaining: "0",
      spent: "0",
      status: "unused",
    },
  ],
  errors: [],
  from: "2026-07-01",
  label: "July 2026",
  month: "2026-07",
  ok: true,
  opening_total: "0",
  outliers: [],
  spent_total: "0",
  summary: "…",
  through: "2026-07-31",
  to: "2026-07-31",
  total_overspend: "0",
  total_spend: "17.00",
  transactions: 117,
  trend_from: "2026-05-02",
  trend_to: "2026-07-31",
  uncategorized_count: 114,
  unjudged: ["Dining Out"],
  unmapped_accounts: ["Expenses:Unknown"],
  unmapped_total: "17.00",
  warnings: [],
};

const allocationAccepted: AllocationConfirmation = {
  allocated_on: "2026-07-30",
  amount: "250.00",
  available: "1312.44",
  commit: null,
  currency: "USD",
  directive: "…",
  envelope: "Groceries",
  errors: [],
  known_envelopes: ["Groceries", "Transport"],
  ok: true,
  over_allocated: false,
  path: "ledger/budget.beancount",
  warnings: [],
};

type Calls = {
  allocate: unknown[];
  search: unknown[];
  spending: unknown[];
  monthEnd: unknown[];
};

function fakeClient(overrides: Partial<BookkeeperClient> = {}): {
  client: BookkeeperClient;
  calls: Calls;
} {
  const calls: Calls = {
    allocate: [],
    monthEnd: [],
    search: [],
    spending: [],
  };
  const client: BookkeeperClient = {
    allocateToEnvelope: (input) => {
      calls.allocate.push(input);
      return Promise.resolve({ data: allocationAccepted, ok: true });
    },
    getEnvelopes: () => Promise.resolve({ data: envelopes, ok: true }),
    getMonthEndReport: (input) => {
      calls.monthEnd.push(input);
      return Promise.resolve({ data: monthEnd, ok: true });
    },
    getReviewQueue: () =>
      Promise.resolve({
        data: {
          entries: [],
          errors: [],
          ok: true,
          shown: 0,
          total: 0,
          warnings: [],
        },
        ok: true,
      }),
    getSpendingReport: (input) => {
      calls.spending.push(input);
      return Promise.resolve({
        data: {
          currency: "USD",
          errors: [],
          from: input.from,
          granularity: "month",
          ok: true,
          periods: [],
          points: [],
          to: input.to,
          total: "0",
          unmapped_accounts: [],
          unmapped_total: "0",
          warnings: [],
        },
        ok: true,
      });
    },
    searchTransactions: (input) => {
      calls.search.push(input);
      return Promise.resolve({
        data: {
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
        },
        ok: true,
      });
    },
    startSync: () =>
      Promise.resolve({ data: { job_id: "job-1", started: true }, ok: true }),
    ...overrides,
  };
  return { calls, client };
}

const down = <T>(): Promise<SidecarResult<T>> =>
  Promise.resolve({
    error: "Could not reach the bookkeeper service. It may not be running.",
    ok: false,
  });

/**
 * Result shape as the assertions below read it — loose on purpose, so a test
 * can check `message` without first narrowing on `status`.
 */
type ToolOutput = { status: string; message: string; kind: string };

/** `tool()`'s execute takes an options bag none of these tools reads. */
const run = (t: unknown, input: unknown): Promise<ToolOutput> =>
  (t as { execute: (i: unknown, o: unknown) => Promise<ToolOutput> }).execute(
    input,
    {}
  );

describe("the tool surface", () => {
  it("is exactly the six of PLAN.md §5.3 plus Phase 5's month-end report", () => {
    const tools = bookkeeperTools(fakeClient().client);
    assert.deepEqual(
      Object.keys(tools).sort(),
      [...BOOKKEEPER_TOOL_NAMES].sort()
    );
    assert.equal(Object.keys(tools).length, 7);
  });

  it("has no tool that confirms a categorization", () => {
    // §5.3 rule 2, and the single most important constraint of the phase:
    // approving 40 transactions must be 40 HTTP calls and zero model calls.
    const names = Object.keys(bookkeeperTools(fakeClient().client)).join(" ");
    assert.ok(!names.includes("confirm"));
  });
});

describe("every tool turns a dead sidecar into a value, not a throw", () => {
  const inputs: Record<string, unknown> = {
    allocate_to_envelope: { amount: 250, envelope: "Groceries" },
    get_envelope_status: {},
    get_month_end_report: {},
    get_review_queue: {},
    get_spending_report: {},
    search_transactions: { query: "whole foods" },
    sync_accounts: {},
  };

  for (const name of BOOKKEEPER_TOOL_NAMES) {
    it(`${name} reports the outage`, async () => {
      const tools = bookkeeperTools({
        allocateToEnvelope: down,
        getEnvelopes: down,
        getMonthEndReport: down,
        getReviewQueue: down,
        getSpendingReport: down,
        searchTransactions: down,
        startSync: down,
      } as BookkeeperClient);

      const output = await run(tools[name], inputs[name]);
      assert.equal(output.status, "error");
      assert.match(output.message, /bookkeeper service/i);
    });
  }
});

describe("allocate_to_envelope — the only tool that writes", () => {
  it("resolves the envelope case-insensitively and sends the ledger's spelling", async () => {
    // A model answering "groceries" for an envelope named "Groceries" is a
    // near-certainty; that must not create a second envelope.
    const { calls, client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.allocate_to_envelope, {
      amount: 250,
      envelope: "  groceries ",
    });

    assert.equal(output.status, "ok");
    assert.equal(
      (calls.allocate[0] as { envelope: string }).envelope,
      "Groceries"
    );
  });

  it("refuses an envelope that does not exist, and names the real ones", async () => {
    const { calls, client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.allocate_to_envelope, {
      amount: 250,
      envelope: "Food",
    });

    assert.equal(output.status, "error");
    assert.match(output.message, /Groceries, Transport/);
    assert.match(output.message, /Nothing was allocated/);
    assert.equal(calls.allocate.length, 0, "must not reach the sidecar");
  });

  it("sends the amount as an exact decimal string", async () => {
    const { calls, client } = fakeClient();
    const tools = bookkeeperTools(client);
    await run(tools.allocate_to_envelope, {
      amount: 37.5,
      envelope: "Groceries",
    });
    assert.equal((calls.allocate[0] as { amount: string }).amount, "37.50");
  });

  it("refuses amounts that are not a whole number of cents", async () => {
    const { client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.allocate_to_envelope, {
      amount: 10.005,
      envelope: "Groceries",
    });
    assert.equal(output.status, "error");
    assert.match(output.message, /cents/);
  });

  it("refuses a zero or negative amount", async () => {
    const { client } = fakeClient();
    const tools = bookkeeperTools(client);
    const amounts = [0, -5];
    const outputs = await Promise.all(
      amounts.map((amount) =>
        run(tools.allocate_to_envelope, { amount, envelope: "Groceries" })
      )
    );
    outputs.forEach((output, i) => {
      assert.equal(
        output.status,
        "error",
        `amount ${amounts[i]} should be refused`
      );
    });
  });

  it("refuses an implausible order of magnitude", async () => {
    const { client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.allocate_to_envelope, {
      amount: 5_000_000,
      envelope: "Groceries",
    });
    assert.equal(output.status, "error");
  });

  it("refuses a date that is not a real calendar day", async () => {
    const { client } = fakeClient();
    const tools = bookkeeperTools(client);
    const dates = ["2026-02-30", "2026-13-01", "last tuesday"];
    const outputs = await Promise.all(
      dates.map((date) =>
        run(tools.allocate_to_envelope, {
          amount: 10,
          date,
          envelope: "Groceries",
        })
      )
    );
    outputs.forEach((output, i) => {
      assert.equal(output.status, "error", `${dates[i]} should be refused`);
    });
  });

  it("reports a refusal the sidecar sent back as ok:false", async () => {
    // A 200 carrying ok:false means nothing was written. Reporting success
    // here would be the worst bug available in this app.
    const { client } = fakeClient({
      allocateToEnvelope: () =>
        Promise.resolve({
          data: {
            ...allocationAccepted,
            errors: ["the budget file is not writable"],
            ok: false,
          },
          ok: true,
        }),
    });
    const tools = bookkeeperTools(client);
    const output = await run(tools.allocate_to_envelope, {
      amount: 250,
      envelope: "Groceries",
    });

    assert.equal(output.status, "error");
    assert.match(output.message, /not writable/);
    assert.match(output.message, /Nothing was allocated/);
  });

  it("does not write when the envelope list could not be read", async () => {
    const { calls, client } = fakeClient({ getEnvelopes: down });
    const tools = bookkeeperTools(client);
    const output = await run(tools.allocate_to_envelope, {
      amount: 250,
      envelope: "Groceries",
    });
    assert.equal(output.status, "error");
    assert.equal(calls.allocate.length, 0);
  });
});

describe("get_spending_report", () => {
  it("defaults both ends of the window so the model never has to do date maths", async () => {
    const { calls, client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.get_spending_report, {});

    assert.equal(output.status, "ok");
    const window = calls.spending[0] as { from: string; to: string };
    assert.match(window.from, /^\d{4}-\d{2}-\d{2}$/);
    assert.match(window.to, /^\d{4}-\d{2}-\d{2}$/);
    assert.ok(window.from < window.to);
  });

  it("refuses a range that ends before it starts", async () => {
    const { client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.get_spending_report, {
      from: "2026-07-01",
      to: "2026-06-01",
    });
    assert.equal(output.status, "error");
    assert.match(output.message, /starts .* after it ends/);
  });

  it("refuses a hallucinated date rather than passing it to the ledger", async () => {
    const { client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.get_spending_report, { from: "last month" });
    assert.equal(output.status, "error");
  });
});

describe("search_transactions", () => {
  it("refuses an empty search term", async () => {
    const { calls, client } = fakeClient();
    const tools = bookkeeperTools(client);
    const output = await run(tools.search_transactions, { query: "   " });
    assert.equal(output.status, "error");
    assert.equal(calls.search.length, 0);
  });

  it("caps the limit the model asks for", async () => {
    const { calls, client } = fakeClient();
    const tools = bookkeeperTools(client);
    await run(tools.search_transactions, { limit: 5000, query: "coffee" });
    assert.equal((calls.search[0] as { limit: number }).limit, 100);
  });
});

/** Just enough of a tool to call `toModelOutput` with a hand-built result. */
type ToolSpec = {
  toModelOutput: (options: { output: Record<string, unknown> }) => {
    type: string;
    value: string;
  };
};

describe("toModelOutput — what the model is allowed to read back", () => {
  // §5.3 rule 1: the model never recites a figure, so it cannot transpose one.
  // The full payload goes to React; the model gets an acknowledgement.
  it("hands the model no numbers from a successful result", () => {
    const tools = bookkeeperTools(fakeClient().client);
    for (const name of BOOKKEEPER_TOOL_NAMES) {
      const spec = tools[name] as unknown as ToolSpec;
      const rendered = spec.toModelOutput({
        output: { data: envelopes, kind: "envelopes", status: "ok" },
      });
      assert.equal(rendered.type, "text");
      assert.ok(
        !/\d/.test(rendered.value),
        `${name} leaked a digit into the model's context: ${rendered.value}`
      );
      assert.match(rendered.value, /displayed|started|recorded/i);
    }
  });

  it("tells the model the figures are absent, not merely off-limits", () => {
    // The regression guard for the fabrication measured on 2026-07-31. The
    // previous wording ("do not repeat its numbers") implied the model was
    // holding the figures, and it answered "You have $25.00 left in your
    // Groceries envelope" against a real balance of 0.00 — a different
    // invented number on each of four turns. Withholding the data is only
    // half the mechanism; the model also has to be told it does not have it,
    // or it fills the gap. See renderedByTheUi in result.ts.
    const tools = bookkeeperTools(fakeClient().client);
    for (const name of BOOKKEEPER_TOOL_NAMES) {
      const spec = tools[name] as unknown as ToolSpec;
      const { value } = spec.toModelOutput({
        output: { data: envelopes, kind: "envelopes", status: "ok" },
      });
      assert.match(
        value,
        /not shown the figures|do not know them/i,
        `${name} must tell the model the figures are absent`
      );
      assert.match(value, /invented|state none/i);
    }
  });

  it("passes an error through, because the model does have to relay it", () => {
    const tools = bookkeeperTools(fakeClient().client);
    const spec = tools.get_envelope_status as unknown as ToolSpec;
    const rendered = spec.toModelOutput({
      output: {
        kind: "envelopes",
        message: "the sidecar is down.",
        status: "error",
      },
    });
    assert.match(rendered.value, /the sidecar is down/);
    assert.match(rendered.value, /do not guess/i);
  });
});
