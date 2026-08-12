/**
 * GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Produced from the sidecar's OpenAPI schema by `pnpm run sidecar:types`
 * (see scripts/generate-sidecar-types.ts). Hand-editing this file
 * reintroduces exactly the drift PLAN.md §5.1's typed contract exists to
 * prevent: change the pydantic models in `sidecar/bookkeeper/api.py`, then
 * regenerate.
 */

export interface paths {
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Health
         * @description Liveness, the beancount version, and which ledger is being served.
         *
         *     `root` is what `paths.root()` actually computed, symlink-resolved --
         *     deliberately not the `BOOKKEEPER_ROOT` env var. An unset var must report
         *     the real repo path rather than an empty string, because the whole value
         *     of the field is that it answers without the caller knowing how the
         *     process was configured.
         */
        get: operations["health_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sync": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Sync */
        post: operations["sync_sync_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/accounts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Accounts
         * @description Current SimpleFIN-derived asset accounts and balances, from the ledger.
         *
         *     This reads the *ledger*, not SimpleFIN live -- fetching live data is
         *     reserved for `/sync` given the ~24 req/day rate limit (PLAN.md §3.1).
         */
        get: operations["accounts_accounts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/envelopes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Envelopes
         * @description Envelope balances as of `asof` (ISO date; defaults to the UTC day).
         *
         *     Feeds the cached ledger into the pure `compute_envelope_state` rather than
         *     calling `envelope_report`, which would reload the ledger from disk on every
         *     request -- this is Phase 4's `get_envelope_status`, on the chat hot path.
         */
        get: operations["envelopes_envelopes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/verify": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Verify
         * @description Run the ledger + envelope integrity checks (PLAN.md §5.2).
         *
         *     A failing ledger is a 200 with `ok: false`, not an HTTP error: the request
         *     succeeded and the findings *are* the payload. Reserving 5xx for genuine
         *     endpoint failure keeps "the books are wrong" distinguishable from "the
         *     sidecar is broken".
         */
        get: operations["verify_verify_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/review-queue": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Review Queue
         * @description Transactions awaiting human categorization. Read-only.
         */
        get: operations["review_queue_review_queue_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/categorize": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Categorize
         * @description Predict accounts for uncategorized transactions. Dry run unless `apply`.
         */
        post: operations["categorize_categorize_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/accounts/categorizable": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Categorizable Accounts
         * @description The open expense/income accounts a categorization may name.
         *
         *     Deliberately *not* `/accounts`, which answers a different question (which
         *     bank accounts exist and what is in them) and filters on `Assets:`.
         *
         *     The set comes from `categorize.context.build_ledger_context`, not from a
         *     filter written here, because it is the same set the cascade is
         *     constrained to predict into. Two definitions of "a valid account" would
         *     drift, and the drift would surface as a review-card dropdown offering an
         *     account the categorizer could never suggest -- and then as
         *     `POST /review/confirm` rejecting a whole batch over an account the UI
         *     itself proposed. `LedgerContext.examples` is built and thrown away here;
         *     one definition is worth a scan over already-cached entries.
         */
        get: operations["categorizable_accounts_accounts_categorizable_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/review/confirm": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Review Confirm
         * @description Accept a batch of human categorizations: write them, and teach tier 1.
         *
         *     This is what an *Accept* button hits. It is deliberately not one of the
         *     six chat tools (§5.3 rule 2): approving forty transactions must be forty
         *     deterministic HTTP calls and zero LLM calls, so the button reaches this
         *     endpoint directly and the model is never in the loop.
         *
         *     A rejected batch is a 200 with `ok: false` and one error per bad
         *     confirmation, not a 500. The findings *are* the payload -- the UI has to
         *     show the user which account it refused and why -- and flattening them
         *     into a 500 would leave "your correction is invalid" indistinguishable
         *     from "the sidecar fell over" (the same reasoning as `/verify`).
         *
         *     `context` is passed so `confirm_categorizations` actually runs its
         *     open-account check; without it the guard is inert and a typo'd account
         *     lands in the ledger. That check rejects the *whole* batch, which is why
         *     `GET /accounts/categorizable` exists for the UI to pre-validate against.
         */
        post: operations["review_confirm_review_confirm_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sync/start": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Sync Start
         * @description Kick off a sync and return immediately with its job id.
         *
         *     §5.3 rule 3: a sync fetches over the network and can be followed by
         *     categorizing dozens of transactions at ~1-2s each. Doing that inside the
         *     request would stall a chat turn for a minute, so the work runs on a
         *     background thread and the UI polls `GET /sync/status/{job_id}`.
         *
         *     Starting a sync while one is already running returns the running job
         *     rather than launching a second: SimpleFIN allows on the order of 24
         *     requests a day (§3.1), so a double-clicked *Sync* is not a wasted thread,
         *     it is a meaningful slice of the daily budget.
         */
        post: operations["sync_start_sync_start_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sync/status/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Sync Status
         * @description Where a sync job has got to. Cheap enough to poll.
         */
        get: operations["sync_status_sync_status__job_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/backfill/plan": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Backfill Plan
         * @description Show the windows a backfill would request. Spends nothing.
         *
         *     Not a nicety at ~24 requests a day (§3.1): this is how a caller sees what
         *     a run will cost, and what a previous run already completed, *before*
         *     committing any of the budget to it. It is a `POST` only because it takes
         *     a body; it neither fetches nor writes -- not even the resume state file.
         *
         *     `ok: false` here means the plan does not fit in what is left of today's
         *     budget, which is a real answer and not an error.
         */
        post: operations["backfill_plan_backfill_plan_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/backfill/start": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Backfill Start
         * @description Kick off a backfill and return immediately with its job id.
         *
         *     Refuses while a sync is running. The two are different job kinds, so the
         *     registry's own single-flighting would not catch it, and they draw on one
         *     shared budget of ~24 requests a day (§3.1) -- a sync racing a five-window
         *     backfill is a fifth of the day spent on a collision nobody asked for.
         */
        post: operations["backfill_start_backfill_start_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/backfill/status/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Backfill Status
         * @description Where a backfill job has got to. Cheap enough to poll.
         */
        get: operations["backfill_status_backfill_status__job_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/envelopes/allocate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Envelopes Allocate
         * @description Move money into an envelope by appending an `allocate` directive.
         *
         *     A refused allocation -- unknown envelope, negative amount, a directive
         *     that would not parse -- is a 200 with `ok: false`, its reasons, and the
         *     known envelope names. The request succeeded and the refusal is the
         *     payload, exactly as with `/verify`; a 4xx here would also throw away
         *     `known_envelopes`, which is the field that lets a caller fix its own
         *     mistake.
         *
         *     Over-allocation is reported, never prevented: `over_allocated` and the
         *     recomputed `available` come back on a successful write, because §5.2's
         *     `verify` is what judges a budget, and the numbers move under the user's
         *     feet whenever a sync lands.
         */
        post: operations["envelopes_allocate_envelopes_allocate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/transactions/search": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Transactions Search
         * @description Free-text search over ledger transactions. Read-only.
         *
         *     `q` is untrusted -- it is typed by a user or emitted by an 8B model --
         *     and `reports.search` is where that is dealt with (bound query parameter,
         *     escaped as a literal pattern). The cached ledger is fed in rather than
         *     reloaded per keystroke (§5.1).
         *
         *     An empty `q` comes back as `ok: false` with a reason rather than a 422:
         *     the caller is often a model, and a structured "nothing to search for"
         *     is easier for it to recover from than an HTTP error.
         */
        get: operations["transactions_search_transactions_search_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/reports/spending": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Reports Spending
         * @description Spend by envelope over time. Both bounds inclusive, both optional.
         *
         *     Defaults to the ledger's own first and last transaction dates rather than
         *     a wall-clock window, so a report of a fixed ledger does not change meaning
         *     because a day passed.
         *
         *     `period` is validated by `reports.spending` against its own `PERIODS`
         *     rather than re-listed here; an unknown period or an unparseable date is a
         *     422, because unlike a refused allocation there is no useful payload to
         *     return -- the request cannot be answered at all.
         */
        get: operations["reports_spending_reports_spending_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/reports/month-end": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Reports Month End
         * @description The composite month-end report: envelopes, budget vs actual, unmapped.
         *
         *     `month` is `YYYY-MM` and defaults to the month of the ledger's last
         *     transaction rather than the wall-clock month, for the same reason
         *     `/reports/spending` defaults its window off the ledger's own bounds: a
         *     report of a fixed ledger must not become an empty one because a day
         *     passed.
         *
         *     An unparseable `month` is a 422 — unlike a refused allocation there is no
         *     useful payload to return, because there is no month to report on.
         */
        get: operations["reports_month_end_reports_month_end_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/reports/budget": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Reports Budget
         * @description Allocated vs actually spent per envelope. Both bounds inclusive and optional.
         *
         *     Defaults to the ledger's own first and last transaction dates rather than
         *     a wall-clock window, for the reason `/reports/spending` does: a report of
         *     a fixed ledger must not change meaning because a day passed.
         *
         *     An unparseable date is a 422 -- there is no useful payload to return,
         *     since the request cannot be answered at all. A backwards window is a 200
         *     with `ok: false`, because that request *was* answered: the answer is that
         *     it describes no window.
         */
        get: operations["reports_budget_reports_budget_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/reports/trends": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Reports Trends
         * @description Spending direction per envelope, and transactions unusual for theirs.
         *
         *     Monthly periods only, and deliberately: an envelope budget is a monthly
         *     instrument (§5.2), and a granularity parameter would let a caller pick
         *     the one that produced the answer it wanted.
         *
         *     Same error split as `/reports/budget`: an unparseable date is a 422, a
         *     window that describes nothing is a 200 with `ok: false`.
         */
        get: operations["reports_trends_reports_trends_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/reconcile": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Reconcile
         * @description Check one account against a bank statement (PLAN.md §5.2 item 3).
         *
         *     POST rather than GET because a statement is a body, not a query string.
         *
         *     A *discrepancy* is a 200 with `ok: false`, not an error status: the client
         *     asked a question and got the answer, and the answer is where the books
         *     disagree with the bank. Only a request that cannot be answered at all --
         *     an unknown or ambiguous account, a statement in the wrong currency, a
         *     balance with no date -- is a 422, because there is no reconciliation to
         *     return.
         *
         *     Deliberately does not 500 on a ledger that failed to load, unlike
         *     `/reports/month-end`. The likeliest reason this ledger has errors is a
         *     *failing balance assertion*, which is the exact condition someone reaches
         *     for reconciliation to diagnose; refusing then would withhold the tool at
         *     the moment it is wanted. The load errors come back as a note instead.
         */
        post: operations["reconcile_reconcile_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AccountPositionModel
         * @description One currency's worth of an account's balance.
         *
         *     `number` is a `Decimal` and therefore serializes as a string. A balance
         *     is money; JSON numbers are doubles.
         */
        AccountPositionModel: {
            /** Number */
            number: string;
            /** Currency */
            currency: string;
        };
        /** AccountsResponse */
        AccountsResponse: {
            /** Accounts */
            accounts: components["schemas"]["LedgerAccountModel"][];
            /** Note */
            note?: string | null;
        };
        /** AllocateRequest */
        AllocateRequest: {
            /** Envelope */
            envelope: string;
            /** Amount */
            amount: number | string;
            /**
             * Currency
             * @default USD
             */
            currency: string;
            /** Allocated On */
            allocated_on?: string | null;
        };
        /** AllocateResponse */
        AllocateResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Envelope */
            envelope: string;
            /** Amount */
            amount: string;
            /** Currency */
            currency: string;
            /** Allocated On */
            allocated_on?: string | null;
            /** Directive */
            directive: string;
            /** Path */
            path: string;
            /** Available */
            available?: string | null;
            /** Over Allocated */
            over_allocated: boolean;
            /** Known Envelopes */
            known_envelopes: string[];
            commit?: components["schemas"]["CommitModel"] | null;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /**
         * AutoApplyPolicyModel
         * @description Whether unattended writes are permitted at all, and on whose say-so.
         *
         *     Reported on every categorize response because "auto-apply is OFF" is the
         *     headline fact about a dry run (Phase 3, measured), not a footnote.
         */
        AutoApplyPolicyModel: {
            /** Threshold */
            threshold?: number | null;
            /**
             * Source
             * @default default
             */
            source: string;
        };
        /** BackfillRequest */
        BackfillRequest: {
            /** From Date */
            from_date: string;
            /** To Date */
            to_date?: string | null;
            /** Max Requests */
            max_requests?: number | null;
            /**
             * Restart
             * @default false
             */
            restart: boolean;
            /**
             * Demo
             * @default false
             */
            demo: boolean;
        };
        /**
         * BackfillResponse
         * @description One backfill run or preview, with all three date ranges kept apart.
         *
         *     `requested_*`, `honoured_*` and `data_*` are three different facts and
         *     the browser must not compute any of them from the others -- a window the
         *     server capped honoured less than it was asked for, and an account that
         *     was quiet spans less than was honoured. Field-for-field
         *     `BackfillResult.to_dict()`, which is also the job payload below.
         */
        BackfillResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Dry Run */
            dry_run: boolean;
            /** Complete */
            complete: boolean;
            /** Requested From */
            requested_from?: string | null;
            /** Requested To */
            requested_to?: string | null;
            /** Honoured From */
            honoured_from?: string | null;
            /** Honoured Through */
            honoured_through?: string | null;
            /** Data From */
            data_from?: string | null;
            /** Data Through */
            data_through?: string | null;
            /** Windows */
            windows: components["schemas"]["BackfillWindowModel"][];
            /** Requests Made */
            requests_made: number;
            /** Requests Planned */
            requests_planned: number;
            /** Requests Used Today */
            requests_used_today: number;
            /** Requests Remaining Today */
            requests_remaining_today: number;
            /** Daily Budget */
            daily_budget: number;
            /** Transactions Seen */
            transactions_seen: number;
            /** Transactions Added */
            transactions_added: number;
            /** Opening Balances Written */
            opening_balances_written: number;
            /** Stopped Reason */
            stopped_reason?: string | null;
            /** State Path */
            state_path?: string | null;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /** BackfillStartResponse */
        BackfillStartResponse: {
            /** Job Id */
            job_id: string;
            /** Kind */
            kind: string;
            /** State */
            state: string;
            /** Started */
            started: boolean;
        };
        /** BackfillStatusResponse */
        BackfillStatusResponse: {
            /** Job Id */
            job_id: string;
            /** Kind */
            kind: string;
            /** State */
            state: string;
            /** Progress */
            progress: number;
            /** Total */
            total: number;
            /** Step */
            step: string;
            result?: components["schemas"]["BackfillResponse"] | null;
            /** Error */
            error?: string | null;
            /** Started At */
            started_at: number;
            /** Finished At */
            finished_at?: number | null;
            /** Done */
            done: boolean;
            /** Summary */
            summary: string;
        };
        /** BackfillWindowModel */
        BackfillWindowModel: {
            /** Index */
            index: number;
            /**
             * Start
             * Format: date
             */
            start: string;
            /**
             * End
             * Format: date
             */
            end: string;
            /** Days */
            days: number;
            /** State */
            state: string;
            /** Requests */
            requests: number;
            /** Transactions Seen */
            transactions_seen: number;
            /** Transactions Added */
            transactions_added: number;
            /** Data From */
            data_from?: string | null;
            /** Data Through */
            data_through?: string | null;
            /** Errors */
            errors: string[];
        };
        /**
         * BudgetLineModel
         * @description One envelope's allocation against its actual spending over a window.
         *
         *     `consumed_ratio` is nullable and that is load-bearing, not laziness: an
         *     allocation of zero has no ratio, and both plausible substitutes are wrong
         *     in opposite directions (0 reads as untouched, 1 as exhausted). `null` is
         *     the only value a client cannot misread, and TypeScript will make it
         *     handle the case.
         *
         *     The type asymmetry on the next three fields is deliberate. A ratio is not
         *     money, so `consumed_ratio` is a genuine JSON number; `overspend` is money
         *     and therefore a `Decimal` rendered as a string. `overspent` is a
         *     server-computed boolean so the UI reads a flag rather than deriving one
         *     by parsing a string -- a browser cannot do `Decimal` arithmetic and must
         *     not try. This mirrors `EnvelopeBalanceModel`, which carries the same pair
         *     for the same reason after the Phase 3 `available` fix.
         */
        BudgetLineModel: {
            /** Name */
            name: string;
            /** Allocated */
            allocated: string;
            /** Spent */
            spent: string;
            /** Remaining */
            remaining: string;
            /** Consumed Ratio */
            consumed_ratio?: number | null;
            /** Status */
            status: string;
            /** Overspent */
            overspent: boolean;
            /** Overspend */
            overspend: string;
            /** Carried In */
            carried_in: string;
            /** Balance */
            balance: string;
        };
        /** BudgetReportResponse */
        BudgetReportResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /**
             * From Date
             * Format: date
             */
            from_date: string;
            /**
             * To Date
             * Format: date
             */
            to_date: string;
            /** Currency */
            currency: string;
            /** Envelopes */
            envelopes: components["schemas"]["BudgetLineModel"][];
            /** Total Allocated */
            total_allocated: string;
            /** Total Spent */
            total_spent: string;
            /** Total Remaining */
            total_remaining: string;
            /** Total Overspend */
            total_overspend: string;
            /** Unmapped Total */
            unmapped_total: string;
            /** Unmapped Accounts */
            unmapped_accounts: string[];
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /** CategorizableAccountsResponse */
        CategorizableAccountsResponse: {
            /** Accounts */
            accounts: string[];
        };
        /** CategorizeRequest */
        CategorizeRequest: {
            /**
             * Apply
             * @default false
             */
            apply: boolean;
            /** Limit */
            limit?: number | null;
            /**
             * Use Llm
             * @default true
             */
            use_llm: boolean;
        };
        /** CategorizeResponse */
        CategorizeResponse: {
            /** Ok */
            ok: boolean;
            /** Applied */
            applied: boolean;
            /** Summary */
            summary: string;
            result: components["schemas"]["CategorizeResultModel"];
        };
        /** CategorizeResultModel */
        CategorizeResultModel: {
            /** Ok */
            ok: boolean;
            policy: components["schemas"]["AutoApplyPolicyModel"];
            /** Applied */
            applied: boolean;
            /** Decisions */
            decisions: components["schemas"]["DecisionModel"][];
            /** Files Written */
            files_written: string[];
            commit?: components["schemas"]["CommitModel"] | null;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /**
         * CommitModel
         * @description One auto-commit attempt, mirroring `categorize.gitcommit.CommitResult`.
         *
         *     Surfaced rather than swallowed because git *is* the undo system (§9): a
         *     UI that has just written forty transactions needs the sha to tell the
         *     user what to revert.
         */
        CommitModel: {
            /** Ok */
            ok: boolean;
            /** Committed */
            committed: boolean;
            /** Sha */
            sha: string;
            /** Message */
            message: string;
            /** Files */
            files: string[];
            /** Warnings */
            warnings: string[];
        };
        /** ConfirmRequest */
        ConfirmRequest: {
            /** Confirmations */
            confirmations: components["schemas"]["ConfirmationModel"][];
        };
        /** ConfirmResponse */
        ConfirmResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Confirmed */
            confirmed: number;
            /** Learned */
            learned: number;
            /** Files Written */
            files_written: string[];
            commit?: components["schemas"]["CommitModel"] | null;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /**
         * ConfirmationModel
         * @description One human decision. Keyed on `(asset_account, simplefin_id)`.
         *
         *     Both halves are required because SimpleFIN ids are unique per account,
         *     not globally (`ingest/dedup.py`) -- the id alone would address two
         *     different transactions at two different banks.
         */
        ConfirmationModel: {
            /** Asset Account */
            asset_account: string;
            /** Simplefin Id */
            simplefin_id: string;
            /** Account */
            account: string;
        };
        /**
         * CurrencyTotalModel
         * @description What the matches add up to, in one currency, in each direction.
         *
         *     Never one scalar. A match set spans accounts, currencies and both
         *     directions, and a lone figure labelled "total" over that mixture is a
         *     claim someone would act on and that would be false. `spent` and
         *     `received` are kept apart so a refund cannot quietly answer "how much did
         *     I spend" with a smaller number than was spent; `transferred` is money
         *     moved between the user's own accounts and is in neither, because the
         *     search returns both legs of a transfer and counting them would report the
         *     same dollar twice.
         */
        CurrencyTotalModel: {
            /** Currency */
            currency: string;
            /** Spent */
            spent: string;
            /** Received */
            received: string;
            /** Net */
            net: string;
            /** Transferred */
            transferred: string;
            /** Spend Count */
            spend_count: number;
            /** Receipt Count */
            receipt_count: number;
            /** Transfer Count */
            transfer_count: number;
            /** Accounts */
            accounts: string[];
        };
        /**
         * DecisionModel
         * @description One prediction and what happened to it, mirroring `apply.Decision`.
         */
        DecisionModel: {
            /** Simplefin Id */
            simplefin_id: string;
            /** Asset Account */
            asset_account: string;
            /** Posted Date */
            posted_date: string;
            /** Description */
            description: string;
            /** Amount */
            amount: string;
            /** Currency */
            currency: string;
            /** Disposition */
            disposition: string;
            /** Suggested Account */
            suggested_account?: string | null;
            /** Confidence */
            confidence?: number | null;
            /** Tier */
            tier?: string | null;
            /**
             * Rationale
             * @default
             */
            rationale: string;
            /** Mcc */
            mcc?: string | null;
            /** Payee */
            payee?: string | null;
        };
        /** EnvelopeBalanceModel */
        EnvelopeBalanceModel: {
            /** Name */
            name: string;
            /** Allocated */
            allocated: string;
            /** Spent */
            spent: string;
            /** Balance */
            balance: string;
            /** Overspent */
            overspent: boolean;
            /** Overspend */
            overspend: string;
        };
        /** EnvelopeReportResponse */
        EnvelopeReportResponse: {
            /**
             * Asof
             * Format: date
             */
            asof: string;
            /** Envelopes */
            envelopes: components["schemas"]["EnvelopeBalanceModel"][];
            /** Budgeted Cash */
            budgeted_cash: string;
            /** Total Envelope Balance */
            total_envelope_balance: string;
            /** Total Overspend */
            total_overspend: string;
            /** Available */
            available: string;
            /** Summary */
            summary: string;
        };
        /** EnvelopeSeriesModel */
        EnvelopeSeriesModel: {
            /** Name */
            name: string;
            /** Total */
            total: string;
            /** Points */
            points: components["schemas"]["SpendPointModel"][];
        };
        /**
         * EnvelopeTrendModel
         * @description One envelope's direction, with the figures the verdict was read from.
         *
         *     `slope`, `mean` and `relative_slope` are returned as the values actually
         *     used, so a client can recompute the classification instead of taking it
         *     on faith. They are `Decimal` and therefore JSON strings: a trend
         *     statistic is derived from money and must not be finished in a float in
         *     the browser.
         *
         *     `direction` is one of `up`, `down`, `flat`, `insufficient_data`. The
         *     fourth is abstention and is *not* `flat`: `flat` means the slope was
         *     measured and is small, which is a finding a user acts on differently
         *     from "we don't know yet". It is a fourth enum value rather than a null
         *     `direction` so a client is never asked to infer abstention from an
         *     absence. `reason`, `periods_observed` and `periods_required` make the
         *     verdict interrogable either way.
         */
        EnvelopeTrendModel: {
            /** Name */
            name: string;
            /** Direction */
            direction: string;
            /** Periods Observed */
            periods_observed: number;
            /** Periods Required */
            periods_required: number;
            /** Total */
            total: string;
            /** Mean */
            mean: string;
            /** Slope */
            slope: string;
            /** Relative Slope */
            relative_slope?: string | null;
            /** Reason */
            reason: string;
            /** Points */
            points: components["schemas"]["SpendPointModel"][];
        };
        /**
         * FindingModel
         * @description One thing that is, or might be, wrong.
         *
         *     `delta` is this finding's share of `statement - ledger`. `confirmed`
         *     separates observed from inferred, and a client must render the two
         *     differently: confirmed findings are components that sum to `explained`,
         *     unconfirmed ones are alternatives to each other and summing them is
         *     meaningless. Collapsing the distinction in a UI would present a guess with
         *     the authority of a measurement.
         */
        FindingModel: {
            /** Kind */
            kind: string;
            /** Delta */
            delta: string;
            /** Explanation */
            explanation: string;
            /** Confirmed */
            confirmed: boolean;
            /** Ledger Entries */
            ledger_entries: components["schemas"]["LedgerEntryModel"][];
            /** Statement Lines */
            statement_lines: components["schemas"]["StatementLineModel-Output"][];
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /** HealthResponse */
        HealthResponse: {
            /** Status */
            status: string;
            /** Beancount Version */
            beancount_version: string;
            /** Root */
            root: string;
        };
        /** LedgerAccountModel */
        LedgerAccountModel: {
            /** Account */
            account: string;
            /** Balance */
            balance: components["schemas"]["AccountPositionModel"][];
        };
        /**
         * LedgerEntryModel
         * @description A ledger posting implicated in a finding.
         *
         *     `location` is `file:line`. It is the field that makes a finding
         *     actionable: a UI can print it and a user can open it, which is the
         *     difference between "there is a duplicate somewhere" and a fix.
         */
        LedgerEntryModel: {
            /**
             * Date
             * Format: date
             */
            date: string;
            /** Amount */
            amount: string;
            /** Description */
            description: string;
            /** Simplefin Id */
            simplefin_id?: string | null;
            /** Location */
            location: string;
        };
        /**
         * MonthEndEnvelopeModel
         * @description One envelope's month.
         *
         *     `overspent` and `over_budget` are two different failures. Overspent means
         *     the running balance is negative — money already spent with nothing behind
         *     it. Over budget means this month's spend exceeded this month's allocation,
         *     which an envelope with a healthy carried balance can do without ever going
         *     negative. A client that collapsed them would raise a false alarm or hide a
         *     real one.
         */
        MonthEndEnvelopeModel: {
            /** Name */
            name: string;
            /** Opening Balance */
            opening_balance: string;
            /** Allocated */
            allocated: string;
            /** Spent */
            spent: string;
            /** Closing Balance */
            closing_balance: string;
            /** Remaining */
            remaining: string;
            /** Overspend */
            overspend: string;
            /** Consumed Ratio */
            consumed_ratio?: number | null;
            /** Status */
            status: string;
            /** Direction */
            direction: string;
            /** Direction Reason */
            direction_reason: string;
            /**
             * Periods Observed
             * @default 0
             */
            periods_observed: number;
            /**
             * Periods Required
             * @default 0
             */
            periods_required: number;
            /** Overspent */
            overspent: boolean;
            /** Over Budget */
            over_budget: boolean;
        };
        /**
         * MonthEndOutlierModel
         * @description One transaction that is unusual for its envelope.
         *
         *     Carries `median`, `scale` and `scale_method` with it so `score` can be
         *     recomputed from this one record. A flag whose basis is not visible is not
         *     a finding a user can check.
         */
        MonthEndOutlierModel: {
            /** Envelope */
            envelope: string;
            /**
             * Posted Date
             * Format: date
             */
            posted_date: string;
            /** Description */
            description: string;
            /** Amount */
            amount: string;
            /** Score */
            score: string;
            /** Median */
            median: string;
            /** Scale */
            scale: string;
            /** Scale Method */
            scale_method: string;
            /** Threshold */
            threshold: string;
        };
        /** MonthEndReportResponse */
        MonthEndReportResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Month */
            month: string;
            /** Label */
            label: string;
            /**
             * From Date
             * Format: date
             */
            from_date: string;
            /**
             * To Date
             * Format: date
             */
            to_date: string;
            /**
             * Asof
             * Format: date
             */
            asof: string;
            /** Coverage */
            coverage: string;
            /** Data Through */
            data_through?: string | null;
            /** Through */
            through?: string | null;
            /** Complete */
            complete: boolean;
            /** Days Elapsed */
            days_elapsed: number;
            /** Days In Month */
            days_in_month: number;
            /** Transactions */
            transactions: number;
            /** Categorized Count */
            categorized_count: number;
            /** Uncategorized Count */
            uncategorized_count: number;
            /** Currency */
            currency: string;
            /** Envelopes */
            envelopes: components["schemas"]["MonthEndEnvelopeModel"][];
            /** Opening Total */
            opening_total: string;
            /** Allocated Total */
            allocated_total: string;
            /** Spent Total */
            spent_total: string;
            /** Closing Total */
            closing_total: string;
            /** Unmapped Total */
            unmapped_total: string;
            /** Unmapped Accounts */
            unmapped_accounts: string[];
            /** Total Spend */
            total_spend: string;
            /** Categorization */
            categorization: string;
            /** Categorized Share */
            categorized_share: string;
            /** Budgeted Cash */
            budgeted_cash: string;
            /** Available */
            available: string;
            /** Total Overspend */
            total_overspend: string;
            /** Outliers */
            outliers: components["schemas"]["MonthEndOutlierModel"][];
            /** Trend From */
            trend_from?: string | null;
            /** Trend To */
            trend_to?: string | null;
            /** Unjudged */
            unjudged: string[];
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /**
         * OutlierAssessmentModel
         * @description Whether an envelope was judged for outliers at all, and on what basis.
         *
         *     Present for every envelope so that "nothing unusual" stays
         *     distinguishable from "not enough data to look". Collapsing the two is how
         *     a report ends up asserting the unfalsifiable "nothing looks unusual".
         */
        OutlierAssessmentModel: {
            /** Envelope */
            envelope: string;
            /** Sample Size */
            sample_size: number;
            /** Judged */
            judged: boolean;
            /** Reason */
            reason: string;
            /** Median */
            median?: string | null;
            /** Scale */
            scale?: string | null;
            /**
             * Scale Method
             * @default
             */
            scale_method: string;
            /**
             * Outliers Found
             * @default 0
             */
            outliers_found: number;
        };
        /**
         * OutlierModel
         * @description One transaction that is unusual for its envelope.
         *
         *     Deliberately self-contained: `median`, `scale` and `threshold` travel
         *     with the flag so `(amount - median) / scale` can be recomputed from a
         *     single card. An outlier a user cannot interrogate is worse than none, and
         *     a chat surface renders these one at a time.
         */
        OutlierModel: {
            /** Envelope */
            envelope: string;
            /**
             * Posted Date
             * Format: date
             */
            posted_date: string;
            /** Description */
            description: string;
            /** Amount */
            amount: string;
            /** Score */
            score: string;
            /** Median */
            median: string;
            /** Scale */
            scale: string;
            /** Scale Method */
            scale_method: string;
            /** Threshold */
            threshold: string;
        };
        /**
         * ReconcileRequest
         * @description A statement to check the ledger against.
         *
         *     Every field but `account` is optional, and the useful combinations are all
         *     of them. The minimum -- `closing_balance` + `closing_date`, no lines -- is
         *     the case a user can always supply ("on this date my balance was X"), and
         *     it still comes back with candidate transactions rather than a scalar.
         *
         *     `lines` is the API's equivalent of the CLI's `--statement file.csv`: the
         *     sidecar deliberately does *not* accept a filesystem path from the browser
         *     side, since the Next.js layer never touches the user's disk on the
         *     sidecar's behalf (§9). The CSV parser lives in `reconcile.statement` and
         *     serves the CLI; over HTTP a caller sends the parsed lines.
         */
        ReconcileRequest: {
            /** Account */
            account: string;
            /** Closing Date */
            closing_date?: string | null;
            /** Closing Balance */
            closing_balance?: number | string | null;
            /**
             * Lines
             * @default []
             */
            lines: components["schemas"]["StatementLineModel-Input"][];
            /** From Date */
            from_date?: string | null;
            /** Count */
            count?: number | null;
            /**
             * Window
             * @default 3
             */
            window: number;
            /**
             * Source
             * @default api
             */
            source: string;
        };
        /** ReconcileResponse */
        ReconcileResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Account */
            account: string;
            /** Currency */
            currency: string;
            /** Source */
            source: string;
            /** Closing Date */
            closing_date?: string | null;
            /** Assertion Date */
            assertion_date?: string | null;
            /** Statement Balance */
            statement_balance?: string | null;
            /** Ledger Balance */
            ledger_balance?: string | null;
            /** Delta */
            delta?: string | null;
            /** Explained */
            explained: string;
            /** Residual */
            residual: string;
            /** Reconciled */
            reconciled: boolean;
            /** From Date */
            from_date?: string | null;
            /** To Date */
            to_date?: string | null;
            /** Statement Lines */
            statement_lines: number;
            /** Ledger Entries */
            ledger_entries: number;
            /** Matched */
            matched: number;
            /** Findings */
            findings: components["schemas"]["FindingModel"][];
            /** Notes */
            notes: string[];
            /** Errors */
            errors: string[];
        };
        /**
         * ReviewEntryModel
         * @description One transaction awaiting a decision, mirroring `review.ReviewEntry`.
         *
         *     `simplefin_id` is the key `POST /review/confirm` batches on, so it is
         *     typed rather than left to a `dict[str, Any]` that TypeScript would widen
         *     to `unknown`: a typo in that field silently confirms nothing, and the
         *     user reads that as "I approved 40 transactions and nothing happened"
         *     against their own financial records.
         *
         *     `amount` is a *string* here, not a `Decimal`, because `ReviewEntry`
         *     already carries it as one -- primitives only, so the queue crosses to
         *     the browser with no encoder in between.
         */
        ReviewEntryModel: {
            /** Simplefin Id */
            simplefin_id: string;
            /** Asset Account */
            asset_account: string;
            /** Posted Date */
            posted_date: string;
            /** Description */
            description: string;
            /** Amount */
            amount: string;
            /** Currency */
            currency: string;
            /** Current Account */
            current_account: string;
            /** Suggested Account */
            suggested_account?: string | null;
            /** Confidence */
            confidence?: number | null;
            /** Tier */
            tier?: string | null;
            /**
             * Rationale
             * @default
             */
            rationale: string;
            /** Mcc */
            mcc?: string | null;
            /** Payee */
            payee?: string | null;
        };
        /** ReviewQueueModel */
        ReviewQueueModel: {
            /** Ok */
            ok: boolean;
            /** Entries */
            entries: components["schemas"]["ReviewEntryModel"][];
            /** Total */
            total: number;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /** ReviewQueueResponse */
        ReviewQueueResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            queue: components["schemas"]["ReviewQueueModel"];
        };
        /** SpendPointModel */
        SpendPointModel: {
            /** Period */
            period: string;
            /** Amount */
            amount: string;
        };
        /** SpendingReportResponse */
        SpendingReportResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /**
             * From Date
             * Format: date
             */
            from_date: string;
            /**
             * To Date
             * Format: date
             */
            to_date: string;
            /** Period */
            period: string;
            /** Currency */
            currency: string;
            /** Periods */
            periods: string[];
            /** Envelopes */
            envelopes: components["schemas"]["EnvelopeSeriesModel"][];
            /** Total */
            total: string;
            /** Unmapped Total */
            unmapped_total: string;
            /** Unmapped Accounts */
            unmapped_accounts: string[];
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /**
         * StatementLineModel
         * @description One transaction as the bank recorded it.
         *
         *     `amount` is a `Decimal` (a JSON *string* on the wire) in the ledger's own
         *     sign convention: negative is money leaving the account. A browser cannot
         *     do decimal arithmetic and must not try, so this is the one place a caller
         *     has to get the sign right -- and `POST /reconcile` says so when a whole
         *     payload looks inverted rather than silently negating it.
         */
        "StatementLineModel-Input": {
            /**
             * Posted Date
             * Format: date
             */
            posted_date: string;
            /** Amount */
            amount: number | string;
            /**
             * Description
             * @default
             */
            description: string;
            /**
             * Row
             * @default 0
             */
            row: number;
        };
        /**
         * StatementLineModel
         * @description One transaction as the bank recorded it.
         *
         *     `amount` is a `Decimal` (a JSON *string* on the wire) in the ledger's own
         *     sign convention: negative is money leaving the account. A browser cannot
         *     do decimal arithmetic and must not try, so this is the one place a caller
         *     has to get the sign right -- and `POST /reconcile` says so when a whole
         *     payload looks inverted rather than silently negating it.
         */
        "StatementLineModel-Output": {
            /**
             * Posted Date
             * Format: date
             */
            posted_date: string;
            /** Amount */
            amount: string;
            /**
             * Description
             * @default
             */
            description: string;
            /**
             * Row
             * @default 0
             */
            row: number;
        };
        /**
         * SyncJobResult
         * @description What a finished sync job produced.
         *
         *     Typed concretely rather than left as a free dict because this is the
         *     payload `GET /sync/status/{job_id}` hands the browser, and an untyped
         *     `result` is exactly the `unknown` that costs the web layer its safety.
         *     The endpoint 404s a job of any other kind, so this stays honest.
         */
        SyncJobResult: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Accounts Synced */
            accounts_synced: number;
            /** Transactions Seen */
            transactions_seen: number;
            /** Transactions Added */
            transactions_added: number;
            /** Pending Skipped */
            pending_skipped: number;
            /** Balances Written */
            balances_written: number;
            /** Opening Balances Written */
            opening_balances_written: number;
        };
        /** SyncRequest */
        SyncRequest: {
            /** Since */
            since?: string | null;
            /**
             * Demo
             * @default false
             */
            demo: boolean;
        };
        /** SyncResponse */
        SyncResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Accounts Synced */
            accounts_synced: number;
            /** Transactions Added */
            transactions_added: number;
            /** Balances Written */
            balances_written: number;
        };
        /** SyncStartRequest */
        SyncStartRequest: {
            /** Since */
            since?: string | null;
            /**
             * Demo
             * @default false
             */
            demo: boolean;
        };
        /** SyncStartResponse */
        SyncStartResponse: {
            /** Job Id */
            job_id: string;
            /** Kind */
            kind: string;
            /** State */
            state: string;
            /** Started */
            started: boolean;
        };
        /** SyncStatusResponse */
        SyncStatusResponse: {
            /** Job Id */
            job_id: string;
            /** Kind */
            kind: string;
            /** State */
            state: string;
            /** Progress */
            progress: number;
            /** Total */
            total: number;
            /** Step */
            step: string;
            result?: components["schemas"]["SyncJobResult"] | null;
            /** Error */
            error?: string | null;
            /** Started At */
            started_at: number;
            /** Finished At */
            finished_at?: number | null;
            /** Done */
            done: boolean;
            /** Summary */
            summary: string;
        };
        /** TransactionMatchModel */
        TransactionMatchModel: {
            /**
             * Posted Date
             * Format: date
             */
            posted_date: string;
            /** Description */
            description: string;
            /** Amount */
            amount: string;
            /** Currency */
            currency: string;
            /** Account */
            account: string;
            /** Categorized Account */
            categorized_account?: string | null;
            /** Envelope */
            envelope?: string | null;
            /** Simplefin Id */
            simplefin_id?: string | null;
            /** Payee */
            payee?: string | null;
            /** Memo */
            memo?: string | null;
        };
        /** TransactionSearchResponse */
        TransactionSearchResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Query */
            query: string;
            /** Matches */
            matches: components["schemas"]["TransactionMatchModel"][];
            /** Total */
            total: number;
            /** Amount Totals */
            amount_totals: components["schemas"]["CurrencyTotalModel"][];
            /** Mixed Currency */
            mixed_currency: boolean;
            /** Limit */
            limit: number;
            /** Truncated */
            truncated: boolean;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /** TrendsReportResponse */
        TrendsReportResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /**
             * From Date
             * Format: date
             */
            from_date: string;
            /**
             * To Date
             * Format: date
             */
            to_date: string;
            /** Currency */
            currency: string;
            /** Periods */
            periods: string[];
            /** Envelopes */
            envelopes: components["schemas"]["EnvelopeTrendModel"][];
            /** Outliers */
            outliers: components["schemas"]["OutlierModel"][];
            /** Assessments */
            assessments: components["schemas"]["OutlierAssessmentModel"][];
            /** Unmapped Total */
            unmapped_total: string;
            /** Unmapped Accounts */
            unmapped_accounts: string[];
            /** Unmapped Transactions */
            unmapped_transactions: number;
            /** Min Periods */
            min_periods: number;
            /** Flat Band */
            flat_band: string;
            /** Min Transactions */
            min_transactions: number;
            /** Outlier Threshold */
            outlier_threshold: string;
            /** Errors */
            errors: string[];
            /** Warnings */
            warnings: string[];
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
        /** VerifyResponse */
        VerifyResponse: {
            /** Ok */
            ok: boolean;
            /** Summary */
            summary: string;
            /** Errors */
            errors: string[];
            /** Notes */
            notes: string[];
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    health_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HealthResponse"];
                };
            };
        };
    };
    sync_sync_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SyncRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    accounts_accounts_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AccountsResponse"];
                };
            };
        };
    };
    envelopes_envelopes_get: {
        parameters: {
            query?: {
                asof?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EnvelopeReportResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    verify_verify_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VerifyResponse"];
                };
            };
        };
    };
    review_queue_review_queue_get: {
        parameters: {
            query?: {
                limit?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReviewQueueResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    categorize_categorize_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CategorizeRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CategorizeResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    categorizable_accounts_accounts_categorizable_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CategorizableAccountsResponse"];
                };
            };
        };
    };
    review_confirm_review_confirm_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfirmRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfirmResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    sync_start_sync_start_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SyncStartRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncStartResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    sync_status_sync_status__job_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncStatusResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backfill_plan_backfill_plan_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackfillRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackfillResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backfill_start_backfill_start_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackfillRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackfillStartResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    backfill_status_backfill_status__job_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackfillStatusResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    envelopes_allocate_envelopes_allocate_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AllocateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AllocateResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    transactions_search_transactions_search_get: {
        parameters: {
            query: {
                q: string;
                limit?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TransactionSearchResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reports_spending_reports_spending_get: {
        parameters: {
            query?: {
                from?: string | null;
                to?: string | null;
                period?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SpendingReportResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reports_month_end_reports_month_end_get: {
        parameters: {
            query?: {
                month?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MonthEndReportResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reports_budget_reports_budget_get: {
        parameters: {
            query?: {
                from?: string | null;
                to?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BudgetReportResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reports_trends_reports_trends_get: {
        parameters: {
            query?: {
                from?: string | null;
                to?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TrendsReportResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reconcile_reconcile_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReconcileRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReconcileResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
