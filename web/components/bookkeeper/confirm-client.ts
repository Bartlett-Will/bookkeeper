/**
 * The click path that does not go through the model.
 *
 * This is PLAN.md §5.3 rule 2, and it is the single most important constraint
 * in Phase 4. When the reviewer clicks *Accept*, this module POSTs to the
 * sidecar proxy and renders the answer. It does not append a message, it does
 * not call `sendMessage`, it does not re-enter the chat loop. Approving 332
 * transactions is 332 HTTP requests — or, batched, a handful — and **zero** LLM
 * calls.
 *
 * The reason is measured, not stylistic: §3.3 puts an 8B model's per-step
 * tool-calling accuracy near 90%, which compounds to roughly a 40% failure rate
 * across five steps, and the model degrades further over a long conversation.
 * Routing the most-repeated action in the app through that is how a budgeting
 * tool ends up writing the wrong account into a ledger. The model's job is to
 * summon this UI; the UI's job is to talk to the sidecar itself.
 *
 * Correspondingly, `POST /review/confirm` is deliberately not one of the six
 * tools. If it ever becomes one, this file is why it should not have.
 *
 * `fetch` here also keeps a hard invariant from §9: the browser talks only to
 * Next, Next proxies to the sidecar, and the SimpleFIN Access URL never leaves
 * the Python process.
 */

import type { Confirmation, ConfirmResult } from "./types";

export const CONFIRM_ENDPOINT = "/api/bookkeeper/review/confirm";

/**
 * Mirrors the sidecar's `GET /accounts/categorizable`, which returns
 * `{accounts: string[]}` — the open expense and income accounts, and the only
 * set a correction may target.
 *
 * `ReviewCard` degrades to accept-only if this fails, so the queue stays
 * usable when the sidecar is down — but the correction control needs the
 * list, because `POST /review/confirm` rejects the whole batch if any account
 * is not open in the ledger. Pre-validating here is what stops one bad
 * account costing a user their other thirty-nine approvals.
 */
export const ACCOUNTS_ENDPOINT = "/api/bookkeeper/accounts/categorizable";

export type ConfirmFn = (
  confirmations: Confirmation[]
) => Promise<ConfirmResult>;

/**
 * Send a batch of human decisions straight to the sidecar.
 *
 * Batch-first because `confirm_categorizations` makes one ledger pass and one
 * git commit per call: forty single-item requests would produce forty commits
 * and forty ledger rewrites, where one request produces one revertible commit.
 */
export const confirmCategorizations: ConfirmFn = async (confirmations) => {
  const response = await fetch(CONFIRM_ENDPOINT, {
    body: JSON.stringify({ confirmations }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });

  if (!response.ok) {
    // A non-2xx has no `ConfirmResult` body to trust, and the caller must not
    // read "no errors listed" as "nothing went wrong". Throwing routes this to
    // the reducer's `submit-error`, which leaves the rows re-submittable.
    const detail = await response.text().catch(() => "");
    throw new Error(
      `The sidecar returned ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`
    );
  }

  return parseConfirmResult(await response.json());
};

/**
 * Read a `ConfirmResult` out of the sidecar's response envelope.
 *
 * Every sidecar payload arrives wrapped — `{ok, summary, <key>: {...}}`, where
 * `/review-queue` names the key `queue` and `/review/confirm` names it
 * `result`. Casting the envelope straight to `ConfirmResult` is the bug this
 * function exists to prevent, and it is a quiet one: `confirmed` would read
 * `undefined`, `keys.length - undefined` is `NaN`, and `NaN > 0` is false, so a
 * fully-applied batch would slip past the shortfall check and every row would
 * be marked `confirmed` on no evidence at all. The reducer is careful about
 * exactly this; it can only be as careful as the value handed to it.
 *
 * Unrecognizable shapes throw rather than defaulting. A `confirmed` this
 * function had to invent is indistinguishable from one the server sent, and
 * the whole point of the shortfall path is that the UI does not guess how much
 * of a batch landed. Throwing routes to `submit-error`, which leaves the rows
 * visible and re-submittable.
 */
export function parseConfirmResult(body: unknown): ConfirmResult {
  const envelope = asRecord(body);
  if (!envelope) {
    throw new Error("The sidecar's confirm response was not a JSON object.");
  }

  // Tolerates a flattened payload so this keeps working if the sidecar ever
  // stops wrapping; `confirmed` is the field that tells the two apart.
  const payload = asRecord(envelope.result) ?? envelope;

  if (typeof payload.confirmed !== "number") {
    throw new Error(
      "The sidecar's confirm response had no `confirmed` count, so how much of the batch landed is unknown."
    );
  }

  return {
    confirmed: payload.confirmed,
    errors: asStrings(payload.errors),
    files_written: asStrings(payload.files_written),
    learned: typeof payload.learned === "number" ? payload.learned : 0,
    // `ok: false` is a refusal the sidecar answers 200 with, so its absence
    // must not read as success — only an explicit `true` counts.
    ok: payload.ok === true,
    warnings: asStrings(payload.warnings),
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v) => typeof v === "string") : [];
}

/**
 * Accounts a correction may target.
 *
 * Fetched rather than typed, because a free-text account field in a ledger UI
 * is a defect: beancount will happily accept `Expenses:Groceries` next to
 * `Expenses:Food:Groceries`, and a typo becomes a permanent second account that
 * quietly splits a category's history in two. The correction control is a
 * closed set or it is nothing.
 */
export async function fetchCategorizationAccounts(
  signal?: AbortSignal
): Promise<string[]> {
  const response = await fetch(ACCOUNTS_ENDPOINT, { signal });
  if (!response.ok) {
    throw new Error(`The sidecar returned ${response.status}`);
  }
  const body = (await response.json()) as {
    accounts?: (string | { account?: string })[];
  };
  return (body.accounts ?? [])
    .map((item) => (typeof item === "string" ? item : (item.account ?? "")))
    .filter(Boolean);
}

/**
 * The accounts a transaction can be categorized *as*.
 *
 * Asset and equity accounts are the transaction's own side of the entry, never
 * its category, so offering them would only invite a nonsensical confirmation
 * that the sidecar would reject for the whole batch. `Expenses:Unknown` and
 * `Income:Unknown` are excluded for the reason `confirm_categorizations` gives
 * when it rejects them: they are the absence of a decision, not a decision.
 */
export function categorizationTargets(accounts: string[]): string[] {
  return accounts
    .filter(
      (account) =>
        (account.startsWith("Expenses:") || account.startsWith("Income:")) &&
        !account.endsWith(":Unknown")
    )
    .sort((a, b) => a.localeCompare(b));
}
