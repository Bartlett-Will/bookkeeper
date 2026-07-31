/**
 * Selection and confirmation state for the review queue.
 *
 * Pure and DOM-free so the rules below can be tested directly — they are the
 * part of `ReviewCard` where being wrong has consequences in a ledger rather
 * than on screen.
 *
 * Two of those rules are worth stating up front.
 *
 * **A confirmation that did not land must never look accepted.** Optimistic UI
 * is fine for a "like" button; here the optimistic state is a claim about what
 * is written in a financial record, and a row that shows as categorized while
 * still posting to `Expenses:Unknown` sends the reviewer away believing they
 * finished. So rows go to `submitting` on click and reach `confirmed` only on a
 * server response that accounts for them.
 *
 * A partial batch is not a success. `confirm_categorizations` returns a
 * count of applied edits, not the set of keys it applied — it skips any
 * confirmation that matched no `Expenses:Unknown` posting (already categorized,
 * or an id the ledger doesn't know) and reports that as a warning plus a
 * shortfall in `confirmed`. When the count is short, this module can't tell
 * which rows landed, so it refuses to guess: every row in that batch becomes
 * `uncertain` and the card asks for a reload, because the server is the only
 * thing that knows. Guessing here would mean marking a row confirmed that is
 * still uncategorized. (If the sidecar ever returns the skipped keys, this
 * collapses into per-row `confirmed`/`failed` — see `UNCERTAIN_ON_SHORTFALL`.)
 */

import type { Confirmation, ConfirmResult, ReviewEntry } from "./types";

/**
 * A queue row's identity. `simplefin_id` is unique per account, not globally,
 * so the asset account is part of the key — the sidecar keys on the same pair.
 */
export type EntryKey = string;

/**
 * Joined with a NUL, written as an explicit escape: neither component can
 * contain one, so no pair of accounts and ids can collide on a shared key.
 */
export function entryKey(entry: {
  asset_account: string;
  simplefin_id: string;
}): EntryKey {
  return `${entry.asset_account}\u0000${entry.simplefin_id}`;
}

export type RowStatus =
  | "awaiting"
  | "submitting"
  | "confirmed"
  | "failed"
  | "uncertain";

export type ReviewState = {
  /** Per-row account override; absent means "use the row's suggestion". */
  chosen: Record<EntryKey, string>;
  /** Absent means `awaiting`, which keeps the common case out of the map. */
  status: Record<EntryKey, RowStatus>;
  /** Why a row is `failed`, shown on the row itself. */
  errors: Record<EntryKey, string>;
  selected: ReadonlySet<EntryKey>;
  /** Set when the server's account of a batch didn't match what we sent. */
  needsReload: boolean;
  /** Batch-level message, shown above the rows. */
  notice: string | null;
};

export const initialReviewState: ReviewState = {
  chosen: {},
  errors: {},
  needsReload: false,
  notice: null,
  selected: new Set(),
  status: {},
};

export type ReviewAction =
  | { type: "toggle"; key: EntryKey }
  | { type: "select"; keys: EntryKey[] }
  | { type: "deselect"; keys: EntryKey[] }
  | { type: "clear-selection" }
  | { type: "choose"; key: EntryKey; account: string }
  | { type: "submit-start"; keys: EntryKey[] }
  | { type: "submit-result"; keys: EntryKey[]; result: ConfirmResult }
  | { type: "submit-error"; keys: EntryKey[]; message: string }
  | { type: "dismiss-notice" };

/** A row the user may still act on. */
export function isActionable(status: RowStatus): boolean {
  return status === "awaiting" || status === "failed";
}

export function statusOf(state: ReviewState, key: EntryKey): RowStatus {
  return state.status[key] ?? "awaiting";
}

/** The account a row would be confirmed as: the override, else the suggestion. */
export function chosenAccount(
  state: ReviewState,
  entry: ReviewEntry
): string | null {
  return state.chosen[entryKey(entry)] ?? entry.suggested_account;
}

/**
 * The confirmations a set of rows would produce.
 *
 * Rows with no account resolve to nothing and are dropped rather than sent:
 * the sidecar rejects the *entire batch* if any account is unopened or is
 * `Expenses:Unknown`, so one unresolvable row would cost the user every other
 * approval in the click. Silently dropping is the wrong half of that trade to
 * hide, which is why `buildConfirmations` reports the dropped keys too.
 */
export function buildConfirmations(
  state: ReviewState,
  entries: ReviewEntry[],
  keys: Iterable<EntryKey>
): { confirmations: Confirmation[]; sent: EntryKey[]; skipped: EntryKey[] } {
  const wanted = new Set(keys);
  const confirmations: Confirmation[] = [];
  const sent: EntryKey[] = [];
  const skipped: EntryKey[] = [];

  for (const entry of entries) {
    const key = entryKey(entry);
    if (!(wanted.has(key) && isActionable(statusOf(state, key)))) {
      continue;
    }
    const account = chosenAccount(state, entry);
    if (account) {
      confirmations.push({
        account,
        asset_account: entry.asset_account,
        simplefin_id: entry.simplefin_id,
      });
      sent.push(key);
    } else {
      skipped.push(key);
    }
  }
  return { confirmations, sent, skipped };
}

/**
 * Whether a short `confirmed` count leaves the batch's rows unknowable.
 *
 * True while the sidecar reports only a count. It is a named constant so the
 * reason survives: this is a contract limitation, not a UI preference.
 */
export const UNCERTAIN_ON_SHORTFALL = true;

function withStatus(
  status: Record<EntryKey, RowStatus>,
  keys: EntryKey[],
  next: RowStatus
): Record<EntryKey, RowStatus> {
  const updated = { ...status };
  for (const key of keys) {
    updated[key] = next;
  }
  return updated;
}

function withoutKeys<T>(
  record: Record<EntryKey, T>,
  keys: EntryKey[]
): Record<EntryKey, T> {
  const updated = { ...record };
  for (const key of keys) {
    delete updated[key];
  }
  return updated;
}

function handleResult(
  state: ReviewState,
  keys: EntryKey[],
  result: ConfirmResult
): ReviewState {
  if (!result.ok) {
    // The sidecar validates the whole batch before touching the ledger, so a
    // rejection means nothing was written. Every row goes back to awaiting —
    // the user's selections and corrections are preserved so they can fix the
    // offending account and resubmit rather than start over.
    const message =
      result.errors.join(" ") || "The sidecar rejected the batch.";
    return {
      ...state,
      errors: keys.reduce(
        (acc, key) => {
          acc[key] = message;
          return acc;
        },
        { ...state.errors }
      ),
      needsReload: false,
      notice: message,
      status: withStatus(state.status, keys, "failed"),
    };
  }

  const shortfall = keys.length - result.confirmed;
  if (shortfall > 0 && UNCERTAIN_ON_SHORTFALL) {
    return {
      ...state,
      errors: withoutKeys(state.errors, keys),
      needsReload: true,
      notice:
        `The sidecar applied ${result.confirmed} of ${keys.length} confirmations ` +
        "and did not say which. Reload the queue to see what actually landed.",
      selected: new Set(),
      status: withStatus(state.status, keys, "uncertain"),
    };
  }

  return {
    ...state,
    errors: withoutKeys(state.errors, keys),
    notice:
      result.warnings.length > 0 ? result.warnings.join(" ") : state.notice,
    selected: new Set(),
    status: withStatus(state.status, keys, "confirmed"),
  };
}

export function reviewReducer(
  state: ReviewState,
  action: ReviewAction
): ReviewState {
  switch (action.type) {
    case "toggle": {
      const selected = new Set(state.selected);
      if (selected.has(action.key)) {
        selected.delete(action.key);
      } else if (isActionable(statusOf(state, action.key))) {
        selected.add(action.key);
      }
      return { ...state, selected };
    }
    case "select": {
      const selected = new Set(state.selected);
      for (const key of action.keys) {
        if (isActionable(statusOf(state, key))) {
          selected.add(key);
        }
      }
      return { ...state, selected };
    }
    case "deselect": {
      const selected = new Set(state.selected);
      for (const key of action.keys) {
        selected.delete(key);
      }
      return { ...state, selected };
    }
    case "clear-selection":
      return { ...state, selected: new Set() };
    case "choose":
      return {
        ...state,
        chosen: { ...state.chosen, [action.key]: action.account },
        // Choosing a different account is the user answering the objection
        // that made the row fail, so the stale error goes with it.
        errors: withoutKeys(state.errors, [action.key]),
        status:
          statusOf(state, action.key) === "failed"
            ? withStatus(state.status, [action.key], "awaiting")
            : state.status,
      };
    case "submit-start":
      return {
        ...state,
        errors: withoutKeys(state.errors, action.keys),
        notice: null,
        status: withStatus(state.status, action.keys, "submitting"),
      };
    case "submit-result":
      return handleResult(state, action.keys, action.result);
    case "submit-error":
      // A transport failure is genuinely ambiguous — the request may have been
      // applied before the connection dropped. `failed` is the safe reading:
      // it keeps the row visible and re-submittable, and `confirm` is
      // idempotent for a row that did land (it no longer matches an
      // `Expenses:Unknown` posting, so it is skipped, not double-applied).
      return {
        ...state,
        errors: action.keys.reduce(
          (acc, key) => {
            acc[key] = action.message;
            return acc;
          },
          { ...state.errors }
        ),
        notice: action.message,
        status: withStatus(state.status, action.keys, "failed"),
      };
    case "dismiss-notice":
      return { ...state, notice: null };
    default:
      return state;
  }
}
