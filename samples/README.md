# samples/

Committed demo artifacts. Safe to read, safe to share — this is synthetic data
from SimpleFIN's **public demo server**, not anyone's real bank account.

## `simplefin-response.json`

One complete, unedited response from:

```
GET https://demo:demo@beta-bridge.simplefin.org/simplefin/accounts?start-date=<epoch>
```

captured 2026-07-30. It is exactly the input that produced the committed
ledger: 169 Checking + 169 Savings = **338 transactions**, and the id set
matches `ledger/transactions/2026.beancount` exactly.

**Reformatted for reading.** The wire response is minified — 64,880 bytes on a
single line. This file is the same JSON pretty-printed (`json.tool`, keys in
original order); the parsed content is byte-for-byte equivalent. To see the
true wire format, run a sync and look in `data/raw/`.

### What to look at

**The transaction shape is thin.** Per the SimpleFIN spec a transaction is only
`id`, `posted`, `amount`, `description`, and optionally `transacted_at` /
`pending` / `extra`. There is no structured merchant identity to categorize
from — that is the central constraint on the whole AI design (PLAN.md §3.1).

**`payee`, `memo`, and `mcc` are undocumented bonuses.** This server returns
them; the spec never mentions them. `mcc: "5812"` is a merchant category code
(eating places). Tempting to categorize from, but a real bank may send none of
the three, so ingest stores them as optional metadata and nothing depends on
them.

**Transaction ids collide across accounts — this is the important one.**
338 transactions carry only **169 distinct ids**. Id `1777795200` appears in
*both* accounts, on genuinely different transactions:

```
Demo Savings  → id 1777795200, amount "-15.50", "Fishing bait"
Demo Checking → id 1777795200, amount "-19.96", "Fishing bait"
```

SimpleFIN scopes ids *per account*, not globally. Deduplicating on the bare id
silently discarded every transaction from the second account — real,
unannounced data loss on a financial ledger. The dedup key is therefore
`(account, id)`. You can see the collision directly in this file.

**Errors arrive alongside a successful response.** Note the top-level
`errors` array sitting next to a perfectly good `accounts` payload:

```json
"errors": ["Requested date range exceeds limit of 90 days and was capped."]
```

HTTP 200, real data, *and* a soft error saying the window was silently
truncated. A client that only checks the status code would never notice its
date range was ignored.

**One account has zero transactions.** `Demo Empty Account` still needs an
`open` directive and a balance assertion — the empty case is easy to forget.

## Why `data/raw/` is gitignored and this file isn't

Every sync archives its raw response to `data/raw/` before parsing, so
re-parsing never costs an API request (SimpleFIN allows roughly 24/day). That
directory stays gitignored **permanently and deliberately**: from Phase 6 it
fills with real bank transactions. Un-ignoring it to share a sample would arm a
foot-gun that fires the first time someone connects a real account.

This directory is the safe alternative — a curated, obviously-public artifact,
committed on purpose.
