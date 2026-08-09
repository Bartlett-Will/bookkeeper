"""Unit tests for `suggest_rules`.

This module proposes edits to the user's own categorization rules. The
failure it must never have is not "a weak suggestion" -- it is writing one,
or making a confident claim about a suggestion's effect that turns out to be
false. So the fixture is built so that every claim in `rulesuggest.py`'s
docstring is separately falsifiable here:

- `SAFEWAY` is mangled three ways (`SQ *SAFEWAY 1234`, `SAFEWAY #1234`,
  `SAFEWAY STORE 4471`, `SAFEWAY.COM`), which is the whole case a rule exists
  for: tier 1 sees three keys, and one regex is one statement. It also has
  `SAFEWAY FUEL` already booked elsewhere in the ledger, so the pattern is
  provably broader than the merchant.
- `BLUE BOTTLE` is 8 transactions worth 32.00; `STATE FARM` is 5 worth
  2500.00. Count and money do not merely differ, they invert, so the ranking
  cannot be right by accident.
- `AMAZON` and `AMAZON WEB SERVICES` share a first token and *disagree* about
  their account -- a collision, not a merchant.
- `MUNI TRANSIT` has no memory entry and existing ledger postings, so the
  second evidence source is exercised on its own.
- `PORTOLA ROASTERS` has neither, and must come back undecided rather than
  given an invented account.
- `CHEZ PANISSE` is 2 transactions: below the bar, and declined.

Abstention and refusal are asserted as distinct outcomes throughout, in the
shape `test_reports_trends.py` established. "No merchant qualifies" is an
answer; a suggestion nobody can check is not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from beancount import loader

from bookkeeper.categorize.context import uncategorized_transactions
from bookkeeper.categorize.normalize import normalize_description
from bookkeeper.categorize.rules import _compile_rule, _matches
from bookkeeper.tuning.rulesuggest import (
    CONFLICT_CONFIRMED_MEMORY,
    CONFLICT_LEDGER_HISTORY,
    CONFLICT_SHADOWED,
    EVIDENCE_CONFIRMED_MEMORY,
    EVIDENCE_LEDGER_HISTORY,
    MIN_OCCURRENCES,
    RANKED_BY,
    suggest_rules,
)

LEDGER = """\
option "title" "rule suggestion fixture"
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking            USD
2026-01-01 open Expenses:Food:Groceries    USD
2026-01-01 open Expenses:Food:Coffee       USD
2026-01-01 open Expenses:Cloud             USD
2026-01-01 open Expenses:Shopping          USD
2026-01-01 open Expenses:Insurance         USD
2026-01-01 open Expenses:Transit           USD
2026-01-01 open Expenses:Unknown           USD

2026-01-01 * "Opening balance"
  Assets:Checking                        40000.00 USD
  Equity:Opening-Balances

;; --- already categorized, so the ledger can disagree with a pattern --------
;; SAFEWAY FUEL is a real gas station booked to transit. The Safeway grocery
;; pattern reaches it, which is the "broader than the merchant" conflict.
2026-01-04 * "SAFEWAY FUEL"
  Assets:Checking                          -60.00 USD
  Expenses:Transit
2026-01-05 * "SAFEWAY FUEL"
  Assets:Checking                          -60.00 USD
  Expenses:Transit
2026-01-06 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Transit
2026-01-07 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Transit
2026-01-08 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Transit

;; --- one merchant, three normalized keys -----------------------------------
2026-02-01 * "SQ *SAFEWAY 1234"
  Assets:Checking                          -35.00 USD
  Expenses:Unknown
2026-02-08 * "SAFEWAY #1234"
  Assets:Checking                          -42.00 USD
  Expenses:Unknown
2026-02-15 * "SAFEWAY STORE 4471"
  Assets:Checking                          -28.00 USD
  Expenses:Unknown
2026-03-01 * "SAFEWAY STORE 4471"
  Assets:Checking                          -31.00 USD
  Expenses:Unknown
2026-03-08 * "SAFEWAY.COM"
  Assets:Checking                          -55.00 USD
  Expenses:Unknown
2026-03-15 * "SAFEWAY.COM"
  Assets:Checking                          -19.00 USD
  Expenses:Unknown

;; --- many, small: wins on count, loses on money ----------------------------
2026-02-02 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-03 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-04 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-05 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-06 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-07 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-09 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown
2026-02-10 * "BLUE BOTTLE"
  Assets:Checking                           -4.00 USD
  Expenses:Unknown

;; --- few, large: wins on money, loses on count -----------------------------
2026-02-11 * "STATE FARM"
  Assets:Checking                         -500.00 USD
  Expenses:Unknown
2026-03-11 * "STATE FARM"
  Assets:Checking                         -500.00 USD
  Expenses:Unknown
2026-04-11 * "STATE FARM"
  Assets:Checking                         -500.00 USD
  Expenses:Unknown
2026-05-11 * "STATE FARM"
  Assets:Checking                         -500.00 USD
  Expenses:Unknown
2026-06-11 * "STATE FARM"
  Assets:Checking                         -500.00 USD
  Expenses:Unknown

;; --- below the bar: known merchant, too few transactions -------------------
2026-02-12 * "CHEZ PANISSE"
  Assets:Checking                         -180.00 USD
  Expenses:Unknown
2026-03-12 * "CHEZ PANISSE"
  Assets:Checking                         -220.00 USD
  Expenses:Unknown

;; --- a shared first token that is a collision, not a merchant --------------
2026-02-13 * "AMAZON"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-02-14 * "AMAZON"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-02-16 * "AMAZON"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-02-17 * "AMAZON"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-02-18 * "AMAZON"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-02-19 * "AMAZON"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-02-20 * "AMAZON WEB SERVICES"
  Assets:Checking                          -80.00 USD
  Expenses:Unknown
2026-02-21 * "AMAZON WEB SERVICES"
  Assets:Checking                          -80.00 USD
  Expenses:Unknown
2026-02-22 * "AMAZON WEB SERVICES"
  Assets:Checking                          -80.00 USD
  Expenses:Unknown
2026-02-23 * "AMAZON WEB SERVICES"
  Assets:Checking                          -80.00 USD
  Expenses:Unknown
2026-02-24 * "AMAZON WEB SERVICES"
  Assets:Checking                          -80.00 USD
  Expenses:Unknown
2026-02-25 * "AMAZON WEB SERVICES"
  Assets:Checking                          -80.00 USD
  Expenses:Unknown

;; --- evidence from the ledger, with nothing in memory ----------------------
2026-02-26 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Unknown
2026-02-27 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Unknown
2026-02-28 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Unknown
2026-03-02 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Unknown
2026-03-03 * "MUNI TRANSIT"
  Assets:Checking                          -10.00 USD
  Expenses:Unknown

;; --- big enough to matter, and nothing has decided an account --------------
2026-03-04 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown
2026-03-05 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown
2026-03-06 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown
2026-03-07 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown
2026-03-09 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown
2026-03-10 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown
2026-03-13 * "PORTOLA ROASTERS"
  Assets:Checking                          -12.00 USD
  Expenses:Unknown

;; --- no token to build a pattern from --------------------------------------
2026-03-14 * "999999"
  Assets:Checking                           -7.00 USD
  Expenses:Unknown
"""

#: Two transactions of one known merchant. Nothing here clears the bar, which
#: is the state the demo ledger is in and the answer the module must give
#: rather than lowering its own threshold to have something to say.
THIN_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking          USD
2026-01-01 open Expenses:Food:Groceries  USD
2026-01-01 open Expenses:Unknown         USD

2026-01-01 * "Opening balance"
  Assets:Checking                       1000.00 USD
  Equity:Opening-Balances

2026-01-05 * "SAFEWAY"
  Assets:Checking                        -35.00 USD
  Expenses:Unknown
2026-02-05 * "SAFEWAY"
  Assets:Checking                        -42.00 USD
  Expenses:Unknown
"""

MEMORY = {
    "safeway": {"Expenses:Food:Groceries": 3},
    "blue bottle": {"Expenses:Food:Coffee": 2},
    "state farm": {"Expenses:Insurance": 1},
    "chez panisse": {"Expenses:Food:Groceries": 1},
    "amazon": {"Expenses:Shopping": 4},
    "amazon web services": {"Expenses:Cloud": 4},
}

#: An existing rule that already claims every Blue Bottle transaction, so the
#: suggestion for it would be appended behind a rule that always wins.
SHADOWING_RULES = """\
- name: "Blue Bottle Coffee"
  pattern: "BLUE BOTTLE"
  account: "Expenses:Food:Coffee"
"""

BROKEN_RULES = """\
- name: "Unclosable"
  pattern: "SAFEWAY(unbalanced"
  account: "Expenses:Food:Groceries"
"""

RULES_TARGETING_A_CLOSED_ACCOUNT = """\
- name: "Gone"
  pattern: "SAFEWAY"
  account: "Expenses:Renamed:Away"
"""


def _load(text):
    entries, errors, _options = loader.load_string(text)
    assert not errors, errors
    return entries


@pytest.fixture(scope="module")
def entries():
    return _load(LEDGER)


@pytest.fixture(scope="module")
def thin_entries():
    return _load(THIN_LEDGER)


@pytest.fixture(scope="module")
def memory_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("memory") / "memory.json"
    path.write_text(json.dumps(MEMORY, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def rule_files(tmp_path_factory):
    """The several `rules.yaml` states a suggestion has to cope with."""
    directory = tmp_path_factory.mktemp("rules")
    written = {
        "shadowing": SHADOWING_RULES,
        "broken": BROKEN_RULES,
        "closed_account": RULES_TARGETING_A_CLOSED_ACCOUNT,
    }
    paths = {name: directory / f"{name}.yaml" for name in written}
    for name, text in written.items():
        paths[name].write_text(text, encoding="utf-8")
    # A path that does not exist: `RuleCategorizer` treats a missing file as
    # "no rules", which is the ordinary state before a user writes any.
    paths["absent"] = directory / "absent.yaml"
    return paths


def run(entries, memory_file, rules_path, **kwargs):
    return suggest_rules(entries=entries, memory_path=memory_file, rules_path=rules_path, **kwargs)


@pytest.fixture(scope="module")
def result(entries, memory_file, rule_files):
    return run(entries, memory_file, rule_files["absent"])


@pytest.fixture(scope="module")
def shadowed(entries, memory_file, rule_files):
    return run(entries, memory_file, rule_files["shadowing"])


def named(result, name):
    return next(s for s in result.suggestions if s.name == name)


# --- nothing is ever written ------------------------------------------------
#
# The single most important property in this file. §5.5 raises autonomy only
# on measured evidence and decision 5 makes review-everything the shipped
# default, so software editing a user's categorization rules unasked is not a
# bug to be graded, it is the thing that must not happen.


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under `root`, with file contents. Directories map to None."""
    return {
        path.relative_to(root).as_posix(): (None if path.is_dir() else path.read_bytes())
        for path in root.rglob("*")
    }


@pytest.fixture
def disk_root(tmp_path, monkeypatch):
    """A `BOOKKEEPER_ROOT` tree holding a ledger, a memory and a rules file.

    Deliberately the *default* path resolution rather than the explicit
    `rules_path` / `memory_path` arguments: the arguments are what the tests
    above use for convenience, and this is the arrangement a real run has.
    """
    root = tmp_path / "root"
    (root / "ledger").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    (root / "ledger" / "main.beancount").write_text(LEDGER, encoding="utf-8")
    (root / "data" / "memory.json").write_text(
        json.dumps(MEMORY, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "data" / "rules.yaml").write_text(SHADOWING_RULES, encoding="utf-8")
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
    return root


def test_suggest_rules_writes_nothing_at_all(disk_root):
    """Not `data/rules.yaml`, not `data/memory.json`, not the ledger, nothing.

    Asserted over the whole tree byte for byte rather than over the two files
    that would be the obvious targets, because the next thing a future change
    writes will be a third file -- a `.suggestions.json` cache, a backup of
    the rules it "helpfully" rewrote. The set of paths is compared as well as
    their contents, so a created file fails even if nothing existing changed.
    """
    before = _snapshot(disk_root)

    outcome = suggest_rules()

    assert outcome.ok is True
    assert outcome.suggestions, "the fixture must actually produce suggestions here"
    assert _snapshot(disk_root) == before


def test_applied_is_false_on_every_path(entries, thin_entries, memory_file, rule_files):
    """A field, not a docstring promise, so a change that starts writing has
    to also lie in its own response to go unnoticed. Checked on the success
    path, the nothing-qualifies path and the failure path alike."""
    for outcome in (
        run(entries, memory_file, rule_files["absent"]),
        run(thin_entries, memory_file, rule_files["absent"]),
        run(entries, memory_file, rule_files["broken"]),
    ):
        assert outcome.applied is False
        assert outcome.to_dict()["applied"] is False


def test_the_response_never_claims_to_have_changed_anything(result):
    """`render()` is what a CLI user reads, and it states the disposition
    before it states the suggestions."""
    text = result.render()

    assert "Nothing was written." in text
    assert "Paste a block below into data/rules.yaml to accept it." in text


# --- it declines when nothing qualifies -------------------------------------


def test_a_ledger_where_nothing_clears_the_bar_says_so(thin_entries, memory_file, rule_files):
    """Abstention is a first-class answer here, as in `trends.py`. Two
    transactions of a known merchant is exactly the state tier 1 already
    handles, and a rule proposed anyway would be machinery bought for
    nothing."""
    outcome = run(thin_entries, memory_file, rule_files["absent"])

    assert outcome.ok is True
    assert outcome.suggestions == ()
    assert outcome.shown == 0
    assert outcome.covered_by_suggestions == 0
    assert "No merchant clears the bar for a rule." in outcome.render()


def test_a_merchant_below_the_bar_is_declined_not_invisible(result):
    """`CHEZ PANISSE` is seen twice and has a confirmed memory, so it is
    known, counted and deliberately not suggested. Counting it in
    `merchants_seen` is what makes this a threshold rather than a blind
    spot -- a reader can see the module looked and said no."""
    assert not any("Chez" in s.name for s in result.suggestions)
    assert result.merchants_seen == 10
    assert result.uncategorized_total == 46


def test_lowering_the_bar_deliberately_admits_the_same_merchant(
    entries, memory_file, rule_files
):
    """The counterpart that stops the test above from passing for the wrong
    reason. If `CHEZ PANISSE` were absent because of a grouping or evidence
    failure rather than the threshold, dropping the threshold would not
    produce it."""
    outcome = run(entries, memory_file, rule_files["absent"], min_occurrences=2)

    chez = named(outcome, "Chez Panisse")
    assert chez.occurrences == 2
    assert chez.account == "Expenses:Food:Groceries"
    assert outcome.min_occurrences == 2


def test_the_bar_it_applied_is_stated_in_the_response(result):
    """So a reader can disagree with an editorial threshold rather than
    having to infer it from what is missing."""
    assert result.min_occurrences == MIN_OCCURRENCES
    assert f"at least {MIN_OCCURRENCES} to qualify" in result.render()


def test_every_suggestion_actually_clears_the_stated_bar(result):
    for s in result.suggestions:
        assert s.occurrences >= result.min_occurrences, s.name


# --- ranking ----------------------------------------------------------------


def test_ranking_is_by_occurrences_and_money_would_invert_it(result):
    """A merchant seen 8 times is worth a rule; one seen 5 times is less so,
    however large the 5.

    `BLUE BOTTLE` and `STATE FARM` are the pair the fixture exists to carry:
    32.00 across 8 transactions against 2500.00 across 5, so the two orders do
    not merely differ, they put the same pair at opposite ends. Asserted on
    that pair rather than on whichever suggestion happens to top the list,
    because the top slot is a fact about the whole fixture's composition and
    this is a fact about the ranking key.
    """
    blue = named(result, "Blue Bottle")
    state_farm = named(result, "State Farm")
    assert (blue.occurrences, state_farm.occurrences) == (8, 5)
    assert blue.total_amount < state_farm.total_amount

    by_rank = [s.name for s in result.suggestions]
    by_money = [s.name for s in sorted(result.suggestions, key=lambda s: -s.total_amount)]

    assert by_rank.index("Blue Bottle") < by_rank.index("State Farm")
    assert by_money.index("State Farm") < by_money.index("Blue Bottle")
    assert by_rank != by_money


def test_the_highest_ranked_suggestion_is_not_the_recommended_one(result):
    """Ranking maximises transactions removed from the review queue. It is not
    an endorsement, and on this fixture the two come apart.

    `Amazon` tops the list at 12 transactions -- honestly, since `pattern:
    "amazon"` really does reach the six `AMAZON WEB SERVICES` transactions
    too -- and it is precisely the suggestion a user must not paste, because
    those six carry a confirmed memory of a different account. A reader who
    took position 1 as advice would accept the worst rule in the report, so
    the conflict has to travel with the rank.
    """
    top = result.suggestions[0]

    assert top.name == "Amazon"
    assert top.occurrences == 12
    assert top.conflict_free is False
    assert any(c.kind == CONFLICT_CONFIRMED_MEMORY for c in top.conflicts)


def test_the_order_is_total_and_stable(result):
    """Occurrences first, then total amount, then the pattern. Stated as the
    exact sort key so a later change to it has to be deliberate."""
    keys = [(-s.occurrences, -s.total_amount, s.pattern) for s in result.suggestions]

    assert keys == sorted(keys)


def test_the_ranking_it_used_is_named_in_the_response(result):
    assert result.ranked_by == RANKED_BY == "occurrences"
    assert "ranked by occurrences" in result.render()


def test_money_is_reported_even_though_it_is_not_ranked_on(result):
    """Amount answers a different question -- what getting it wrong costs --
    so a reader who wants that order has the numbers to build it."""
    state_farm = named(result, "State Farm")

    assert state_farm.total_amount == pytest.approx(2500)
    assert str(state_farm.total_amount) == "2500.00"
    assert str(state_farm.median_amount) == "500.00"


def test_undecided_merchants_are_ranked_the_same_way(result):
    keys = [(-u.occurrences, -u.total_amount, u.normalized_keys) for u in result.undecided]

    assert keys == sorted(keys)


# --- grouping matches tier 1 ------------------------------------------------


def test_two_manglings_of_one_merchant_are_one_group(result):
    """The property, not the function call: `SQ *SAFEWAY 1234` and
    `SAFEWAY #1234` are the same merchant to tier 1, so they must be the same
    merchant here. A module that grouped differently would propose rules for
    merchants the memory tier does not consider the same merchant, and the
    two would then disagree about which transactions a confirmation covers."""
    shared_key = normalize_description("SQ *SAFEWAY 1234")
    assert shared_key == normalize_description("SAFEWAY #1234")

    safeway = named(result, "Safeway")
    # Group membership, not merely a pattern that happens to reach both: the
    # key tier 1 would index has to be a key this suggestion declares. A rule
    # whose pattern covers two transactions that tier 1 files separately is a
    # different (and weaker) thing than one merchant seen two ways.
    assert shared_key in safeway.normalized_keys
    assert "SQ *SAFEWAY 1234" in safeway.sample_descriptions
    assert "SAFEWAY #1234" in safeway.sample_descriptions
    assert safeway.occurrences == 6


def test_the_pattern_generalizes_over_the_keys_tier_one_sees_separately(result):
    """Three keys, one rule. That collapse is the only thing a rule buys over
    tier 1's exact index, so it is the thing to pin."""
    safeway = named(result, "Safeway")

    assert safeway.normalized_keys == ("safeway", "safeway com", "safeway store")
    assert safeway.pattern == "safeway"


def test_every_reported_key_is_one_tier_one_would_index(result, entries):
    """A key this module invented is a key no confirmation can ever match.
    Checked against `normalize_description` over the real backlog rather than
    against a hand-written list, so it holds for keys the fixture grows."""
    real_keys = {normalize_description(t.description) for t in uncategorized_transactions(entries)}

    for s in result.suggestions:
        assert set(s.normalized_keys) <= real_keys, s.name
        assert set(s.keys_with_evidence) <= set(s.normalized_keys), s.name
    for u in result.undecided:
        assert set(u.normalized_keys) <= real_keys


def test_a_description_with_no_token_founds_no_pattern_and_is_declared(result):
    """`999999` normalizes to nothing. Excluding it silently would leave the
    figures not adding up with no explanation for the gap."""
    assert any("normalizes to nothing" in w for w in result.warnings)
    assert any("excluded from every figure here" in w for w in result.warnings)


# --- where the account comes from -------------------------------------------


def test_a_confirmed_memory_is_the_strongest_evidence(result):
    safeway = named(result, "Safeway")

    assert safeway.evidence == EVIDENCE_CONFIRMED_MEMORY
    assert safeway.account == "Expenses:Food:Groceries"
    assert 'prior confirmation(s) for "safeway"' in safeway.evidence_detail


def test_ledger_history_is_used_when_memory_has_nothing(result):
    """`MUNI TRANSIT` is in no memory entry, and three existing postings name
    an account for it."""
    muni = named(result, "Muni Transit")

    assert muni.evidence == EVIDENCE_LEDGER_HISTORY
    assert muni.account == "Expenses:Transit"
    assert '3/3 existing posting(s) for "muni transit" are booked here' in muni.evidence_detail


def test_a_merchant_with_no_evidence_is_never_given_an_invented_account(result):
    """A rule matches at confidence 1.0 -- the user stating a fact -- so an
    account proposed from a fitted estimate would launder a guess into a
    certainty. `PORTOLA ROASTERS` is 7 transactions and comes back undecided."""
    assert not any("Portola" in s.name for s in result.suggestions)

    portola = next(u for u in result.undecided if u.normalized_keys == ("portola roasters",))
    assert portola.occurrences == 7
    assert "no confirmed memory and no existing ledger posting" in portola.reason
    assert "categorize" in portola.reason


def test_a_cluster_reaching_keys_nothing_decided_says_which(result):
    """The Safeway rule is proposed off one confirmed key and fires on three.
    That is the generalization the rule buys, and the risk it carries; a
    reader who is not told cannot weigh it."""
    safeway = named(result, "Safeway")

    assert safeway.keys_with_evidence == ("safeway",)
    assert "the pattern also reaches 2 key(s) nothing has decided on" in safeway.evidence_detail
    assert "safeway com, safeway store" in safeway.evidence_detail


def test_a_first_token_collision_is_split_rather_than_resolved(result):
    """`amazon` and `amazon web services` share a first word and disagree
    about their account. Resolving that would file one merchant under
    another's account on the strength of a shared word."""
    retail = named(result, "Amazon")
    cloud = named(result, "Amazon Web Services")

    assert retail.account == "Expenses:Shopping"
    assert cloud.account == "Expenses:Cloud"
    assert retail.normalized_keys == ("amazon",)
    assert cloud.normalized_keys == ("amazon web services",)


def test_the_sign_is_read_off_the_transactions_not_assumed(result):
    """Every fixture merchant is spending only, so every rule narrows to
    `negative` -- which stops it also claiming a refund from that merchant."""
    for s in result.suggestions:
        assert s.sign == "negative", s.name
        assert "sign: negative" in s.yaml


# --- conflicts --------------------------------------------------------------


def test_an_existing_rule_that_shadows_the_suggestion_is_flagged(shadowed):
    """First match wins in file order and a suggestion is appended, so a
    fully-shadowed rule would never fire. Pasting it in would look like an
    action and be a no-op."""
    blue = named(shadowed, "Blue Bottle")
    conflict = next(c for c in blue.conflicts if c.kind == CONFLICT_SHADOWED)

    assert conflict.count == blue.occurrences == 8
    assert "already handled by the existing rule Blue Bottle Coffee" in conflict.detail
    assert "would never fire at all" in conflict.detail
    assert blue.conflict_free is False


def test_shadowing_is_only_reported_when_a_rule_actually_shadows(result, shadowed):
    """Without that rules.yaml the same suggestion is clean, so the flag is
    about the file and not about the merchant."""
    assert named(result, "Blue Bottle").conflicts == ()
    assert any(c.kind == CONFLICT_SHADOWED for c in named(shadowed, "Blue Bottle").conflicts)


def test_a_pattern_broader_than_its_merchant_is_flagged_against_the_ledger(result):
    """`SAFEWAY FUEL` is booked to transit and the grocery pattern reaches it.
    Either the pattern is too broad or the merchant belongs somewhere else,
    and both are the user's call, not this module's."""
    safeway = named(result, "Safeway")
    conflict = next(c for c in safeway.conflicts if c.kind == CONFLICT_LEDGER_HISTORY)

    assert conflict.account == "Expenses:Transit"
    assert conflict.count == 2
    assert "already in the ledger match this pattern" in conflict.detail
    assert safeway.already_categorized_matches == 2


def test_a_conflict_is_sized_in_transactions_so_it_can_be_weighed(result, shadowed):
    """"Matches 40, of which 12 contradict a confirmation" is a judgement a
    reader can make. "Matches 40, has a conflict" is not."""
    for outcome in (result, shadowed):
        for s in outcome.suggestions:
            for c in s.conflicts:
                assert c.count > 0, (s.name, c.kind)
                assert c.detail, (s.name, c.kind)


def test_conflict_free_reports_a_fact_and_never_a_verdict(result, shadowed):
    """Named for what was measured. `recommended` would be this module
    telling a user what to do, which is the one thing a suggestion may not
    do."""
    for outcome in (result, shadowed):
        for s in outcome.suggestions:
            assert s.conflict_free == (not s.conflicts), s.name
        text = outcome.render().casefold()
        assert "we recommend" not in text
        assert "you should" not in text


def test_a_suggestion_contradicting_a_confirmed_memory_is_flagged(result):
    """The conflict `rulesuggest.py` calls the dangerous one.

    The `Amazon` rule is `pattern: "amazon"` for `Expenses:Shopping`. It also
    matches six `AMAZON WEB SERVICES` transactions whose confirmed memory says
    `Expenses:Cloud`. Those particular transactions stay correct because tier
    1 runs first, which is precisely why it is dangerous rather than harmless:
    the rule fires on every other mangling of that merchant and silently
    contradicts a decision the user made by hand.
    """
    retail = named(result, "Amazon")
    conflict = next(c for c in retail.conflicts if c.kind == CONFLICT_CONFIRMED_MEMORY)

    assert conflict.account == "Expenses:Cloud"
    assert conflict.count == 6
    assert retail.conflict_free is False


# --- the one claim the module makes, checked the way it says to check it ----


def _matched_indices(suggestion, backlog):
    """Which backlog transactions the printed YAML actually claims.

    Compiled through tier 2's compiler and run through tier 2's matcher, so
    this is what the rule does rather than a second model of it.
    """
    block = yaml.safe_load(suggestion.yaml)
    assert len(block) == 1, suggestion.name
    compiled = _compile_rule(block[0], 0)
    return {index for index, txn in enumerate(backlog) if _matches(compiled, txn)}


@pytest.mark.parametrize(
    ("rules", "min_occurrences"),
    [("absent", None), ("shadowing", None), ("absent", 2), ("absent", 1)],
)
def test_the_claimed_count_is_reproduced_by_running_the_printed_yaml(
    entries, memory_file, rule_files, rules, min_occurrences
):
    """The module's own falsifiable claim, verified by its own instructions:
    "the match counts are reproduced by running the printed YAML".

    Asserted as a general property over every suggestion rather than over one
    example, and across four thresholds, because lowering the bar admits
    narrower merchants whose patterns have more room to over-reach -- at
    `min_occurrences=1` every normalized key with any evidence becomes a
    candidate. This is the equality that failed for exactly one suggestion
    before the transaction pool was widened, and the one a future change to
    clustering or pattern-building would break first.
    """
    backlog = uncategorized_transactions(entries)
    outcome = run(
        entries, memory_file, rule_files[rules], min_occurrences=min_occurrences
    )
    assert outcome.suggestions, (rules, min_occurrences)

    for s in outcome.suggestions:
        assert len(_matched_indices(s, backlog)) == s.occurrences, s.name


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (reported, not fixed): `covered_by_suggestions` is "
        "`sum(s.occurrences)`, which double-counts a transaction two patterns "
        "both reach. Since the pool was widened it reports 42 of a 46-transaction "
        "backlog where 36 distinct transactions are covered -- 91% claimed "
        "against 78% actual. The overlap is `amazon` also reaching the six "
        "`AMAZON WEB SERVICES` transactions. On real data with more overlapping "
        "patterns the figure can exceed `uncategorized_total` outright. Remove "
        "this marker when the total is taken over distinct transactions."
    ),
)
def test_the_covered_count_does_not_double_count_a_transaction(result, entries):
    """"These suggestions clear N of your backlog" is the headline number a
    user decides how much of this report to act on by, and it is the one
    figure here that is a claim about the set rather than about one rule.

    Two suggestions legitimately reach the same transaction -- that is what
    the collision split produces, and neither rule is wrong to claim it -- so
    the total has to be a union, not a sum.
    """
    backlog = uncategorized_transactions(entries)
    covered = set().union(*(_matched_indices(s, backlog) for s in result.suggestions))

    assert len(covered) == 36
    assert sum(s.occurrences for s in result.suggestions) == 42
    assert result.covered_by_suggestions == len(covered)


def test_the_printed_yaml_parses_to_the_fields_that_were_measured(result):
    """A block that YAML reads differently from what was measured would make
    every count on the card describe a rule the user did not paste."""
    for s in result.suggestions:
        block = yaml.safe_load(s.yaml)[0]

        assert block["pattern"] == s.pattern, s.name
        assert block["account"] == s.account, s.name
        assert block["name"] == s.name
        assert block.get("sign") == s.sign, s.name
        # Compiling it is part of the claim: a regex carrying `[`, `*` or `\`
        # must survive the round trip.
        assert _compile_rule(block, 0).pattern.pattern == s.pattern, s.name


def test_the_pattern_matches_every_raw_description_it_was_derived_from(result):
    """Normalization collapses punctuation, so a pattern rebuilt by joining
    normalized tokens with a literal space would not match `PG&E` or
    `SAFEWAY.COM`. Asserted against the raw bank strings on the card."""
    for s in result.suggestions:
        compiled = re.compile(s.pattern, re.IGNORECASE)
        for description in s.sample_descriptions:
            assert compiled.search(description) is not None, (s.name, description)


# --- refusals ---------------------------------------------------------------


def test_an_unparseable_rules_yaml_fails_rather_than_being_worked_around(
    entries, memory_file, rule_files
):
    """Suggesting against a file that does not parse would produce shadow
    counts of zero and quietly claim no existing rule conflicts."""
    outcome = run(entries, memory_file, rule_files["broken"])

    assert outcome.ok is False
    assert outcome.suggestions == ()
    assert "existing rules.yaml is unusable" in outcome.errors[0]
    assert outcome.render().startswith("rule suggestions failed:")


def test_a_rule_targeting_an_account_that_is_not_open_fails_the_report(
    entries, memory_file, rule_files
):
    """A configuration bug that misfiles transactions quietly. Tier 2 raises
    it during shadow evaluation and this module must not paper over it."""
    outcome = run(entries, memory_file, rule_files["closed_account"])

    assert outcome.ok is False
    assert "not open in the ledger" in outcome.errors[0]


# --- limit ------------------------------------------------------------------


def test_a_limit_trims_the_list_and_the_response_says_what_was_shown(
    result, entries, memory_file, rule_files
):
    """A limit takes the top of the ranking, not an arbitrary two.

    Asserted against the unlimited run's own first two rather than only
    against hard-coded names, so this keeps testing "the top of the list"
    if the fixture's composition changes again.
    """
    outcome = run(entries, memory_file, rule_files["absent"], limit=2)

    assert outcome.shown == 2
    assert len(outcome.suggestions) == 2
    assert [s.name for s in outcome.suggestions] == [s.name for s in result.suggestions[:2]]
    assert [s.name for s in outcome.suggestions] == ["Amazon", "Blue Bottle"]
    assert outcome.covered_by_suggestions == 20
    # The backlog is still described in full: a limit is a display choice and
    # must not shrink the reader's picture of how much is uncategorized.
    assert outcome.uncategorized_total == 46
    assert outcome.merchants_seen == 10


# --- money crosses as a string ----------------------------------------------


#: A number written in exponent form, e.g. `0E+2` or `-1.5e-3`. Matched as a
#: whole string, so prose that merely contains an "e" is not a false positive.
EXPONENT_FORM = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")


def _string_values(value, path=""):
    """Every string in a nested payload, with the path that reached it."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _string_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _string_values(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def test_every_amount_crosses_as_a_quantized_string(result):
    """Money is `Decimal` in the sidecar and a string at the edge, never a
    float to be finished in a browser."""
    payload = result.to_dict()

    for line in payload["suggestions"] + payload["undecided"]:
        for field in ("total_amount", "median_amount"):
            assert isinstance(line[field], str), (line, field)
            assert re.fullmatch(r"-?\d+\.\d{2}", line[field]), (line, field)


@pytest.mark.parametrize("with_rules", [False, True])
def test_no_figure_leaves_this_module_in_exponent_notation(
    entries, thin_entries, memory_file, rule_files, with_rules
):
    """`Decimal` division takes the dividend's exponent less the divisor's, so
    `Decimal(0) / Decimal("150.00")` is `Decimal("0E+2")` and serializes as
    the string `"0E+2"`. That parses as a number and compares as garbage, and
    it reached a browser once in Phase 5.

    Every string in the payload is checked rather than the fields that carry
    money today, because the next one will be a different field.
    """
    rules = rule_files["shadowing" if with_rules else "absent"]
    for source in (entries, thin_entries):
        payload = run(source, memory_file, rules).to_dict()
        offenders = [
            (path, text) for path, text in _string_values(payload) if EXPONENT_FORM.match(text)
        ]
        assert offenders == []


# --- rendering --------------------------------------------------------------


def test_render_shows_the_block_the_evidence_and_the_conflict(shadowed):
    text = shadowed.render()

    assert "46 uncategorized transaction(s) across 10 merchant(s)" in text
    assert '  pattern: "safeway"' in text
    assert '  account: "Expenses:Food:Groceries"' in text
    assert "because: tier 1 has" in text
    assert "CONFLICT (shadowed):" in text
    assert "CONFLICT (ledger_history):" in text
    assert "also matches 2 already-categorized transaction(s)" in text


def test_render_shows_the_undecided_merchants_and_what_to_do_with_them(result):
    """The most actionable line in the report: one confirmation teaches tier 1
    a whole key, and asking again afterwards may turn it into a suggestion."""
    text = result.render()

    assert "1 merchant(s) big enough to matter that nothing has decided an account for" in text
    assert "portola roasters" in text
    assert "PORTOLA ROASTERS" in text


def test_render_surfaces_the_warnings_rather_than_only_the_payload(result):
    text = result.render()

    assert "warnings:" in text
    assert "normalizes to nothing" in text
