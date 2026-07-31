"use client";

/**
 * The review queue: the primary surface of the app, and the one place where a
 * click writes to a ledger.
 *
 * Auto-apply is off on measured evidence (Phase 3), so every synced
 * transaction sits in `Expenses:Unknown` until a human confirms it — currently
 * a few hundred of them. That volume is the design brief, not an edge case:
 * rows are compact and revealed progressively, and the batch controls exist so
 * that approving a hundred transactions is a handful of clicks rather than a
 * hundred.
 *
 * Accept does not go through the model. It calls `confirmCategorizations`,
 * which POSTs straight to `/api/bookkeeper/review/confirm`. There is no
 * `sendMessage`, no tool call, no chat turn. This is PLAN.md §5.3 rule 2 and
 * the reason `POST /review/confirm` is deliberately absent from the six tools:
 * §3.3 measures an 8B model's per-step tool-calling accuracy near 90%, which
 * compounds badly, and the most-repeated action in the app is the last place
 * that belongs. The model's job is to summon this card; the card talks to the
 * sidecar itself.
 *
 * Decision state lives in `review-state.ts` — in particular the rule that a
 * confirmation which did not land must never look accepted.
 */

import {
  type Dispatch,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useState,
} from "react";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import {
  type ConfirmFn,
  categorizationTargets,
  confirmCategorizations,
  fetchCategorizationAccounts,
} from "./confirm-client";
import {
  accountLeaf,
  confidenceBand,
  formatAmount,
  formatConfidence,
  formatDate,
  formatTier,
  pluralize,
  sumAmounts,
} from "./format";
import {
  buildConfirmations,
  chosenAccount,
  type EntryKey,
  entryKey,
  initialReviewState,
  isActionable,
  type ReviewAction,
  type ReviewState,
  type RowStatus,
  reviewReducer,
  statusOf,
} from "./review-state";
import type { ReviewEntry, ReviewQueueData } from "./types";

/** Rows rendered before the reader asks for more. */
const INITIAL_VISIBLE = 25;
const VISIBLE_STEP = 25;

type AccountsState =
  | { status: "loading" }
  | { status: "ready"; accounts: string[] }
  | { status: "failed"; message: string };

export function ReviewCard({
  queue,
  confirm = confirmCategorizations,
}: {
  queue: ReviewQueueData;
  /** Injectable so the confirm path can be exercised without a network. */
  confirm?: ConfirmFn;
}) {
  const [state, dispatch] = useReducer(reviewReducer, initialReviewState);
  const [visible, setVisible] = useState(INITIAL_VISIBLE);
  const [accounts, setAccounts] = useState<AccountsState>({
    status: "loading",
  });

  useEffect(() => {
    const controller = new AbortController();
    fetchCategorizationAccounts(controller.signal)
      .then((all) =>
        setAccounts({ accounts: categorizationTargets(all), status: "ready" })
      )
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setAccounts({
          message:
            error instanceof Error
              ? error.message
              : "the account list failed to load",
          status: "failed",
        });
      });
    return () => controller.abort();
  }, []);

  const { entries } = queue;

  const actionableKeys = useMemo(
    () =>
      entries
        .map((entry) => entryKey(entry))
        .filter((key) => isActionable(statusOf(state, key))),
    [entries, state]
  );

  const selectedKeys = useMemo(() => [...state.selected], [state.selected]);

  const selectedTotal = useMemo(() => {
    const chosen = new Set(selectedKeys);
    return sumAmounts(
      entries
        .filter((entry) => chosen.has(entryKey(entry)))
        .map((entry) => entry.amount)
    );
  }, [entries, selectedKeys]);

  const accept = useCallback(
    async (keys: EntryKey[]) => {
      const { confirmations, sent, skipped } = buildConfirmations(
        state,
        entries,
        keys
      );

      if (confirmations.length === 0) {
        dispatch({
          keys: [],
          message:
            skipped.length > 0
              ? `${pluralize(skipped.length, "row")} have no account to confirm. Choose one first.`
              : "Nothing to confirm.",
          type: "submit-error",
        });
        return;
      }

      // Pre-validated here because `confirm` rejects the *entire batch* if one
      // account is not open — a single bad row would otherwise cost the user
      // every other approval in the click. Skipped when the account list did
      // not load: the server validates regardless, and refusing to submit on
      // our own missing data would be the worse failure.
      if (accounts.status === "ready") {
        const open = new Set(accounts.accounts);
        const unknown = [
          ...new Set(
            confirmations
              .map((confirmation) => confirmation.account)
              .filter((account) => !open.has(account))
          ),
        ];
        if (unknown.length > 0) {
          dispatch({
            keys: [],
            message: `${unknown.join(", ")} ${unknown.length === 1 ? "is not an open account" : "are not open accounts"}. The sidecar would reject the whole batch, so nothing was sent.`,
            type: "submit-error",
          });
          return;
        }
      }

      dispatch({ keys: sent, type: "submit-start" });
      try {
        const result = await confirm(confirmations);
        dispatch({ keys: sent, result, type: "submit-result" });
      } catch (error) {
        dispatch({
          keys: sent,
          message:
            error instanceof Error
              ? error.message
              : "The confirmation could not be sent.",
          type: "submit-error",
        });
      }
    },
    [accounts, confirm, entries, state]
  );

  const selectAll = useCallback(
    () => dispatch({ keys: actionableKeys, type: "select" }),
    [actionableKeys]
  );
  const clearSelection = useCallback(
    () => dispatch({ type: "clear-selection" }),
    []
  );
  const dismissNotice = useCallback(
    () => dispatch({ type: "dismiss-notice" }),
    []
  );
  const acceptSelected = useCallback(
    () => accept(selectedKeys),
    [accept, selectedKeys]
  );
  const showMore = useCallback(
    () => setVisible((count) => count + VISIBLE_STEP),
    []
  );

  const shown = entries.slice(0, visible);
  const remaining = entries.length - shown.length;
  const busy = selectedKeys.some(
    (key) => statusOf(state, key) === "submitting"
  );
  const currency = entries[0]?.currency ?? "USD";

  return (
    <section className="w-full rounded-xl border border-border/60 bg-card shadow-[var(--shadow-card)]">
      <header className="border-border/60 border-b px-4 py-3">
        <h3 className="font-medium text-[13px] text-foreground">
          Waiting for review
        </h3>
        {/*
          Deliberately does not claim a queue size unless the payload proves
          one. The sidecar currently reports `total` as the number it returned
          rather than the number waiting — at `limit=25` it answers `total: 25`
          while 332 sit in the queue — so "25 transactions" would tell the
          reviewer they were done when they had barely started. "Showing 25"
          is true either way, and upgrades itself once `total` means what its
          name says.
        */}
        <p className="text-[12px] text-muted-foreground">
          {queue.total > entries.length
            ? `Showing ${entries.length} of ${pluralize(queue.total, "transaction")}`
            : `Showing ${pluralize(entries.length, "transaction")}`}
          {" · nothing is written until you accept"}
        </p>
      </header>

      {queue.errors.length > 0 ? (
        <p className="border-border/60 border-b bg-destructive/10 px-4 py-2 text-[12px] text-destructive">
          {queue.errors.join(" ")}
        </p>
      ) : null}

      {queue.warnings.length > 0 ? (
        <p className="border-border/60 border-b px-4 py-2 text-[12px] text-muted-foreground">
          {queue.warnings.join(" ")}
        </p>
      ) : null}

      {accounts.status === "failed" ? (
        <p className="border-border/60 border-b px-4 py-2 text-[12px] text-muted-foreground">
          Corrections are unavailable — {accounts.message}. You can still accept
          the suggestions below.
        </p>
      ) : null}

      {state.notice ? (
        <div
          className={cn(
            "flex items-start justify-between gap-3 border-border/60 border-b px-4 py-2 text-[12px]",
            state.needsReload
              ? "bg-destructive/10 text-destructive"
              : "text-muted-foreground"
          )}
        >
          <span>{state.notice}</span>
          <button
            className="shrink-0 underline underline-offset-2"
            onClick={dismissNotice}
            type="button"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2 border-border/60 border-b px-4 py-2">
        <button
          className="rounded-md border border-border/60 px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
          disabled={actionableKeys.length === 0}
          onClick={selectAll}
          type="button"
        >
          Select all {actionableKeys.length}
        </button>
        <button
          className="rounded-md border border-border/60 px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
          disabled={selectedKeys.length === 0}
          onClick={clearSelection}
          type="button"
        >
          Clear
        </button>

        <span className="ml-auto flex items-center gap-2">
          {selectedKeys.length > 0 ? (
            <span className="text-[12px] text-muted-foreground tabular-nums">
              {pluralize(selectedKeys.length, "row")} ·{" "}
              {formatAmount(selectedTotal, currency)}
            </span>
          ) : null}
          <button
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 font-medium text-[12px] text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
            disabled={selectedKeys.length === 0 || busy}
            onClick={acceptSelected}
            type="button"
          >
            {busy ? <Spinner className="size-3" /> : null}
            Accept{selectedKeys.length > 0 ? ` ${selectedKeys.length}` : ""}
          </button>
        </span>
      </div>

      {entries.length === 0 ? (
        <p className="px-4 py-8 text-center text-[13px] text-muted-foreground">
          Nothing is waiting for review.
        </p>
      ) : (
        <ul className="divide-y divide-border/40">
          {shown.map((entry) => (
            <ReviewRow
              accounts={accounts}
              dispatch={dispatch}
              entry={entry}
              key={entryKey(entry)}
              onAccept={accept}
              selected={state.selected.has(entryKey(entry))}
              state={state}
            />
          ))}
        </ul>
      )}

      {remaining > 0 ? (
        <div className="border-border/60 border-t px-4 py-2 text-center">
          <button
            className="text-[12px] text-muted-foreground underline underline-offset-2 hover:text-foreground"
            onClick={showMore}
            type="button"
          >
            Show {Math.min(VISIBLE_STEP, remaining)} more ({remaining} left)
          </button>
        </div>
      ) : null}
    </section>
  );
}

const BAND_CLASS: Record<string, string> = {
  high: "text-foreground",
  low: "text-destructive",
  medium: "text-muted-foreground",
  none: "text-muted-foreground",
};

function ReviewRow({
  accounts,
  dispatch,
  entry,
  onAccept,
  selected,
  state,
}: {
  accounts: AccountsState;
  dispatch: Dispatch<ReviewAction>;
  entry: ReviewEntry;
  onAccept: (keys: EntryKey[]) => void;
  selected: boolean;
  state: ReviewState;
}) {
  const key = entryKey(entry);
  const status = statusOf(state, key);
  const account = chosenAccount(state, entry);
  const band = confidenceBand(entry.confidence);
  const error = state.errors[key];

  const toggle = useCallback(
    () => dispatch({ key, type: "toggle" }),
    [dispatch, key]
  );
  const choose = useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>) =>
      dispatch({ account: event.target.value, key, type: "choose" }),
    [dispatch, key]
  );
  const acceptOne = useCallback(() => onAccept([key]), [key, onAccept]);

  const options = accounts.status === "ready" ? accounts.accounts : [];
  // A suggestion the account endpoint did not list still has to be selectable,
  // or the row's own default would be missing from its dropdown.
  const withSuggestion =
    account && !options.includes(account) ? [account, ...options] : options;

  return (
    <li
      className={cn(
        "flex items-start gap-3 px-4 py-2.5",
        status === "confirmed" && "opacity-55",
        status === "submitting" && "opacity-70"
      )}
    >
      <input
        aria-label={`Select ${entry.description}`}
        checked={selected}
        className="mt-1 size-3.5 shrink-0 accent-primary"
        disabled={!isActionable(status)}
        onChange={toggle}
        type="checkbox"
      />

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="truncate text-[13px] text-foreground">
            {entry.description}
          </span>
          <span className="shrink-0 text-[13px] text-foreground tabular-nums">
            {formatAmount(entry.amount, entry.currency)}
          </span>
        </div>

        <p className="mt-0.5 text-[11px] text-muted-foreground">
          {formatDate(entry.posted_date)} · {accountLeaf(entry.asset_account)}
          {entry.payee ? ` · ${entry.payee}` : ""}
        </p>

        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          {accounts.status === "ready" || account ? (
            <select
              aria-label={`Category for ${entry.description}`}
              className="max-w-[260px] rounded-md border border-border/60 bg-background px-1.5 py-1 text-[12px] text-foreground disabled:opacity-60"
              disabled={!isActionable(status)}
              onChange={choose}
              value={account ?? ""}
            >
              {/*
                A closed set, never free text. beancount will happily open
                `Expenses:Groceries` alongside `Expenses:Food:Groceries`, so a
                typo here would become a permanent second account splitting a
                category's history in two.
              */}
              {account ? null : <option value="">Choose an account…</option>}
              {withSuggestion.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-[12px] text-muted-foreground">
              No suggestion
            </span>
          )}

          <span className={cn("text-[11px] tabular-nums", BAND_CLASS[band])}>
            {formatConfidence(entry.confidence)} · {formatTier(entry.tier)}
          </span>

          <RowStatusCell onAccept={acceptOne} status={status} />
        </div>

        {error ? (
          <p className="mt-1 text-[11px] text-destructive">{error}</p>
        ) : null}
      </div>
    </li>
  );
}

function RowStatusCell({
  onAccept,
  status,
}: {
  onAccept: () => void;
  status: RowStatus;
}) {
  if (status === "submitting") {
    return (
      <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
        <Spinner className="size-3" /> confirming
      </span>
    );
  }
  if (status === "confirmed") {
    return <span className="text-[11px] text-muted-foreground">confirmed</span>;
  }
  if (status === "uncertain") {
    return (
      <span className="text-[11px] text-destructive">reload to check</span>
    );
  }
  return (
    <button
      className="ml-auto rounded-md border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      onClick={onAccept}
      type="button"
    >
      {status === "failed" ? "Retry" : "Accept"}
    </button>
  );
}
