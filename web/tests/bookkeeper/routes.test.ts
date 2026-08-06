import { expect, test } from "@playwright/test";

/**
 * Contract tests for the `/api/bookkeeper/*` proxies.
 *
 * Every case here is rejected by the route *before* it reaches the sidecar, so
 * this file never writes to a ledger and never spends SimpleFIN budget. That
 * is deliberate and worth preserving: the endpoints these routes front are
 * real financial writes, and a test suite that exercises them for real is a
 * test suite that commits to someone's books on every run.
 *
 * What is checked is the part the rest of the app depends on — the shape of a
 * refusal. Workers downstream branch on `kind` and render `error`, so those
 * two fields are load-bearing API, not incidental debug output.
 */

const API = "/api/bookkeeper";

/** The error envelope every proxy returns, from `lib/sidecar/respond.ts`. */
type ErrorBody = {
  error: string;
  kind: "unreachable" | "timeout" | "http" | "malformed";
  reachable: boolean;
  status?: number;
};

/**
 * Establishes the session before any assertion.
 *
 * `proxy.ts` redirects an unauthenticated request to `/api/auth/local`, and it
 * rebuilds the return URL from `pathname` alone — so the query string is
 * dropped and `?limit=abc` comes back as a plain, valid request. Without this
 * warm-up a validation test silently asserts against the wrong request and
 * passes for the wrong reason.
 */
test.beforeEach(async ({ request }) => {
  await request.get("/");
});

test.describe("bookkeeper proxy input validation", () => {
  test("rejects a non-numeric limit rather than forwarding it", async ({
    request,
  }) => {
    const response = await request.get(`${API}/review-queue?limit=abc`);

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.kind).toBe("http");
    expect(body.status).toBe(400);
    // The message names the offending value: a bad limit is our bug, and the
    // sidecar's 422 would not say which layer produced it.
    expect(body.error).toContain("limit");
    expect(body.error).toContain("abc");
  });

  test("rejects a zero limit", async ({ request }) => {
    const response = await request.get(`${API}/review-queue?limit=0`);
    expect(response.status()).toBe(400);
  });

  test("requires a search query", async ({ request }) => {
    const response = await request.get(`${API}/transactions/search`);

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.error).toContain("q");
  });

  test("requires a non-empty search query", async ({ request }) => {
    const response = await request.get(`${API}/transactions/search?q=%20`);
    expect(response.status()).toBe(400);
  });
});

test.describe("review confirm — the click-bypass path", () => {
  test("rejects a confirmation missing simplefin_id", async ({ request }) => {
    // The failure this guards against is silent rather than loud: a batch
    // keyed on the wrong field matches nothing, and "confirmed 0 of 40" reads
    // like an empty queue instead of like a bug.
    const response = await request.post(`${API}/review/confirm`, {
      data: {
        confirmations: [{ account: "Expenses:Food", asset_account: "A" }],
      },
    });

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.error).toContain("simplefin_id");
    expect(body.error).toContain("confirmations[0]");
  });

  test("names the index of the offending entry in a batch", async ({
    request,
  }) => {
    const good = {
      account: "Expenses:Food:Groceries",
      asset_account: "Assets:SimpleFIN:Checking",
      simplefin_id: "1",
    };
    const response = await request.post(`${API}/review/confirm`, {
      data: { confirmations: [good, good, { account: "x" }] },
    });

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    // Position matters when a forty-card approval is rejected — "one of them
    // is malformed" is not an actionable message.
    expect(body.error).toContain("confirmations[2]");
  });

  test("rejects a body that is not an object", async ({ request }) => {
    const response = await request.post(`${API}/review/confirm`, {
      data: [],
    });
    expect(response.status()).toBe(400);
  });

  test("rejects a missing confirmations array", async ({ request }) => {
    const response = await request.post(`${API}/review/confirm`, { data: {} });

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.error).toContain("confirmations");
  });
});

test.describe("allocate — money stays a decimal string", () => {
  test("rejects an amount with more than two decimal places", async ({
    request,
  }) => {
    const response = await request.post(`${API}/envelopes/allocate`, {
      data: { amount: "12.3456", envelope: "Groceries" },
    });

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.error).toContain("amount");
  });

  test("rejects a numeric amount", async ({ request }) => {
    // Accepting a JSON number here would mean a float had already touched the
    // value before we ever saw it.
    const response = await request.post(`${API}/envelopes/allocate`, {
      data: { amount: 125.0, envelope: "Groceries" },
    });
    expect(response.status()).toBe(400);
  });

  test("rejects an empty envelope name", async ({ request }) => {
    const response = await request.post(`${API}/envelopes/allocate`, {
      data: { amount: "125.00", envelope: "  " },
    });

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.error).toContain("envelope");
  });
});

test.describe("categorize — writing is opt-in", () => {
  test("rejects a non-boolean apply flag", async ({ request }) => {
    const response = await request.post(`${API}/categorize`, {
      data: { apply: "yes" },
    });

    expect(response.status()).toBe(400);
    const body: ErrorBody = await response.json();
    expect(body.error).toContain("apply");
  });

  test("rejects a non-integer limit", async ({ request }) => {
    const response = await request.post(`${API}/categorize`, {
      data: { limit: 2.5 },
    });
    expect(response.status()).toBe(400);
  });
});
