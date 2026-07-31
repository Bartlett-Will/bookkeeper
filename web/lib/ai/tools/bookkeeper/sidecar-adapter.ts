import "server-only";

import {
  allocateToEnvelope as sidecarAllocate,
  getEnvelopes as sidecarGetEnvelopes,
  getReviewQueue as sidecarGetReviewQueue,
  getSpendingReport as sidecarGetSpendingReport,
  searchTransactions as sidecarSearchTransactions,
  startSync as sidecarStartSync,
} from "@/lib/sidecar/client";
// Functions from `client`, types from `contract`. `contract.ts` carries no
// `server-only`, so types stay importable from client components; reaching
// them through `client.ts` would drag `server-only` into the browser bundle.
import type {
  CallOptions,
  SidecarResult as TransportResult,
} from "@/lib/sidecar/contract";
import type { BookkeeperClient, EnvelopeReport, SidecarResult } from "./client";
import {
  toAllocation,
  toReviewQueue,
  toSpendingReport,
  toSyncStarted,
  toTransactionSearch,
} from "./normalize";

// Binds the six-method port in `client.ts` to the real HTTP client in
// `lib/sidecar/client.ts`. Wiring only — the payload mapping lives in
// `normalize.ts`, which is free of `server-only` so it can be tested.
//
// The one conversion this file does own is the result type. `lib/sidecar`
// discriminates on `outcome` and carries a `kind`, a `status` and a `detail`;
// the port discriminates on `ok` and carries a single sentence. That is not
// duplication for its own sake — the port's consumer is an 8B model that has
// to say something sensible to a human, and `{kind: "malformed", detail: {…}}`
// is not that. The richer shape is still what the `/api/bookkeeper/*` proxies
// return (`respond.ts` keeps all three fields); it is only the model that gets
// the plain sentence.

/** Transport result → port result, applying `project` to a successful payload. */
function narrow<Raw, T>(
  result: TransportResult<Raw>,
  project: (data: Raw) => T
): SidecarResult<T> {
  if (result.outcome === "success") {
    return { data: project(result.data), ok: true };
  }
  return { error: result.message, ok: false };
}

export function createSidecarBookkeeperClient(
  options: CallOptions = {}
): BookkeeperClient {
  return {
    allocateToEnvelope: async (input) => {
      const result = await sidecarAllocate(
        {
          // The sidecar's field is `allocated_on`, not `date` — it names the
          // day the allocation is *effective in the ledger*, which is the
          // date on the emitted `custom "envelope" "allocate"` directive, not
          // the wall-clock moment the request was made.
          allocated_on: input.allocated_on ?? null,
          amount: input.amount,
          currency: input.currency,
          envelope: input.envelope,
        },
        options
      );
      return narrow(result, (body) =>
        toAllocation(body, {
          currency: input.currency,
          envelope: input.envelope,
        })
      );
    },

    getEnvelopes: async (input) => {
      const result = await sidecarGetEnvelopes(
        { asof: input.asof ?? null },
        options
      );
      // The only one of the six already published in the sidecar's OpenAPI
      // schema, so this is a compiler-checked pass-through rather than a
      // projection: `EnvelopeReportResponse` and the port's `EnvelopeReport`
      // are the same shape, and if the sidecar changes one, `pnpm run
      // sidecar:types` makes it a type error here.
      return narrow(result, (data): EnvelopeReport => data);
    },

    getReviewQueue: async (input) => {
      const result = await sidecarGetReviewQueue(
        { limit: input.limit ?? null },
        options
      );
      return narrow(result, toReviewQueue);
    },

    getSpendingReport: async (input) => {
      const result = await sidecarGetSpendingReport(
        { from: input.from, to: input.to },
        options
      );
      return narrow(result, (body) =>
        toSpendingReport(body, { from: input.from, to: input.to })
      );
    },

    searchTransactions: async (input) => {
      const result = await sidecarSearchTransactions(
        { limit: input.limit ?? null, q: input.q },
        options
      );
      return narrow(result, (body) => toTransactionSearch(body, input.q));
    },

    startSync: async (input) => {
      const result = await sidecarStartSync(
        {
          // Always a real sync. `demo: true` hits SimpleFIN's demo server,
          // which is how the CLI and the test suite exercise this without
          // spending against the ~24 req/day live budget (PLAN.md §3.1) — but
          // a chat turn is a user asking for *their* data, and silently
          // serving synthetic accounts would be the worst possible answer.
          demo: false,
          since: input.since ?? null,
        },
        options
      );
      return narrow(result, toSyncStarted);
    },
  };
}
