# bookkeeper

An AI-assisted envelope-budgeting bookkeeper over a plain-text
[beancount](https://beancount.github.io/) ledger, fed by
[SimpleFIN](https://www.simplefin.org/), with a local LLM. Single user,
localhost only, no paid API.

**Status: Phases 0–2 of 7 complete.** The ledger pipeline works end to end
against live data. The AI half does not exist yet. Read
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
| Chat UI running against a local model | ✅ shell only — see below |
| **AI categorization of transactions** | ❌ Phase 3 — not built |
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

**This red is the point, not a bug.** All 338 transactions are uncategorized
(categorization is Phase 3), and the system refuses to let uncategorized
spending quietly vanish from the budget view. Because envelope state is
computed rather than posted, `bean-check` can't validate it — this check is the
substitute guard. It goes green when Phase 3 assigns real expense accounts.

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
`Expenses:Food:Groceries`. Allocated / spent / balance then compute correctly,
including refunds crediting envelopes back. (Demo-server amounts are large and
unrealistic — it's synthetic data.)

### 6. Chat with a local model, and the sidecar boundary

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
- Any automatic categorization
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
   simplefin · ingest · envelope · reports
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
cd sidecar && uv run pytest        # 95 passed, 1 skipped
cd sidecar && uv run ruff check .
cd web && pnpm exec tsc --noEmit && pnpm run build
```

The one skipped test hits the live SimpleFIN demo server; enable with
`RUN_SIMPLEFIN_INTEGRATION=1`.

---

## Known issues

**`available to budget` is wrong when an envelope is overspent.** The formula
credits back negative envelope balances, but overspent money has already left
the bank. Cash 100, allocate 50, spend 80 → real cash is 20, but the system
reports `Available to budget: 50.00` and `verify: OK`. Worse, this *silences*
the over-allocation guard, which is the one check meant to catch budgeting money
you don't have. Fix direction and reproduction in PLAN.md §5.2. **Not yet fixed.**

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

- **Phase 3** — tiered categorization: exact-match memory → user rules →
  statistical → local LLM for the tail only, plus an accuracy harness. Ships in
  review-everything mode; auto-apply only once measured precision earns it.
- **Phase 4** — the six chat tools and generative UI, so the chatbot can
  actually read your books. Clicks bypass the LLM entirely.
- **Phase 5** — expense reports and a model bake-off.
- **Phase 6** — real bank credentials.
