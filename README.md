# bookkeeper

An AI-assisted envelope-budgeting bookkeeper over a plain-text
[beancount](https://beancount.github.io/) ledger, fed by
[SimpleFIN](https://www.simplefin.org/), with a local LLM. Single user,
localhost only, no paid API.

**Status: Phases 0–3 of 7 complete.** The ledger pipeline works end to end
against live data, and transactions now get categorized by a tiered
classifier. The chat half does not exist yet. Read
[What you can demo](#what-you-can-demo) before expecting a chatbot that
answers questions about your budget — it can't yet.

See [PLAN.md](./PLAN.md) for the full design and phase breakdown.

---

## What works today

| Capability | State |
|---|---|
| Pull transactions from SimpleFIN into beancount | ✅ working, live-verified |
| Idempotent re-sync (byte-identical, no duplicates) | ✅ working, live-verified |
| Balance assertions reconciling against bank-reported balances | ✅ working |
| Envelope balances computed from `custom` directives | ✅ working |
| Ledger + envelope integrity checks (`verify`) | ✅ working |
| Tiered categorization (`categorize`, `review`, `eval`) | ✅ working — accuracy measured on a synthetic corpus only |
| Chat UI running against a local model | ✅ shell only — see below |
| **Chat that can answer questions about your books** | ❌ Phase 4 — not built |
| **Expense reports / charts** | ❌ Phase 5 — not built |
| **Real bank data** | ❌ Phase 6 — demo server only |

The chat app boots, talks to local Qwen3, and streams tool calls correctly —
but its only tool is the upstream template's `getWeather`. **It has no
connection to your ledger.** Wiring the six bookkeeper tools is Phase 4.

---

## Requirements

- macOS or Linux, [uv](https://docs.astral.sh/uv/), Node 20+, pnpm (via `corepack enable pnpm`)
- [Ollama](https://ollama.com/) with `qwen3:8b` pulled (~5 GB): `ollama pull qwen3:8b`
- ~16 GB RAM (the design targets a 16 GB Apple Silicon machine; see PLAN.md §3.2)

No `just` or Docker required — use the `Makefile`.

---

## Quickstart

```bash
make install                 # uv sync + pnpm install
```

Supply a SimpleFIN Access URL. The public demo one works and needs no signup:

```bash
mkdir -p data/secrets && chmod 700 data/secrets
printf 'https://demo:demo@beta-bridge.simplefin.org/simplefin' \
  > data/secrets/simplefin.access-url
chmod 600 data/secrets/simplefin.access-url
```

> The Access URL is a **banking credential**. It is stored at mode `0600`,
> never logged, and never sent to the browser. `data/secrets/` is gitignored.
>
> `bookkeeper claim <setup-token>` implements the documented token-exchange
> flow, but the public demo token currently returns `403` server-side and the
> bridge has migrated hosts — see [Known issues](#known-issues). Supplying an
> Access URL directly is the supported path today, and is what a real user
> does anyway.

---

## What you can demo

Everything below is real, runs against the live SimpleFIN demo server, and was
verified end to end.

> **Run every command from the repo root.** `uv run --directory sidecar` keeps
> uv pointed at the Python project while leaving your shell at the root, so the
> `ledger/` paths below resolve. (`bean-check` is the exception — it resolves
> relative to `sidecar/`, hence the `../`.)

### 1. Sync real bank data into a plain-text ledger

```bash
uv run --directory sidecar bookkeeper sync --since 2026-05-01
```

```
accounts synced: 3
transactions seen: 338
transactions added: 338
balance assertions written: 3
opening balance plugs written: 3
```

Look at what it produced — it's just text, readable and diffable:

```bash
head -20 ledger/transactions/2026.beancount
cat ledger/accounts-simplefin.beancount
cat ledger/balances.beancount
```

And look at what the bank actually sent. A complete captured response — the
exact input that produced the committed ledger — is checked in at
[`samples/simplefin-response.json`](./samples/simplefin-response.json), with
annotations in [`samples/README.md`](./samples/README.md):

```bash
head -30 samples/simplefin-response.json
```

It's worth reading side by side with the ledger, because three things are
visible in the raw data that explain the design:

- **A transaction is almost nothing** — an amount and a bank-mangled string.
  There's no merchant identity to categorize from, which is the whole reason
  Phase 3 is hard.
- **Transaction ids collide across accounts.** 338 transactions, **169 distinct
  ids** — every single one appears in both accounts on a *different*
  transaction (`1777795200` is `-15.50` in Savings and `-19.96` in Checking).
  Deduplicating on the bare id silently deleted an entire account's history.
- **A soft error rides along with a successful response** — `"Requested date
  range exceeds limit of 90 days and was capped."` sits in a top-level `errors`
  array next to HTTP 200 and perfectly good data.

Your own raw responses accumulate in `data/raw/` on every sync (so re-parsing
never costs an API call). That directory is gitignored permanently — from
Phase 6 it holds real bank data.

### 2. Prove it's idempotent

The strongest thing to show. Run the same command again:

```bash
uv run --directory sidecar bookkeeper sync --since 2026-05-01
# transactions added: 0, already present (skipped): 338
```

The ledger is **byte-identical**. Delete the generated files and rebuild from
nothing — still byte-identical:

```bash
shasum ledger/transactions/*.beancount ledger/balances.beancount ledger/accounts-simplefin.beancount

rm ledger/transactions/2026.beancount ledger/balances.beancount ledger/accounts-simplefin.beancount
uv run --directory sidecar bookkeeper sync --since 2026-05-01

shasum ledger/transactions/*.beancount ledger/balances.beancount ledger/accounts-simplefin.beancount
# same hashes as before the delete
```

### 3. Show the ledger is internally consistent

```bash
uv run --directory sidecar bean-check ../ledger/main.beancount   # exit 0, silent
```

Double-entry holds and every bank-reported balance reconciles against the
transactions actually in the ledger. Delete one transaction and re-run to watch
it catch the discrepancy — this is the cash-truth guard, and it works.

### 4. Show the budget refusing to lie

```bash
uv run --directory sidecar bookkeeper verify        # exits 1
```

```
verify: FAILED (1 error(s))
  - unmapped expense account "Expenses:Unknown": it has postings but no
    `custom "envelope" "map"` directive targets it, so its spending does not
    count against any envelope
```

**This red is the point, not a bug.** On a fresh checkout all 338 transactions
are still uncategorized, and the system refuses to let uncategorized spending
quietly vanish from the budget view. Because envelope state is computed rather
than posted, `bean-check` can't validate it — this check is the substitute
guard. It goes green once `categorize --apply` (next section) assigns real
expense accounts and those accounts are mapped to envelopes.

### 5. Show envelope math

```bash
uv run --directory sidecar bookkeeper envelopes
```

Every envelope reads zero on a fresh checkout: `budget.beancount` ships empty
(allocating against zero cash would fail the system's own validator), and all
spending is still in the unmapped `Expenses:Unknown`.

To see the engine actually compute, add allocations to `ledger/budget.beancount`:

```beancount
2026-05-01 custom "envelope" "allocate" "Groceries"   800.00 USD
2026-05-01 custom "envelope" "allocate" "Dining Out"  300.00 USD
```

and recategorize a few postings from `Expenses:Unknown` to
`Expenses:Food:Groceries` (or let `categorize` do it — see below). Allocated /
spent / balance then compute correctly, including refunds crediting envelopes
back. (Demo-server amounts are large and unrealistic — it's synthetic data.)

Overspend an envelope and the report says so out loud:

```
Utilities         100.00        130.00        -30.00  OVERSPENT
----------------------------------------------------
Budgeted cash:                   3758.00
Envelope balances (total):        428.00
Overspent (total):                 30.00
Available to budget:             3300.00
```

Money already spent out of an envelope has left the bank, so it is not credited
back into `Available to budget` — the summary block reads as arithmetic,
`cash − balances − overspend`. Overspending is normal (you cover it from next
month's allocation), so `verify` reports it as a **note**, not a failure.

### 6. Categorize transactions

Everything lands in `Expenses:Unknown` at sync time. `categorize` predicts a
real account for each one, using a cascade of tiers where the **first hit
wins**:

| Tier | What it does |
|---|---|
| **memory** | Exact match on a normalized description you've confirmed before. Majority vote over past confirmations; a tie abstains. |
| **rule** | Your own patterns from `data/rules.yaml`. First match in file order. |
| **mcc** | The merchant category code, when the bank sends one. Undocumented in the SimpleFIN spec — see [Known issues](#known-issues). |
| **statistical** | A self-contained naive Bayes over char n-grams and tokens, trained on what you've already confirmed. No scikit-learn. |
| **llm** | Local Qwen3, for descriptions genuinely unlike anything seen. The account is a JSON-Schema `enum` of accounts actually open in your ledger, so a hallucinated account is unrepresentable rather than merely unlikely. |

A tier that doesn't know **abstains** rather than guessing — the transaction
falls through to the next tier, and out the bottom into the review queue.

```bash
uv run --directory sidecar bookkeeper categorize            # dry run: prints, writes nothing
uv run --directory sidecar bookkeeper categorize --no-llm   # deterministic + statistical only
uv run --directory sidecar bookkeeper review                # what's waiting on you
```

**Nothing is written unless you ask.** `categorize` is a dry run by default;
`--apply` writes, and even then only predictions that clear the auto-apply
threshold are applied unattended. **That threshold ships unset**, which means
review-everything: every prediction goes to the queue for a human, and the
ledger is not touched automatically at all. Turning it on is a deliberate act —
set `auto_apply_threshold` in `data/categorize-policy.json` (git-tracked, so
raising autonomy over your books shows up in the history with a date and a
diff), or `BOOKKEEPER_AUTO_APPLY_THRESHOLD` for a single run.

Set it from measured evidence, not a guess. `bookkeeper eval` reports per-tier
top-1 accuracy, coverage, and precision per confidence bucket:

```bash
uv run --directory sidecar bookkeeper eval          # deterministic tiers; hermetic, no Ollama
uv run --directory sidecar bookkeeper eval --llm    # include the LLM tier (slow)
```

> **Read [`docs/phase3-accuracy.md`](./docs/phase3-accuracy.md) before trusting
> any number it prints.** The SimpleFIN demo server has 338 transactions with
> **three distinct descriptions** between them, so accuracy against it is
> meaningless — tier 1 memorizes all three and scores 100%. The real eval runs
> against a synthetic corpus of realistic bank-mangled strings, which measures
> whether the cascade *mechanically works*, not how well it will do on your
> spending. **Real-world accuracy is a Phase 6 question and has not been
> measured.**

Applied categorizations are stamped with metadata (`bookkeeper-tier`,
`bookkeeper-confidence`, `bookkeeper-decision`) so you can always tell a
machine-assigned posting from one you wrote, and an unattended auto-apply from
a human confirmation. Ledger writes are git-committed, so any bad batch is one
`git revert` away.

#### The two files you edit

Both live in `data/`, are plain text, and are git-tracked on purpose — a diff
on either is an audit trail of what the system has been told.

**`data/rules.yaml`** — things you know and shouldn't have to confirm twice.
A list of rules; first match wins; `pattern` is a case-insensitive regex and
`account` must be open in your ledger (both are validated, loudly, rather than
misfiling quietly):

```yaml
- name: PG&E
  pattern: 'PG&E|PACIFIC GAS'
  account: Expenses:Home:Utilities

- name: Paycheck
  pattern: 'ACME ROBOTICS.*PAYROLL'
  account: Income:Salary
  sign: positive        # optional: only match deposits

- name: Big-ticket hardware
  pattern: 'HOME DEPOT'
  account: Expenses:Home:Improvement
  amount_max: -200      # optional bounds, on the SIGNED amount:
                        # spending is negative, so "$200 or more" is <= -200
```

`pattern` is matched against the description *and* the payee, so either can
trigger a rule.

**`data/memory.json`** — written *for* you, not by you. Every time you confirm
or correct a review card it records `normalized description → account`, with a
count. You don't have to edit it, but it's readable and you can, and its git
history is the record of what you've taught the system.

### 7. Chat with a local model, and the sidecar boundary

```bash
make dev        # starts Ollama, the Python sidecar, and Next.js together
```

- <http://localhost:3000> — chat UI, answering from `qwen3:8b` on your machine.
  No API key, no network egress to any model provider.
- <http://localhost:3000/api/sidecar/health> — proves the TypeScript ↔ Python
  boundary: `{"reachable":true,"sidecar":{"status":"ok","beancount_version":"3.2.3"}}`

Ctrl+C tears all three down cleanly.

**Be clear about what this shows.** It's a working local-LLM chat app plus a
proven path to the ledger service. It is *not* a bookkeeper you can talk to —
ask it about groceries and it will make something up, because it has no tool
that reads your books. That's Phase 4.

### What you cannot demo

- Asking the chatbot anything about your budget or transactions
- Categorization *accuracy you can trust* — the mechanism works, but the only
  corpus it has been measured against is synthetic (see
  [`docs/phase3-accuracy.md`](./docs/phase3-accuracy.md))
- Expense reports, charts, or trends
- Real bank accounts

---

## Architecture

```
Next.js (vercel/ai-chatbot, stripped)  :3000
   chat UI ── AI SDK ──► Ollama :11434 (qwen3:8b, think:false)
      │
      │ HTTP
      ▼
Python sidecar (FastAPI)               :8000
   simplefin · ingest · categorize · envelope · reports
   SOLE WRITER to the ledger
      │
      ▼
ledger/*.beancount  ──►  Fava (optional, read-only)
```

The sidecar exists because beancount's ecosystem is Python while the chat app is
TypeScript, and because `loader.load_file` costs seconds — a resident process
loads once and caches on mtime. It is the **only** process that writes ledger
files; Next.js route handlers proxy and never touch `.beancount` directly.

```
ledger/
  main.beancount              includes everything below
  accounts.beancount          hand-curated: expense/envelope accounts + mappings
  accounts-simplefin.beancount  GENERATED — bank accounts, rewritten each sync
  budget.beancount            your envelope allocations (ships empty)
  transactions/YYYY.beancount GENERATED
  balances.beancount          GENERATED — bank-reported balance assertions
data/rules.yaml               your categorization rules (hand-edited)
data/memory.json              learned description → account (written for you)
data/categorize-policy.json   auto-apply threshold; absent = review everything
data/secrets/                 Access URL, 0600, gitignored
sidecar/                      Python: bookkeeper package + tests
web/                          Next.js chat app
```

Envelopes are **computed** from `custom "envelope"` directives rather than real
postings, keeping the ledger clean and portable at the cost of `bean-check` not
validating them (hence `verify`). See PLAN.md §5.2.

---

## Development

```bash
cd sidecar && uv run pytest
cd sidecar && uv run ruff check .
cd web && pnpm exec tsc --noEmit && pnpm run build
```

The one skipped test hits the live SimpleFIN demo server; enable with
`RUN_SIMPLEFIN_INTEGRATION=1`.

### CI

`.github/workflows/ci.yml` runs the same commands on every push, as two jobs so
a Python failure and a TypeScript failure are distinguishable at a glance:
**sidecar** (ruff, pytest) and **web** (ultracite, `tsc --noEmit`, unit tests,
`next build`).

It is offline by design. No Ollama and no SimpleFIN: the accuracy eval defaults
to `use_llm=False`, tier-4 tests are mocked at the HTTP layer, and the live
SimpleFIN test stays skipped because nothing sets `RUN_SIMPLEFIN_INTEGRATION`.
A dedicated step fails the build if a test wrote to the checked-out `ledger/`.

The **§5.5 accuracy regression gate** — non-LLM cascade accuracy must stay above
a committed 0.85 floor — runs as its own named step so a regression is not
buried in the rest of the suite. See `docs/phase3-accuracy.md`.

The sidecar's HTTP surface is what Phase 4 will consume. It is browsable at
<http://localhost:8000/docs> once `bookkeeper serve` is running:

| Endpoint | |
|---|---|
| `GET /health` | liveness + beancount version |
| `GET /accounts` | asset accounts and balances, read from the ledger |
| `GET /envelopes?asof=` | envelope balances, overspend, available to budget |
| `GET /verify` | integrity checks; a failing ledger is a 200 with `ok: false` |
| `GET /review-queue?limit=` | transactions awaiting human categorization |
| `POST /sync` | fetch from SimpleFIN and write the ledger |
| `POST /categorize` | predict accounts; **dry run unless `{"apply": true}`** |

---

## Known issues

**Categorization accuracy is unmeasured on real data.** The cascade works and
is tested, but the demo server offers three distinct transaction descriptions
in total, so every accuracy figure the eval harness can produce today comes
from a synthetic corpus. It measures mechanics, not real-world performance.
See [`docs/phase3-accuracy.md`](./docs/phase3-accuracy.md). This is why
auto-apply ships off.

**`bookkeeper claim` fails against the public demo token.** The bridge migrated
to `beta-bridge.simplefin.org` and its redirect drops the URL path; claiming
returns `403` despite the docs describing the demo token as reusable. External
to this code. Use the demo Access URL above.

**`mcc` / `payee` / `memo` are undocumented.** The live server returns them and
this code captures them as optional metadata, but they appear nowhere in the
SimpleFIN spec, so a real institution may omit all three. Phase 3 must measure
real coverage before relying on them.

**`Expenses:Unknown` absorbs income too.** Uncategorized deposits land there
alongside expenses. Phase 3's categorizer cannot assume everything in that
account is spending.

**Chat latency depends on `think: false`.** Qwen3 is a reasoning model: 32 s per
turn with thinking on, 2 s with it off. The app disables it per call. This is
only reachable through Ollama's native endpoint, not the OpenAI-compatible one.

**Biome reaches into `.omc/`.** `pnpm run check` from `web/` may report
formatting on repo session state. Scope it (`biome check app components lib hooks`)
or add an ignore.

---

## What's next (PLAN.md)

- **Phase 4** — the six chat tools and generative UI, so the chatbot can
  actually read your books. Clicks bypass the LLM entirely.
- **Phase 5** — expense reports and a model bake-off.
- **Phase 6** — real bank credentials.
