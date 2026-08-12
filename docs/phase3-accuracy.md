# Phase 3 — measured categorization accuracy

Measured 2026-07-30 on branch `phase-3-categorization`.
Reproduce with `cd sidecar && uv run bookkeeper eval`.

> **These numbers come from a synthetic corpus. They are not real-world
> accuracy and must not be quoted as such.** They bound the *mechanics* of the
> cascade — that the tiers fire in the right order, that memory generalizes
> across bank manglings of the same merchant, that MCC is or is not separable,
> that the confidence signal means anything. What the product will actually
> score against a real bank feed is a Phase 6 question and nothing here answers
> it. See "What these numbers are worth" at the end, which is the most
> important section of this document.

## Decision

**Auto-apply stays off. Ship in review-everything mode, as PLAN.md decision 5
already specifies.**

The best-measured confidence band is 100.0% precise over 46 held-out
predictions, which clears the 95% target as a point estimate. Its 95% lower
bound is 92.3%. A sample of 46 cannot establish 95% precision, and the gap
between 100.0% and 92.3% is sampling noise rather than measured accuracy.
Turning on unattended writes to a financial ledger on that basis would be
reading noise as evidence.

This is a **data-volume limit, not a cascade failure**. The cascade is
accurate. There is simply not enough held-out data yet to prove it to the
standard §5.5 demands, and no amount of tuning changes that — only more
confirmed real transactions will. Revisit in Phase 6.

## Exit criteria

| PLAN.md §6 Phase 3 exit criterion | Status |
|---|---|
| Measured per-tier accuracy on held-out data | Met — table below |
| Tiers 1–3 alone beat a majority-class baseline | **Met — 90.0% vs 28.3%, margin +61.7pp** |
| Documented, data-driven auto-apply threshold *or* documented decision to leave it off | **Met — off, justified above** |

"Tiers 1–3 alone" is measured as **the cascade with the LLM tier excluded**
(memory, rule, MCC, statistical). Reading it that way means a passing exit
criterion never depends on Ollama being installed. `use_llm=False` is the
default, so the CI gate is hermetic and offline.

## Method

- **Corpus**: `sidecar/tests/fixtures/merchant_corpus.json` — 298 labeled
  transactions, 240 distinct descriptions, 10 accounts drawn from
  `ledger/accounts.beancount`. Committed under `tests/fixtures/` and not
  `data/eval/`, because `data/eval/` is gitignored and a corpus placed there
  would leave CI with nothing to run.
- **Split**: 238 train / 60 held out (~20%), seeded (`20260730`) so the numbers
  do not wobble between runs. Per-transaction, not per-merchant — a merchant
  appearing on both sides is the intended case, since that is how the memory
  tier is supposed to earn its keep.
- **Held-out discipline**: tiers see training data only, via a `LedgerContext`
  the harness builds itself. It never calls `build_ledger_context()`.
  `test_tiers_never_see_the_held_out_set` asserts this against the actual
  context handed to the tiers. See the memory-tier finding below, which was the
  one real leakage vector and is now controlled.
- **13 novel one-off merchants are pinned to the held-out side**, so tiers 1–3
  have never seen them by construction. This makes the test set *harder* than a
  random 20% would be — the conservative direction for an autonomy decision,
  and worth remembering when comparing these figures to anything else.

## Per-tier results

Each tier run independently over all 60 held-out transactions, not just the
ones it happened to reach inside the cascade.

| tier | coverage | precision | top-1 acc | answered | correct |
|---|---|---|---|---|---|
| memory | 63.3% | 100.0% | 63.3% | 38 | 38 |
| rule | 1.7% | 100.0% | 1.7% | 1 | 1 |
| mcc | 41.7% | 100.0% | 41.7% | 25 | 25 |
| statistical | 86.7% | 96.2% | 83.3% | 52 | 50 |
| **cascade (no LLM)** | **93.3%** | **96.4%** | **90.0%** | 56 | 54 |
| majority baseline | 100% | 28.3% | 28.3% | 60 | 17 |

`coverage` is the share of held-out transactions the tier answered at all;
`precision` is correct-over-answered. They are reported separately and never
merged, because abstention is silence rather than error — conflating them makes
a careful tier look terrible and a guessing tier look good.

**The MCC-before-statistical ordering in `models.py` is confirmed, not
overturned.** MCC measures 100.0% precision against the statistical tier's
96.2%, so placing the fixed lookup first is correct. MCC earns this by
abstaining rather than guessing on ambiguous codes: on the held-out set it
abstained on every transaction carrying 4899 (shared between Internet and
Netflix) and 4900 (shared between Electric and Water), and answered only on
codes that map to exactly one account. That is the behaviour the corpus was
built to test for, and the tier passes it.

## Calibration

| bucket | n | correct | measured precision |
|---|---|---|---|
| [0.00, 0.50) | 5 | 4 | 80.0% |
| [0.50, 0.60) | 2 | 2 | 100.0% |
| [0.60, 0.70) | 3 | 2 | 66.7% |
| [0.70, 0.80) | 4 | 4 | 100.0% |
| [0.80, 0.90) | 5 | 5 | 100.0% |
| [0.90, 0.95) | 0 | — | — |
| [0.95, 1.00] | 37 | 37 | 100.0% |

Self-reported confidence is not trusted as a probability, per §5.5. Read as an
ordering signal it is **weak and non-monotonic**: precision goes 100% at
[0.50,0.60) then falls to 66.7% at [0.60,0.70) before recovering. Most buckets
hold 2–8 samples, so most of that movement is noise, which is itself the point
— there is not enough data here to calibrate against, and a threshold read off
this curve would be an artifact.

The one solid band is [0.95, 1.00]: 37 of 37 correct, and all 37 are memory-tier
hits. Even that band, taken alone, has a 95% lower bound of 90.6% — below
target.

The threshold search evaluates the **cumulative tail** at each cutoff (what
auto-apply would actually apply), not individual buckets, and requires the
Wilson 95% lower bound to clear 95% rather than just the point estimate. No
cutoff qualifies. A cutoff of 0.0 is excluded by construction: it means "apply
everything regardless of confidence", which is the absence of a threshold
rather than a calibrated one.

## Error analysis

Two errors and four abstentions out of 60.

| description | true | predicted | tier | conf |
|---|---|---|---|---|
| `EAST BAY MUD AUTOPAY` | Water | Internet | statistical | 0.33 |
| `PARAMOUNT+ 8779021` | Subscriptions | Gas | statistical | 0.62 |
| `BERKELEY BOWL WEST` | Groceries | *(abstained → LLM)* | — | — |
| `76 - EL CERRITO 4432` | Gas | *(abstained → LLM)* | — | — |
| `SUTTER HEALTH PALO ALTO` | Health | *(abstained → LLM)* | — | — |
| `HULU 8776783` | Subscriptions | *(abstained → LLM)* | — | — |

The split is stark:

- **Recurring merchants: 47/47 correct (100%).** Memory and MCC handle the head
  of the distribution exactly, which is precisely the §5.4 premise.
- **Novel one-off merchants: 7/13 correct (54%).** Every error and every
  abstention is a novel merchant. Nothing the cascade had seen before was
  missed.

Both remaining errors are cases the corpus was built to be unfair about:
`EAST BAY MUD` is a water utility whose name says nothing about water, and
`PARAMOUNT+` is a streaming service with no lexical overlap with anything in
training.

`PARAMOUNT+` is worth singling out, because it is the one residual that no
amount of abstention tuning reaches. It predicts Gas at confidence 0.62 with a
**margin of 0.50 over the runner-up** — verified by bisecting `min_margin` until
the tier falls silent. It is not a borderline call the threshold nearly caught;
it is confidently wrong. No floor on either confidence or margin excludes it
without gutting coverage across everything else.

That is the case that argues for the LLM tier on novel merchants rather than
for further tuning of the statistical one. A classifier working from character
n-grams over 238 examples has no way to know what Paramount+ is; a model with
world knowledge does. It is also a reminder that self-reported confidence is
not a safety mechanism — this prediction is both the most confident error in
the set and completely wrong, which is exactly why the auto-apply decision
rests on measured precision rather than on the tiers' own certainty.

## Finding 1 — the statistical tier was starving the LLM tier (found, fixed, re-measured)

**Status: fixed in `statistical.py` during this phase. Numbers above are
post-fix.**

As originally wired, the statistical tier answered **98.3%** of held-out
transactions, so exactly one transaction would ever have reached the LLM tier.
§5.4's design is "the LLM handles the tail, not the head" — there was no tail
left, because the statistical tier had guessed it. All five cascade errors at
the time were novel merchants it had never seen and answered anyway, at
confidence 0.17–0.62.

I measured an abstention sweep and passed it to worker-2, who implemented a
different mechanism: a **margin** — how far ahead of the runner-up the winning
class must be — at 0.10, rather than the floor on the winning posterior I had
proposed.

**End to end the two are indistinguishable on this corpus.** Margin 0.10 and a
posterior floor of 0.30 both produce 93.3% cascade coverage, 96.4% precision
and 90.0% accuracy — measured separately, on my sweep and on theirs. Memory and
MCC have already absorbed the easy cases, so both rules make identical calls on
what is left. Nothing in the numbers below distinguishes them, and the original
recommendation reproduces exactly.

The margin wins on two things that this corpus cannot show. On the tier in
isolation it scores about 2 points more precision at matched coverage without
costing tier-level top-1 accuracy. More importantly the argument is structural:
with ten classes an absolute posterior is diluted by however many accounts are
partially plausible, so it cannot distinguish "torn between two accounts" from
"evidence smeared across eight", and its correct setting drifts as the ledger
opens more accounts. A margin is invariant to how the remaining mass is spread,
so it should degrade more gracefully in Phase 6. That is a reasoned prediction,
not a measured result.

Effect on the cascade, end to end:

| | before | after |
|---|---|---|
| statistical coverage | 98.3% | 86.7% |
| statistical precision | 84.7% | 96.2% |
| cascade precision | 91.5% | **96.4%** |
| cascade accuracy | 90.0% | **90.0%** |
| cascade errors | 5 | **2** |
| routed to the LLM tier | 1 | **4** |

**The fix was free.** End-to-end accuracy did not move, because every
prediction it suppressed was a wrong one. Precision rose 4.9 points and four
genuinely-novel merchants now reach the tier designed for them.

Worth being clear about what this does *not* do: it raises precision but cannot
by itself unlock auto-apply, because the held-out sample is still too small for
the lower bound to clear. It also does not improve accuracy — those four
transactions are now abstentions rather than correct answers, and turning them
into correct answers is the LLM tier's job, which this evaluation does not
measure.

## Finding 2 — the memory tier reads global state (leakage vector, now controlled)

`MemoryCategorizer` is backed by `data/memory.json` and deliberately decoupled
from `LedgerContext.examples`. The rationale in `memory.py` is reasonable on
its own terms — memory should grow only through explicit human confirmation.
But it means the tier reads **global mutable state that a held-out evaluation
cannot control**, and it breaks measurement in both directions:

- In CI, where `data/memory.json` does not exist, the tier abstains on
  everything and reports 0.0% coverage forever. That is not a measurement.
- On a developer machine where `bookkeeper categorize --apply` has run, the
  file may already contain held-out transactions. Measured on this corpus, the
  inflation is **63.3% coverage → 100.0%**: the tier scores perfectly by having
  been told the answers.

Both were observed directly, not theorized; the first run of this harness
reported memory at 0.0%.

The harness now seeds a training-only table into a temp directory and points
the tier at it via `MemoryCategorizer(path=...)`, so the reported 63.3%/100.0%
is honest and independent of local state.
`test_run_eval_does_not_read_the_real_memory_file` plants a contaminated
`memory.json` and asserts eval results do not move.

Worth flagging beyond the harness: **`data/memory.json` is not gitignored** and
is intended to be git-tracked as an audit trail. That is a deliberate design
choice, but it does mean anything that writes to it during development becomes
part of the committed state that future evals must be insulated from. The
harness is insulated; other consumers may not be.

## The demo server, for comparison — a degenerate case

Measured directly from `data/raw/simplefin-2026-07-30T18:55:51Z.json`:

| | |
|---|---|
| transactions | 338 |
| distinct descriptions | **3** |
| distinct MCCs | 2 (`5812`, `5411`) |
| MCC coverage | 97.0% |
| majority-class baseline | 48.5% |

The three descriptions are `Fishing bait` (164, MCC 5812), `Grocery store`
(164, MCC 5411), and `Pay day!` (10, no MCC). The labels are incoherent:
fishing bait carries MCC 5812, *Eating Places*.

**No accuracy measured on this data means anything.** With three distinct
strings, the memory tier reaches 100% after three confirmations, so "tiers 1–3
beat a majority-class baseline" passes vacuously while measuring nothing about
the cascade. This is why the synthetic corpus exists and why it is the primary
eval fixture. These demo figures are reported here only so nobody re-derives
them and mistakes them for a result.

## CI regression gate

§5.5 requires eval to run in CI with an accuracy drop failing the build.
Implemented as tests in `sidecar/tests/test_categorize_eval.py`:

- `test_regression_gate_non_llm_cascade_beats_the_majority_baseline` — the exit
  criterion itself.
- `test_regression_gate_accuracy_does_not_drop_below_the_committed_floor` —
  asserts non-LLM cascade accuracy ≥ **0.85**, set from the measured 90.0% with
  5 points of headroom. It is a floor, not a target; lowering it needs a
  recorded reason here.
- `test_corpus_is_not_degenerate` — guards the fixture against being
  "simplified" back toward the demo server's shape (asserts ≥250 entries, ≥200
  distinct descriptions, majority class <40%). Without this the exit criterion
  could go quietly vacuous through an innocent-looking fixture edit.

**Enforced as of Phase 5** by `.github/workflows/ci.yml`. Until then there was
no CI configuration of any kind in this repository and the gate was real but
unenforced — run only by whoever remembered to run `uv run pytest`.

The workflow runs the three gate tests **by explicit node id, as their own
named step, before the rest of the suite**, so a §5.5 regression shows up as a
red step called "Accuracy regression gate (PLAN.md 5.5)" rather than as one
failure among ~590 dots.

Node ids rather than `-k`. The original reasoning given here was that a `-k`
expression matching nothing "selects zero tests and exits green" — **that is
wrong, and measured to be wrong**: on this pytest a `-k` matching nothing
exits **5** (no tests collected) and a stale node id exits **4** (usage
error). Both fail the build, so the gate could not have gone quiet either
way. Node ids are still the better choice — exit 4 names a broken selector
where exit 5 only says nothing ran, and a node id states which three tests are
the gate rather than leaving it to a pattern — but the justification was
overstated and is corrected here rather than left as a claim a reader would
find false the first time they checked it.

On failure the workflow writes the three possible
causes to the job summary, including the rule that the 0.85 floor is lowered
by recording a reason in this document rather than by editing the constant.

The gate stays hermetic in CI. `use_llm=False` is the eval default, so the
measured tier stack is memory + rule + MCC + statistical and no step reaches
for a model; there is no Ollama on the runner and nothing installs one. The
whole sidecar suite was re-run locally with outbound TCP blocked — including
loopback:11434, on a machine where Ollama *was* running, so a real call would
have failed rather than quietly succeeding — and the only failure was an
unrelated in-flight one. Tier-4 tests are served by `pytest-httpx`.

Two things the workflow enforces that the tests alone do not:

- **`RUN_SIMPLEFIN_INTEGRATION` is never set**, so the one live-network test
  stays skipped rather than becoming a flaky dependency on a third party's
  infrastructure. `pytest -q -rs` prints the skip reason, so "still skipped"
  is visible in the log rather than assumed.
- **The checked-out `ledger/` must be unchanged after the run.** `paths.root()`
  falls back to the repo root when `BOOKKEEPER_ROOT` is unset, so a test that
  forgot the fixture would write to the real ledger. Locally that looks like a
  passing run with a dirty worktree; in CI it is a failed step. This covers
  `ledger/` fully and `data/` only for its tracked contents (`data/memory.json`
  — see Finding 2). `data/raw/` and `data/eval/` are gitignored and a write
  there would not be caught.

Setting `BOOKKEEPER_ROOT` job-wide as a blunter guard was tried and rejected:
`test_starter_rules_yaml_is_valid_against_real_chart_of_accounts` validates the
starter rules against the **real** `ledger/accounts.beancount` on purpose, and
fails when the root is redirected. Reading the committed ledger is intended;
writing to it is what the guard catches.

What remains unenforced: the workflow has never executed on GitHub. Every
command in it was run locally and reported, but the runner environment —
action resolution, cache behaviour, Node 26 and Python 3.12 provisioning — is
confirmed only by the first real run.

## What these numbers are worth

Honestly: **they validate the implementation and tell you almost nothing about
the product.**

What they do establish, and I think establish solidly:

- The cascade is wired correctly and first-hit-wins works.
- The memory tier generalizes across bank manglings of the same merchant
  (63.3% coverage at 100% precision) — the single most load-bearing claim in
  §5.4, since real spending is dominated by recurring merchants.
- The MCC tier abstains on ambiguous codes instead of guessing, which is the
  behaviour that justifies placing it ahead of the statistical tier.
- The held-out plumbing is sound, and the one place leakage could have hidden
  has been found and closed.
- The statistical tier's over-answering was a real design problem, and the
  harness both caught it and confirmed the fix cost nothing.

What they emphatically do not establish:

- **Any real-world accuracy figure.** I wrote both the corpus and the harness.
  The merchant strings are my model of what bank feeds look like, informed by
  §3.1's examples, not a sample of what they *are*. The difficulty I built in
  is the difficulty I thought of; real data will be hard in ways I did not
  anticipate. The 90.0% is a statement about this corpus and nothing else.
- **That 90% will survive contact with real data.** It will almost certainly
  drop. Real feeds carry more merchants, longer tails, more genuine ambiguity,
  and inconsistent user labeling that this corpus does not model at all — every
  entry here has exactly one defensible ground truth except the deliberately
  ambiguous Walgreens and Amazon rows.
- **Anything about the LLM tier.** It was excluded (`use_llm=False`). After the
  Finding 1 fix it would see four transactions out of 60 — enough to matter,
  and far too few to measure. Whether it gets those four right is unmeasured
  and is the obvious next thing to evaluate.

The corpus also flatters the cascade in one specific structural way worth
naming: because I generated recurring merchants from a small set of surface
forms, the memory tier's job is easier than it would be against real strings
with more mangling variety. 240 distinct descriptions across 298 transactions
is a reasonable ratio, but the *kinds* of variation are limited to the ones I
enumerated.

The single most useful thing this exercise produced is not the 90.0%. It is the
two defects in Finding 1 and Finding 2 — both of which would have been
invisible without a harness that separates coverage from precision and controls
the held-out set, and one of which was fixed and re-measured within the phase —
and the fact that the auto-apply decision came out **off**, on a measured
basis, rather than being asserted either way.
