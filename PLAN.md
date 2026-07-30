# bookkeeper — Implementation Plan

An AI-assisted envelope-budgeting bookkeeper. Plain-text **beancount** ledger,
**SimpleFIN** for bank data, a **local LLM** on Apple Silicon, and a **chat
interface** built from the Vercel AI Chatbot template as the primary surface.

---

## 1. What we are building

A single-user, localhost-only tool the user talks to:

> **you:** how am I doing on groceries this month?
> **bot:** *[renders an envelope card: $412 of $600, 11 days left, on pace]*
>
> **you:** sync my accounts
> **bot:** *[pulls SimpleFIN, imports 43 transactions, categorizes]*
> 37 placed automatically. 6 I'm unsure about — *[renders review cards]*

Underneath: SimpleFIN pulls transactions, a tiered categorizer assigns them to
expense accounts, envelope state is computed from `custom` directives, and
reports come from beanquery. Fava remains available, unmodified, as an optional
read-only ledger browser.

---

## 2. Confirmed requirements

All six interview questions answered by the user:

| # | Decision |
|---|---|
| 1 | **Chat is the primary interface**, built from [vercel/ai-chatbot](https://github.com/vercel/ai-chatbot) (Next.js + AI SDK). |
| 2 | **Greenfield ledger**, bootstrapped by backfilling history from SimpleFIN. |
| 3 | **Envelopes as `custom` directives**, fava-envelope style. Ledger stays clean; envelope state is computed. |
| 4 | **Apple Silicon, 16GB.** Practical ceiling ~8B, 14B only when little else is running. |
| 5 | **Review-everything at first**, raising to threshold auto-apply once accuracy is measured. |
| 6 | **Single user, localhost only.** No auth, no multi-tenancy; secrets are a `0600` file. |

---

## 3. Constraints discovered during research

These drive most of §5. Each is sourced.

### 3.1 SimpleFIN gives us almost nothing to work with

Per the [SimpleFIN protocol](https://www.simplefin.org/protocol.html), a
transaction is `id`, `posted`, `amount`, `description`, optional `transacted_at`
/ `pending` / `extra`. That's it.

- **No merchant category codes, no structured merchant identity.** We get a
  short, bank-mangled string — `SQ *COFFEE 4TH ST 8829`, `ACH DEBIT - PG&E WEB
  ONLINE` — and a signed amount. Categorization is therefore short-text
  classification over a closed label set. This is the single most important fact
  about the AI component and §5.4 is built around it.

  > **Amendment (verified live, 2026-07-30).** True of the *specification*, but
  > the running bridge emits more than the spec defines. A live call returns
  > `payee`, `memo`, and **`mcc`** (e.g. `5411` for a grocery store) — none of
  > which appear anywhere in `protocol.html`. The documented `extra` object can
  > also carry `{"category": "food"}` per the spec's own example.
  >
  > These are **undocumented and not guaranteed by any real institution**, so
  > the §5.4 cascade stands as designed and nothing may *require* them. Capture
  > them as optional metadata: where `mcc` is present it is a high-precision
  > deterministic signal that should short-circuit ahead of the LLM, and where
  > it is absent the cascade degrades to exactly the design already specified.
  > Phase 3 should measure MCC coverage on real data before leaning on it.
- **`id` is unique per account and never reused.** A free, exact idempotency key.
  We do not need the fuzzy candidate-matching machinery that
  [beancount-import](https://github.com/jbms/beancount-import) builds for CSV and
  OFX sources.
- **Auth**: a base64 setup token decodes to a claim URL → one-time `POST` → an
  Access URL with embedded basic-auth credentials. Single-use; a 403 on claim
  means already-claimed and should be treated as possible compromise. The Access
  URL is a long-lived banking credential.
- **Rate limit: ~24 requests/day.** Hourly sync is the ceiling. This kills any
  design that polls aggressively or loops per-account.
- **90-day window cap.** A request spanning more than 90 days returns
  `"errors": ["Requested date range exceeds limit of 90 days and was capped."]`
  *alongside* a normal 200 response body. Soft errors arrive in the top-level
  `errors` array and must be surfaced, not ignored. Phase 6 backfill has to
  paginate in ≤90-day windows.
- **Dev server**: the bridge exposes a reusable demo token
  (`aHR0cHM6Ly9icmlkZ2Uuc2ltcGxlZmluLm9yZy9zaW1wbGVmaW4vY2xhaW0vZGVtbw==`)
  returning synthetic accounts. Phases 0–5 develop entirely against it; no real
  bank credentials until Phase 6.

  > **Amendment (verified live, 2026-07-30).** The bridge has migrated to
  > `beta-bridge.simplefin.org`, and the redirect from the old host **drops the
  > path** (`Location` is bare root), so following it lands on a marketing page.
  > Claiming the demo token now returns `403 Forbidden` despite the docs calling
  > it reusable — treat live claim as externally broken.
  >
  > Development instead uses the demo **Access URL** published in
  > `protocol.html`: `https://demo:demo@beta-bridge.simplefin.org/simplefin`.
  > An Access URL is self-describing and carries its own host, so this needs no
  > domain-rewrite hack — the system simply accepts a supplied Access URL, which
  > is the real user flow regardless. The claim path stays implemented to spec.

### 3.2 16GB is the binding constraint on everything

Roughly 70–75% of unified memory is usable for model weights, so ~11–12GB on a
16GB Mac — and that is *before* this app's own footprint. A rough budget:

| Consumer | Estimate |
|---|---|
| macOS + browser + editor | ~4–5 GB |
| Next.js dev server | ~1 GB |
| Python sidecar (beancount loaded) | ~0.5 GB |
| **Left for the model** | **~8–9 GB** |

An 8B at Q4_K_M is ~5GB of weights plus 1–2GB of KV cache — comfortable. A 14B
at Q4 is ~9GB, which fits only if nothing else is running. Two consequences:

- **Default to an 8B model**, with 14B as an opt-in for batch work when the UI
  is closed.
- **One model serves both chat and categorization.** Loading a separate small
  classifier would make Ollama thrash between models on every request. Any
  memory we free elsewhere (see the Postgres→SQLite decision, §5.6) goes to the
  model.

### 3.3 Small models fail at tool calling in specific, designable-around ways

This is the most important finding for the chat interface, and it is worse than
folklore suggests. From [2026 local tool-calling
benchmarks](https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/)
and [agentic guardrail
work](https://dev.to/monuminu/llm-agent-guardrails-the-engineering-playbook-for-taking-an-8b-local-model-from-53-to-99-on-18c):

- **Model choice at 8B swings failure rate by 6×.** Llama-3.1-8B fails ~76.6% of
  tool-calling tasks; **Qwen3-8B fails ~13.0%.** Same size class. This alone
  decides the model.
- **Errors compound across steps.** 90% per-step accuracy over a 5-step chain is
  a ~40% overall failure rate. Deep tool chains are not viable here.
- **Multi-turn degrades hard.** Llama-3.1-8B loses a large share of tasks it
  solved at turn 1 by turn 8.
- **Small models are unreliable at deciding *whether* to call a tool** versus
  replying in prose, and fall into invocation loops when error handling is loose.

The design response (§5.3): keep the model's job **shallow and single-step**,
never route deterministic actions through it, and cap the tool surface.

### 3.4 Beancount and the template

- **Beancount v3** is current; `beancount.ingest` split out into
  [beangulp](https://github.com/beancount/beangulp), queries into `beanquery`.
- [smart_importer](https://github.com/beancount/smart_importer) gives
  scikit-learn account prediction — our tier-3 baseline and the accuracy floor
  the LLM must beat.
- [fava-envelope](https://github.com/polarmutex/fava-envelope) uses a JSON config
  in a `custom "fava-extension"` directive plus per-category allocations:
  `2015-01-01 custom "envelope" "allocate" "Expenses:Health:Dental" 5.80`.
- **Fava's extension API self-describes as "unstable and it might change
  drastically."** We do not build on it. Fava runs unmodified or not at all.
- The **vercel/ai-chatbot** template ships Auth.js, Neon Postgres + Drizzle,
  Vercel Blob storage, AI Gateway provider routing, shadcn/ui, and — importantly
  — tool calling with generative UI and an artifacts system. §5.6 covers what to
  strip.

---

## 4. Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Next.js  (vercel/ai-chatbot, stripped)      :3000       │
│                                                          │
│   chat UI ── AI SDK ──► Ollama  :11434  (Qwen3 8B)       │
│      │                                                   │
│      │ tool calls (shallow, 1 step)                      │
│      ▼                                                   │
│   generative UI: ReviewCard · EnvelopeCard · ReportChart  │
│      │                                                   │
│      │ button clicks bypass the LLM entirely ──────┐     │
└──────┼─────────────────────────────────────────────┼─────┘
       │ HTTP (typed, OpenAPI)                       │
       ▼                                             ▼
┌──────────────────────────────────────────────────────────┐
│  Python sidecar (FastAPI)                    :8000       │
│                                                          │
│  ledger cache (loaded once, mtime-invalidated)           │
│  simplefin · ingest · categorize · envelope · reports    │
│  SOLE WRITER to the ledger                               │
└──────┬────────────────────────────┬──────────────────────┘
       │                            │
       ▼                            ▼
┌─────────────┐            ┌──────────────────┐
│ SimpleFIN   │            │ ledger/*.beancount│──► Fava
│ (demo→real) │            │ git-versioned     │   (read-only,
└─────────────┘            └──────────────────┘    optional)
```

### Repository layout

```
web/                      # Next.js, forked from vercel/ai-chatbot
  lib/ai/tools/           # the 6 tools of §5.3
  components/bookkeeper/  # ReviewCard, EnvelopeCard, ReportChart
  lib/db/                 # Drizzle, SQLite dialect
sidecar/
  bookkeeper/
    simplefin/            # claim, fetch, models
    ingest/               # normalize, dedup, render beancount
    categorize/           # tiers 1-4, confidence, evaluation harness
    envelope/             # directive parsing, computation, validation
    reports/              # beanquery wrappers
    api.py                # FastAPI
    cli.py                # same operations, headless
ledger/
  main.beancount          # options, includes
  accounts.beancount      # open directives, envelope mapping
  budget.beancount        # custom "envelope" allocate directives
  transactions/YYYY.beancount
  balances.beancount      # generated balance assertions
data/
  raw/simplefin-<ts>.json # immutable fetch archive
  memory.json             # learned description → account
  rules.yaml              # user-authored patterns
  eval/                   # held-out labeled set, accuracy reports
```

---

## 5. Key design decisions

### 5.1 The Python/TypeScript boundary: a long-lived Python sidecar

**This is the central architectural decision.** Chat is TypeScript; beancount is
Python. Three options:

| Option | Verdict |
|---|---|
| **Reimplement beancount in TS** | No. The parser, plugin system, and beanquery are large and correctness-critical. Silently diverging from upstream on a financial ledger is the worst outcome available. |
| **Shell out to `bean-query` per request** | No. `loader.load_file` costs seconds on a real ledger and would be paid *on every chat turn*. No caching, no typed contract, errors arrive as stderr text to be regex-parsed, and concurrent invocations can interleave writes. |
| **Long-lived FastAPI sidecar** | **Yes.** |

The sidecar wins on four specific grounds:

1. **Load cost amortization.** Beancount parsing is the dominant latency in any
   ledger operation. A resident process loads once and invalidates on mtime, so
   chat turns hit warm state. This is the difference between a responsive
   assistant and one that stalls for seconds before the model even starts.
2. **Single writer.** The sidecar is the *only* process that writes ledger files.
   Next.js never touches them. This removes a whole class of concurrent-write
   corruption on the user's financial records.
3. **Typed contract.** FastAPI emits OpenAPI; we generate TS types from it, so
   the tool layer is type-checked end to end rather than passing untyped JSON.
4. **The ecosystem is Python.** beancount, beangulp, beanquery, smart_importer,
   fava — all of it. Anything else means reimplementing or subprocessing.

Cost: two processes to supervise. Mitigated with a single `just dev` (or
`docker compose`) that starts Ollama, sidecar, and Next.js together. The sidecar
also exposes the same operations as a CLI, so the whole system remains usable and
scriptable with the chat UI down — which matters for debugging and for cron sync.

### 5.2 Envelopes as computed state from `custom` directives

Per the user's decision, envelopes are **not** real postings. The ledger holds
only real transactions plus declarative directives:

```beancount
; mapping: which expense accounts roll up to which envelope
2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"  "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Dining"     "Dining Out"

; allocations
2026-08-01 custom "envelope" "allocate" "Groceries"   600.00 USD
2026-08-01 custom "envelope" "allocate" "Dining Out"  200.00 USD
```

Envelope state is a **pure function** of (ledger entries, directives):

```
balance(E, asof) = Σ allocate(E, t≤asof) − Σ postings(accounts mapped to E, t≤asof)
available(asof)  = Σ budgeted-cash(asof) − Σ balance(E, asof) over all E
```

Same inputs always produce the same outputs, so it is straightforwardly testable
with golden-file tests over a fixture ledger.

> **Defect in this formula (found on real data, 2026-07-30). Not yet fixed.**
> `available` credits back **negative** envelope balances, but an overspent
> envelope's money has already left the bank — it is not available to re-budget.
>
> Minimal case: cash 100, allocate 50 to Groceries, spend 80. Real cash is 20
> and Groceries is 30 in the hole, so at most 20 can be budgeted. The system
> reports:
> ```
> Budgeted cash:                     20.00
> Envelope balances (total):        -30.00
> Available to budget:               50.00     <-- exceeds cash
> verify: OK                                   <-- guard does not fire
> ```
> Two consequences, the second worse than the first: `available` can exceed
> total cash, and the §5.2 over-allocation check is **silenced**, because
> `available >= 0` passes comfortably. The one guard meant to catch
> "money you don't have" stops working precisely when an envelope is overspent.
>
> Fix direction: negative balances must not credit back —
> `available = Σ budgeted-cash − Σ max(balance(E), 0)`, giving 20 here and
> restoring the guard. Overspend then needs its own explicit surfacing
> (YNAB-style, it is covered from the next period's allocation) rather than
> silently inflating headroom.
>
> Why the fixtures missed it: they covered over-allocation (allocate > cash)
> and the spend-within-allocation false positive, but never an **overspent
> envelope** (spend > allocation). Real data hit it on the first budget.

**Tradeoff, noted once so the decision stays legible:** because envelope state
lives outside the double-entry graph, `bean-check` does not validate it. In a
real-postings design the ledger itself would refuse to balance if envelopes
drifted; here, correctness rests on our validator and tests instead. The upside
the user chose it for is real: the ledger stays clean, portable, and readable by
any beancount tool, and the envelope scheme can be reworked later without
rewriting transaction history.

Since we don't get validation for free, we build it explicitly. `bookkeeper
verify` runs on every sync and in CI:

1. **Unmapped-expense check.** Every expense account with postings must map to
   exactly one envelope. This is *the* silent-drift vector in a computed model:
   spending against an unmapped account simply vanishes from the budget view
   while the ledger stays perfectly valid. Unmapped accounts are a hard error,
   never a silent skip.
2. **Over-allocation check: `available(asof) >= 0`.** You cannot put money into
   envelopes you don't have. This is the common real-world failure and it is
   invisible without an explicit check.

   The check must be `available >= 0`, i.e. `Σ balance(E) <= budgeted cash` — and
   specifically **not** `Σ allocations <= budgeted cash`, which looks equivalent
   but false-positives as soon as any money has been spent. With opening 500,
   income 3000, allocations 3400 and spending 110: cash is 3390 and allocations
   are 3400, so the naive form reports over-allocation, but available is actually
   +100 and the budget is fine. Money already spent out of an envelope must not
   keep counting against the allocation budget. (Both forms were checked against
   worked examples; only the `available >= 0` form is correct.)
3. **Cash reconciliation is unaffected.** SimpleFIN reports `balance` and
   `balance-date` per account; we emit those as beancount `balance` assertions.
   Cash truth is still guaranteed by double-entry regardless of the envelope
   model — if we ever drop or duplicate a transaction, `bean-check` fails at the
   next assertion date rather than the error hiding in a report months later.
4. **Snapshot tests.** Month-end envelope balances are committed as golden files,
   so any change in computation semantics shows up as a reviewable git diff
   rather than a number that quietly shifts.

### 5.3 Chat design: shallow tools, and clicks that bypass the model

Given §3.3 — 8B models compound errors across steps, degrade over turns, and
waver on whether to call a tool at all — the chat layer is built defensively.

**Six tools, deliberately few, each one step, no chaining:**

| Tool | Returns |
|---|---|
| `sync_accounts` | import summary |
| `get_review_queue` | uncategorized transactions + guesses |
| `get_envelope_status` | envelope balances |
| `get_spending_report` | time series by envelope |
| `search_transactions` | matching transactions |
| `allocate_to_envelope` | confirmation (the one write tool) |

Four rules make this work at 8B:

1. **Generative UI, not prose.** Tools return structured data that React renders
   as `ReviewCard`, `EnvelopeCard`, `ReportChart`. The model never recites
   numbers into text, so it cannot transpose a digit. It picks a tool; the data
   path is deterministic.
2. **Actions bypass the LLM entirely.** This is the most important rule. When the
   user clicks *Accept* on a review card, that hits the sidecar API directly —
   it does **not** go back through the model as another turn. Approving 40
   transactions is 40 deterministic HTTP calls, not 40 chances for an 8B model
   to mis-invoke a tool. The model's job is to *summon* the review UI, not to
   mediate every click. This removes the multi-turn degradation of §3.3 from the
   hottest path in the app.
3. **Batch work happens outside the chat turn.** Categorizing 43 transactions at
   ~1–2s each would stall a chat turn for a minute. Sync and categorization run
   as a background job in the sidecar; `sync_accounts` kicks it off and returns
   immediately, and the UI polls for progress. Chat reads results that already
   exist.
4. **Deterministic pre-routing.** Obvious commands (`sync`, `review`) are matched
   before the model sees them and invoke the tool directly. Cheap, instant, and
   it sidesteps the "does this need a tool?" decision the model is worst at.

**Wiring the AI SDK to Ollama.** Ollama exposes an OpenAI-compatible endpoint at
`http://localhost:11434/v1`, so the template's provider config in
`lib/ai/models.ts` can be repointed with a `baseURL` change and a dummy API key.
A dedicated community provider (`ai-sdk-ollama` / `ollama-ai-provider-v2`) is
preferable for streaming tool calls, native option pass-through, and embeddings.
One known gotcha to handle explicitly: **Ollama providers can execute a tool and
then return empty text**, leaving a blank assistant bubble; the tool result must
be rendered by the UI component regardless of whether the model produced
accompanying prose.

> **Amendment (measured on this machine, 2026-07-30).** Benchmarked `qwen3:8b`
> against Ollama with two tools defined and an identical prompt:
>
> | | latency | completion tokens | tool call |
> |---|---|---|---|
> | thinking on (default) | **32 s** | 747 | correct |
> | `think: false` | **2 s** | 24 | correct |
>
> Qwen3 is a reasoning model and spends its whole budget thinking before
> emitting anything. **Thinking must be disabled for interactive chat** — 32 s
> per turn is not a usable assistant.
>
> This promotes provider choice from preference to requirement: `think: false`
> is an **Ollama-native parameter on `/api/chat`** and is *not* accepted by the
> OpenAI-compatible `/v1/chat/completions` endpoint. A plain `baseURL` swap
> therefore cannot turn thinking off. Use a provider with native option
> pass-through, and make thinking a **per-call** option — Phase 3 batch
> categorization may want it *on*, where latency is irrelevant and reasoning may
> help on novel merchant strings.
>
> Two corollaries, both observed: the blank-bubble gotcha above is
> **unconditional** (`content` was empty with `finish_reason: tool_calls` in
> *both* runs, so a successful tool call simply returns no prose); and the
> OpenAI-compat shape returns reasoning in a separate `reasoning` field, so any
> low `max_tokens` cap yields phantom empty responses when thinking consumes it.
>
> Encouragingly, tool selection itself was correct in both runs — given two
> plausible tools it chose `get_envelope_status` with `{"envelope":"groceries"}`.
> The shallow single-step bet holds at 8B.

**Model: Qwen3 8B (Q4_K_M).** Chosen on the §3.3 numbers — ~13% tool-calling
failure versus ~76.6% for Llama-3.1-8B at the same size — and it fits the §3.2
memory budget with room for the rest of the stack. Phi-4 14B is a Phase 5
bake-off candidate for batch categorization when the UI is closed.

### 5.4 Tiered categorization: the LLM handles the tail, not the head

With a 16GB ceiling this is now doubly load-bearing. Real spending is dominated
by recurring merchants, and a memory of past decisions resolves those exactly —
faster, free, deterministic, and auditable. Invoking an 8B model on a string the
user has already categorized forty times would be strictly worse on every axis.

Cascade, first hit wins:

1. **Exact memory.** Normalized description (case-folded, trailing store numbers
   and transaction ids stripped) → previously confirmed account. Confidence 1.0.
   `data/memory.json`, human-readable and git-tracked.
2. **User rules.** Patterns in `rules.yaml` with optional amount/account
   predicates. Encode "any PG&E charge is `Expenses:Home:Utilities`" once.
3. **Statistical.** smart_importer-style classifier over confirmed history.
   Cheap, and it sets the floor the LLM must beat to justify its cost.
4. **Local LLM.** Only for descriptions genuinely unlike anything seen.

Two things make tier 4 reliable rather than a generator of plausible nonsense:

- **Constrain output to the closed set of real accounts.** The JSON Schema passed
  to Ollama's `format` uses an `enum` of accounts actually open in the ledger.
  Grammar-constrained decoding makes a nonexistent account *unrepresentable* —
  a hallucinated `Expenses:Food:Coffee:Speciality` cannot occur. Constrained
  decoding is also typically *faster*, since the token space is pruned.
- **Retrieve, then ask.** The prompt carries the ~20 nearest previously-confirmed
  transactions (trigram or embedding similarity) as few-shot examples. Small
  models are much better at "which of these known patterns does this resemble"
  than at cold-start classification, and it grounds them in the user's own
  naming conventions.

Note this is a *single-shot, schema-constrained classification* — the task small
models are genuinely good at — and is deliberately kept separate from the
multi-turn tool orchestration they are bad at.

### 5.5 Measuring accuracy (and only then raising autonomy)

Per decision 5, we ship in review-everything mode. Raising the threshold requires
evidence, because an untrustworthy auto-apply is worse than none — it silently
corrupts the ledger.

- **Held-out set.** As the user confirms categorizations, a random ~20% is
  withheld from tiers 1–3 training and kept in `data/eval/`.
- **Metrics per tier**: top-1 accuracy, coverage (share of transactions the tier
  answers at all), and precision at each confidence bucket. Reported by
  `bookkeeper eval` as a table.
- **Calibration.** LLM self-reported confidence is a weak signal and is *not*
  trusted directly. We bucket predictions by self-reported confidence and measure
  actual accuracy per bucket; the auto-apply threshold is set where measured
  precision clears a target (start at 95%). If no bucket clears it, auto-apply
  stays off — an acceptable outcome.
- **Regression gate.** Eval runs in CI against the fixture ledger; an accuracy
  drop fails the build.
- **Reversibility.** Ledger files are git-committed on every write, so any bad
  auto-apply batch is one `git revert` away.

### 5.6 What to strip from the template

| Component | Decision |
|---|---|
| **Auth.js / NextAuth** | **Strip the login flow, keep a single hardcoded local user row.** The template keys chats to `userId` throughout; ripping out the FK would touch every query and make future upstream merges painful. Seeding one local user is a few lines and keeps our fork close to upstream. |
| **Neon Postgres** | **Replace with SQLite** (Drizzle supports both; mostly dialect changes). A single-user localhost app should not run a database daemon, and on a 16GB machine the freed memory goes to the model — where it directly buys model quality. |
| **Chat history persistence** | **Keep.** Worth it even without auth: resuming threads is genuinely useful, and the message log doubles as an audit trail of what the assistant was asked to do. |
| **Vercel Blob** | **Strip.** No file uploads in scope. Revisit only if receipt attachments are wanted later, and use the local filesystem then. |
| **AI Gateway / hosted providers** | **Replace** with an Ollama provider at `localhost:11434` (§5.3). |
| **Artifacts feature** | **Keep, initially unused.** It is the natural home for a future "editable monthly budget document" and costs nothing to leave in place. |
| **shadcn/ui + Tailwind** | **Keep.** Our custom components build on it. |

---

## 6. Phases

Each phase ends in something runnable. Phases 0–5 use the SimpleFIN **demo
server** exclusively — no real bank credentials until Phase 6.

### Phase 0 — Skeleton
Sidecar scaffold (uv, FastAPI) with pinned beancount v3 + beangulp + beanquery.
Fork and strip the template per §5.6: SQLite, single local user, Ollama provider.
Hand-written 20-transaction fixture ledger. `just dev` starts all three processes.
**Exit:** `bean-check` green in CI; chat responds through local Ollama; sidecar
`/health` reachable from a Next.js route handler.

### Phase 1 — SimpleFIN ingest
Claim flow (one-shot CLI writing the Access URL to a `0600` file outside git),
`/accounts` client with date windows, raw archival, normalization, `simplefin-id`
dedup, beancount rendering, generated balance assertions. Everything lands in
`Expenses:Unknown`.
**Exit:** `bookkeeper sync` run twice against the demo server produces
byte-identical ledgers — idempotency proven, not assumed — and balance assertions
pass.

### Phase 2 — Envelopes
Directive parsing, the pure computation of §5.2, and `bookkeeper verify` with the
unmapped-expense and over-allocation checks. Golden-file snapshot tests.
**Exit:** allocate → spend → refund sequence yields correct balances; an unmapped
expense account and an over-allocation each fail `verify` loudly.

### Phase 3 — Categorization
Tiers 1–3 first, then tier 4 behind a swappable interface. Schema-constrained
Ollama calls with enum-restricted accounts; nearest-example retrieval. The eval
harness of §5.5 is built *in this phase*, not later.
**Exit:** measured per-tier accuracy on held-out data; tiers 1–3 alone beat a
majority-class baseline; a documented, data-driven auto-apply threshold (or a
documented decision to leave auto-apply off).

### Phase 4 — Chat interface
The six tools of §5.3. `ReviewCard`, `EnvelopeCard`, `ReportChart`. Direct-to-API
click handlers that bypass the model. Background sync with progress polling.
Deterministic pre-routing for obvious commands. Empty-text-after-tool-call
handling.
**Exit:** a full natural-language sync → review → correct loop with no
hand-editing of ledger files; corrections demonstrably change the next run's
predictions; approving 40 transactions makes zero additional LLM calls.

### Phase 5 — Reports and model bake-off
Spend-by-envelope over time, budget vs. actual, trend and outlier detection, all
as rendered charts. Bake off Qwen3 8B vs. Phi-4 14B vs. a Gemma candidate on both
categorization accuracy and tool-calling reliability, measuring latency under the
§3.2 memory budget.
**Exit:** an accuracy/latency table backing the model choice; month-end report
rendered in chat.

### Phase 6 — Real data
Claim a real SimpleFIN token, backfill history, reconcile against actual
statements, tune envelopes and rules against real spending.
**Exit:** balance assertions match real statements for a full month.

---

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **8B model too unreliable for conversational tool calling** | **High** | The §5.3 rules (shallow tools, clicks bypass the model, deterministic pre-routing) remove most of the exposure. Qwen3-8B's ~13% figure is for multi-step agentic tasks; our tool calls are single-step. Fallback: shrink to 3 tools, or drive the review loop entirely from pre-routed commands. The CLI remains fully functional regardless. |
| **Everything must fit in 16GB** | **High** | Measured budget in §3.2. SQLite over Postgres. One model for both jobs. 14B is opt-in for batch only. If memory pressure appears, model quantization drops before the feature set does. |
| **Envelope drift, unvalidated by bean-check** | **Medium** | Accepted tradeoff of decision 3, addressed by the four explicit checks in §5.2 — especially the unmapped-expense error, which is the real silent-drift vector. |
| **Auto-apply silently corrupts the ledger** | **High** | Review-everything by default; threshold set from held-out data per §5.5, never guessed; every write git-committed and revertible. |
| **Access URL leakage** | **High** | `0600` file outside the repo, never logged, `.gitignore` covers `data/` and secrets. Documented as banking-credential-grade. Never sent to the browser. |
| **Template fork diverges from upstream** | **Medium** | Keep modifications narrow and localized (provider config, DB dialect, seeded user, added tools/components). Avoid touching the template's chat internals. |
| **24 req/day rate limit** | **Low** | One `/accounts` call per sync covering all accounts. Raw archive means re-parsing costs zero requests. |
| **Pending→posted id instability** | **Medium** | Pending excluded from the ledger by default; surfaced read-only in chat if wanted. |
| **Chat latency feels sluggish** | ~~Medium~~ **Confirmed, mitigated** | Measured at **32 s/turn with Qwen3 thinking on, 2 s with `think: false`** (§5.3 amendment) — the risk was real and understated. Primary mitigation is disabling thinking for interactive turns, which requires a provider with native Ollama option pass-through. Secondary: batch work moved out of the chat turn (§5.3 rule 3); model kept warm; deterministic pre-routing answers common commands without inference. |

---

## 8. Non-goals

Investment/portfolio tracking, tax preparation, multi-currency (until asked),
multi-user or hosted deployment, mobile apps, bill pay or any write access to
financial institutions, and replacing Fava as a ledger browser.

---

## 9. Notes for implementation

- **Never send the Access URL or raw account credentials to the browser.** All
  SimpleFIN interaction is sidecar-side; the chat layer sees only derived data.
- **The sidecar is the sole ledger writer.** Next.js route handlers proxy; they
  never open a `.beancount` file.
- **Commit ledger changes automatically** after each sync and each accepted batch,
  with a descriptive message. Git is the undo system.
- **Keep the CLI at parity with the chat tools.** Every operation the chatbot can
  invoke must be runnable headless — for cron sync, for debugging, and as the
  fallback if the model proves unreliable.
