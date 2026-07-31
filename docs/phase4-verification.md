# Phase 4 — exit criteria, verified

Measured 2026-07-31 on branch `phase-4-chat`, against commit `68999cb`
("Unit-test the four modules that had none").

> **Every number in this document was produced by a run on this machine.**
> Nothing here is projected, inferred from reading the code, or carried over
> from a previous phase. Where something could not be measured, it is named as
> unmeasured rather than estimated — see "What these numbers are worth" at the
> end, which is the most important section.

## Verdict

| PLAN.md §6 Phase 4 exit criterion | Status |
|---|---|
| A full natural-language sync → review → correct loop, no hand-editing of ledger files | **Met** — 338 transactions synced and 332 corrected through the app; `bean-check` clean; every ledger byte inside an app-made commit |
| Corrections demonstrably change the next run's predictions | **Met** — a merchant the cascade abstained on is predicted from the memory tier after one confirmation, on a *different* surface form |
| Approving 40 transactions makes zero additional LLM calls | **Met — measured 0, twice, on the wire** |

Two defects found in the process, neither of which blocks a criterion. Both
are reported in "Findings" below and have **not** been fixed by me.

## The headline number

**Approving 40 transactions through `POST /api/bookkeeper/review/confirm`
produced 0 LLM calls.** Measured twice, on two independently generated fixture
trees:

| run | fixture | approved | confirmed | LLM calls at the proxy | total requests at the proxy |
|---|---|---|---|---|---|
| 1 | `fixture` | 40 | 40 | **0** | 0 |
| 2 | `fixture2` | 40 | 40 | **0** | 0 |

Not "zero chat completions" — **zero requests of any kind reached Ollama**
during the approval window, inference or metadata. The proxy's audit log for
run 1 contains exactly two entries for the whole session, and both are the
positive control:

```
inference POST /api/chat   model=qwen3:8b
inference POST /api/chat   model=qwen3:8b
```

### Why it is measured this way

"Zero LLM calls" is a claim about a negative, and a negative cannot be
established by reading code — reading only ever covers the call paths you
thought of. So `scripts/ollama_call_counter.py` is placed **on the wire**
between the app and Ollama, and every request is counted **on arrival, before
forwarding**. Two properties follow, and both matter:

- A call from a path nobody remembered — a title generator, a suggestion hook,
  an embedding lookup inside a library — is counted anyway, because the
  instrument observes the socket rather than our own provider seam.
- An *attempted* call counts even if Ollama is down. A counter that recorded
  only successful round trips would report zero for a batch that tried forty
  times and failed forty times, which is a false pass in exactly the situation
  where the harness is most likely to be run.

The batch is driven through **Next.js**, not against the sidecar directly. That
is deliberate: going straight to Python would skip the process where an
accidental model call is most likely to hide.

### The positive control, which is what makes the zero mean anything

A measured zero is ambiguous between "the app called no model" and "the
instrument was never connected", and those look identical in the output. So
each run first sends a real chat turn (`"hello there, introduce yourself in one
short sentence"` — deliberately *not* one of the pre-routed commands) and
requires the counter to move:

| run | positive-control LLM calls | approval LLM calls |
|---|---|---|
| 1 | 2 | 0 |
| 2 | 2 | 0 |

The counter moved to 2 and then to 0 in the same process, against the same
proxy, minutes apart. **The zero is a measurement, not a disconnected wire.**

### The two halves, reported separately

The claim spans two processes, and neither half is allowed to stand in for the
other:

| half | what it covers | how | result |
|---|---|---|---|
| Sidecar | everything downstream of a confirmation | `sidecar/tests/test_phase4_e2e.py`, 17 tests, hermetic | 0 LLM calls |
| Web | the Next.js route the Accept button actually hits | `scripts/phase4_measure_confirm.py` against the running stack | 0 LLM calls |

The sidecar half additionally pins the properties the criterion depends on but
does not name: the batch is **one ledger pass and one git commit** rather than
forty (forty commits would make `git revert` useless as the undo for a bad
batch), it teaches tier 1 forty times, and a second approval pass finds an
empty queue and still calls no model.

## Criterion 1 — the full loop, no hand-editing

Driven by `scripts/phase4_drive_loop.py`. Sync and review are sent as
**sentences** to `POST /api/chat`; the corrections are the direct
`POST /api/bookkeeper/review/confirm` call that `ReviewCard`'s buttons make.
Nothing in the run touches the sidecar directly and nothing opens a
`.beancount` file.

| step | how it was driven | result |
|---|---|---|
| sync | `"sync my accounts"` | `sync_accounts` invoked; job succeeded |
| — | | 338 transactions seen, **338 added**, 0 skipped, 3 balance assertions, 3 opening-balance plugs |
| review | `"show me the review queue"` | `get_review_queue` invoked; 332 entries |
| correct | 332 confirmations in one batch | **332 confirmed, 332 learned, 0 LLM calls**, commit `e39c6b4` |
| after | | review queue **0**; `bean-check` **clean**; `verify` **OK** |

The sync reported one soft error, surfaced rather than swallowed:
`Requested date range exceeds limit of 90 days and was capped.` — the §3.1
behaviour, arriving in a 200 response and correctly propagated to the summary.

`verify` returned OK with two **notes**, both expected: `Dining Out` and
`Groceries` are overspent because this fixture's `budget.beancount` allocates
nothing to them while the demo feed spends against them. Overspend is a note
rather than an error by design (§5.2), and the notes name the amounts.

### "No hand-editing", evidenced structurally

The ledger tree is a git repo, committed clean before the loop. The driver
never writes into it. So anything that differs afterwards was written by the
app — and the diff is a commit the app made:

```
git log  (after)   e39c6b4 Categorize 332 transactions (confirmed by hand)
                   1376051 criterion-1 starting tree (no transactions yet)
git status (after) ?? data/raw/
```

The only untracked path afterwards is `data/raw/` — the immutable fetch
archive, which is data rather than ledger and is gitignored in the real repo.
**No ledger file is left uncommitted, and no commit came from anything but the
app.**

### The sync is replayed, and why that is the honest substitution

`scripts/simplefin_replay.py` serves a previously-captured
`data/raw/simplefin-2026-07-30T18:55:51Z.json` at `/accounts`, and the sidecar
is pointed at it with `BOOKKEEPER_SIMPLEFIN_ACCESS_URL`. Nothing in the sidecar
is stubbed or monkeypatched: `fetch_accounts` makes its ordinary HTTP GET,
archives the bytes, then parses, normalizes, dedups and renders exactly as it
would against the bridge. Only the far end of the socket is replaced.

This is a real substitution and worth stating plainly rather than burying: the
run does **not** prove that a live SimpleFIN fetch works. It proves the loop
works. The bridge is rate-limited to ~24 requests/day (§3.1), its claim
endpoint is broken server-side, and a verification run is a bad reason to spend
either — the live path is Phase 6's business.

## Criterion 2 — corrections change the next run's predictions

The interesting case is **not** a byte-identical repeat. `data/memory.json` is
keyed on the normalized description, so re-showing the same string would prove
only that a cache works. Both measurements below use two different surface
forms of one merchant, differing in payment-rail prefix, store number and
punctuation:

```
confirmed :  SQ *PEGASUS BOOKS 4471
probed    :  PURCHASE AUTHORIZED ON 07/22 PEGASUS BOOKS #8823
```

Measured live through the web stack:

| | prediction for the *novel* form | tier |
|---|---|---|
| before the correction | *(abstained — no prediction)* | — |
| after one confirmation | **`Expenses:Books`** | **memory** |

with `data/memory.json` holding exactly `{"pegasus books": {"Expenses:Books": 1}}`
and **0 LLM calls** across the whole correction loop. The same before/after is
also pinned hermetically by `test_a_correction_changes_the_next_runs_prediction`,
which asserts the "before" state is not already the target account — without
that guard the test could pass while proving nothing.

### The boundary of this claim

Tier 1 generalizes exactly as far as `normalize_description` does, and no
further. A third surface form —

```
POS DEBIT PEGASUS BOOKS OAKLAND 8823
```

— normalizes to a *different* key because of the appended city token, and the
memory tier misses it. This is a real limit of the shipped design and is pinned
by `test_the_generalization_stops_where_normalization_stops`. Reporting
criterion 2 as an unqualified "memory generalizes" would overstate what was
measured.

## Findings

Neither defect is fixed here. Verification and repair are kept in separate
hands on purpose.

### Finding 1 — pre-routing does not bypass the model; it forces a tool choice

§5.3 rule 4 specifies that obvious commands are "matched **before the model
sees them** and invoke the tool directly. Cheap, instant."

As implemented, `preRouteMessage` feeds `streamText`'s `toolChoice`
(`app/(chat)/api/chat/route.ts:340`) and nothing else. The message still goes
to the model; the model is merely constrained to emit that tool call. Measured
at the proxy:

| turn | pre-routed? | LLM calls |
|---|---|---|
| `"sync my accounts"` (first message in a chat) | yes | **3** |
| `"show me the review queue"` (same chat) | yes | **2** |

The decomposition is: one forced tool-call step, one step for the model's
one-sentence reply to the tool result (`MAX_STEPS_PER_TURN = 2`), plus one
title-generation call on a chat's first message only.

**Severity: low, and it is a divergence from the plan rather than a bug.** The
substantive risk rule 4 exists to remove — the "does this need a tool, and
which one?" decision that §3.3 says 8B models are worst at — *is* removed by
`toolChoice`. What is not delivered is "cheap, instant": a pre-routed command
costs two inference calls and their latency, where the plan describes zero.
Worth an explicit decision to either amend the plan or short-circuit the turn,
rather than leaving the code and the plan quietly disagreeing.

This does not touch criterion 3, which is about the confirm path and never
enters the chat route.

### Finding 2 — `sync` does not commit, though §9 says it should

PLAN.md §9: "**Commit ledger changes automatically** after each sync and each
accepted batch". `commit_ledger` is called from `categorize/review.py`,
`categorize/apply.py` and `envelope/allocate.py` — but **nowhere in
`ingest/sync.py`** or its API callers.

Observed directly in the criterion-1 run: immediately after the chat-driven
sync, the tree held

```
?? ledger/balances.beancount
?? ledger/transactions/2026.beancount
```

Both were later swept into the confirm's commit, which is why the loop still
ends clean and why this is easy to miss. **A sync that is never followed by a
confirmation leaves the ledger dirty and uncommitted**, so "git is the undo
system" (§9) does not hold for a sync on its own — there is nothing to revert
to.

**Severity: medium.** It is a gap in the stated undo guarantee on the one
operation that writes the most rows.

### Already fixed — the bug these tests caught earlier

Three of these e2e tests previously failed against `categorize/review.py`,
which read descriptions *after* the ledger write, so tier 1 silently learned
nothing from a confirmation. That bug is fixed (commit `b99735f`, "Read the
description before the write, not after") and the tests are green. Recorded
because it is the clearest evidence that the criterion-2 test is load-bearing
rather than ceremonial: without it, "corrections change the next run's
predictions" would have been asserted while being false.

## Test and lint status

| | |
|---|---|
| Sidecar suite | **554 passed, 1 skipped** |
| Phase 4 e2e | **17 passed** |
| `cd sidecar && uv run ruff check .` | **All checks passed** |

The one skip is `test_simplefin_integration.py`, which hits the real SimpleFIN
demo server and is opt-in behind `RUN_SIMPLEFIN_INTEGRATION=1`. It is a
deliberate network gate, not a stubbed-out test.

## Reproducing

```bash
# instruments
python3 scripts/ollama_call_counter.py --port 11437 &
python3 scripts/simplefin_replay.py --port 8899 \
    --archive data/raw/simplefin-2026-07-30T18:55:51Z.json &

# a fixture ledger — never the real one
python3 scripts/phase4_measure_confirm.py --build-fixture /tmp/p4

# sidecar, rooted at the fixture and pointed at both instruments
cd sidecar && BOOKKEEPER_ROOT=/tmp/p4 \
  BOOKKEEPER_OLLAMA_URL=http://127.0.0.1:11437 \
  BOOKKEEPER_SIMPLEFIN_ACCESS_URL=http://127.0.0.1:8899 \
  uv run bookkeeper serve --port 8100

# web, with OLLAMA_BASE_URL pointed at the counter and SIDECAR_BASE_URL at 8100

# criterion 3
python3 scripts/phase4_measure_confirm.py --web http://127.0.0.1:3100 \
    --counter http://127.0.0.1:11437 --positive-control

# criterion 1
python3 scripts/phase4_drive_loop.py --web http://127.0.0.1:3100 \
    --counter http://127.0.0.1:11437 --root /tmp/p4
```

Both scripts exit non-zero on a failed criterion, so they are usable as gates
and not only as reports.

## What these numbers are worth

The zero is the strongest result in this document, and it is worth being
precise about its scope.

**What it establishes.** No process in the running stack — browser-facing route
handler, sidecar, or any library either of them pulls in — sent a request to
Ollama while forty transactions were approved through the real Accept-button
path. It was observed on the socket, so it covers call paths nobody thought to
look for; it counts attempts rather than successes, so it cannot be faked by a
dead upstream; and a positive control in the same run proves the instrument was
connected. §5.3 rule 2 holds where it matters most.

**What it does not establish.**

- **That a *human* clicking forty Accept buttons in a browser makes no model
  call.** The measurement drives the HTTP route those buttons call, not the
  React components. A click handler that fired something model-backed *in
  addition* to the confirm request would not appear here. The components are
  covered by unit tests, which is a different kind of evidence.
- **That the production build behaves identically.** Everything was measured
  against `next dev` on port 3100, in a detached-HEAD git worktree with its own
  `node_modules` and `.env.local`, because another worker's dev server owned
  port 3000 and the shared `.next` directory. A production `next build && next
  start` was not exercised. I have no specific reason to expect a difference in
  model calls — the route handlers are the same code — but I did not measure it
  and will not claim it.
- **Anything about a live SimpleFIN fetch**, per the replay note above.
- **Anything about categorization accuracy.** Criterion 1's 332 confirmations
  accept the cascade's own suggestions on a degenerate demo feed with three
  distinct descriptions; `docs/phase3-accuracy.md` explains at length why no
  accuracy figure from that data means anything. Criterion 1 is a claim about
  the *loop*, not about whether the guesses were good.

**Provenance.** Commit `68999cb` was measured; the branch has since moved to
`98b3966`. Every path involved — the confirm route, the chat route,
`pre-route.ts`, the review-queue route, and `categorize/review.py` — is
byte-identical between the two, so the results carry forward. The intervening
commits touch envelope allocation and tool unit tests only. There were also
uncommitted working-tree edits under `web/lib/` at the time of the run; those
were **not** included, since the worktree was checked out at the commit.

The real `ledger/` was never written to. Verified with
`git status --short -- ledger/ data/memory.json`, clean, after every run.
