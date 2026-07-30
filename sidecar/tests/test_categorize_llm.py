"""Hermetic tests for tier 4. No live Ollama is contacted here.

Every request is served by `pytest-httpx`, so the suite runs identically on a
machine with no model installed -- which is also the property the tier itself
has to have, since PLAN.md §6 Phase 3 requires the cascade to work with tier 4
absent.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from bookkeeper.categorize.llm import LlmCategorizer
from bookkeeper.categorize.models import (
    CategorizationInput,
    LabeledExample,
    LedgerContext,
    Tier,
    predict_is_valid,
)

CHAT_URL = "http://localhost:11434/api/chat"

ACCOUNTS = (
    "Expenses:Food:Coffee",
    "Expenses:Food:Groceries",
    "Expenses:Home:Utilities",
)

CTX = LedgerContext(
    accounts=ACCOUNTS,
    examples=(
        LabeledExample(
            normalized_description="SQ *COFFEE 4TH ST",
            account="Expenses:Food:Coffee",
            description="SQ *COFFEE 4TH ST 8829",
        ),
        LabeledExample(
            normalized_description="SAFEWAY GROCERY",
            account="Expenses:Food:Groceries",
            description="SAFEWAY GROCERY #1174",
        ),
        LabeledExample(
            normalized_description="ACH DEBIT PG&E WEB ONLINE",
            account="Expenses:Home:Utilities",
            description="ACH DEBIT - PG&E WEB ONLINE",
        ),
    ),
)

TXN = CategorizationInput(
    description="SQ *COFFEE 4TH ST 1174",
    amount=Decimal("-4.75"),
    posted_date=date(2026, 7, 30),
    asset_account="Assets:SimpleFIN:Checking",
    simplefin_id="TXN-1",
    mcc="5812",
    payee="Coffee 4th St",
)


def trivial_normalizer(text: str) -> str:
    """Case-fold and collapse whitespace, and nothing else.

    Keeps these tests measuring the wire format and the abstention rules
    rather than the shared normalizer's behaviour.
    """
    return " ".join(text.upper().split())


def make_categorizer(**kwargs) -> LlmCategorizer:
    kwargs.setdefault("normalizer", trivial_normalizer)
    return LlmCategorizer(**kwargs)


def ollama_reply(**payload) -> dict:
    """An Ollama `/api/chat` envelope wrapping a JSON string, as it really is.

    The schema-constrained answer arrives as *text* in `message.content`, not
    as a nested object -- getting that wrong is the single most likely way for
    this tier to work against a mock and fail against the real server.
    """
    return {
        "model": "qwen3:8b",
        "done": True,
        "message": {"role": "assistant", "content": json.dumps(payload)},
    }


def raw_reply(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}}


def sent_body(httpx_mock) -> dict:
    requests = httpx_mock.get_requests()
    assert len(requests) == 1, "tier 4 is single-shot: one request, one answer"
    return json.loads(requests[0].content)


def test_posts_to_the_native_chat_endpoint(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )

    make_categorizer().predict(TXN, CTX)

    request = httpx_mock.get_requests()[0]
    # Not /v1/chat/completions: the §5.3 amendment measured 32s vs 2s on
    # `think`, which the OpenAI-compatible endpoint does not accept at all.
    assert str(request.url) == CHAT_URL
    body = json.loads(request.content)
    assert body["stream"] is False
    assert body["model"] == "qwen3:8b"


def test_format_enum_is_the_open_account_set(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )

    make_categorizer().predict(TXN, CTX)

    schema = sent_body(httpx_mock)["format"]
    assert schema["properties"]["account"]["enum"] == list(ACCOUNTS)
    assert schema["required"] == ["account", "confidence", "reason"]
    assert schema["additionalProperties"] is False


def test_unknown_catch_alls_never_reach_the_enum(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )
    ctx = LedgerContext(
        accounts=(*ACCOUNTS, "Expenses:Unknown", "Income:Unknown"), examples=CTX.examples
    )

    make_categorizer().predict(TXN, ctx)

    enum = sent_body(httpx_mock)["format"]["properties"]["account"]["enum"]
    assert enum == list(ACCOUNTS)


def test_think_is_off_by_default(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )

    make_categorizer().predict(TXN, CTX)

    assert sent_body(httpx_mock)["think"] is False


def test_think_can_be_enabled_for_batch_work(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )

    make_categorizer(think=True, timeout=120.0).predict(TXN, CTX)

    assert sent_body(httpx_mock)["think"] is True


def test_prompt_carries_the_nearest_confirmed_examples(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )

    make_categorizer().predict(TXN, CTX)

    user_message = sent_body(httpx_mock)["messages"][1]["content"]
    assert "SQ *COFFEE 4TH ST 8829" in user_message, "the nearest example is grounding"
    assert "SQ *COFFEE 4TH ST 1174" in user_message, "the transaction being asked about"
    assert "-4.75 USD" in user_message
    assert "5812" in user_message, "MCC is free signal when the feed sends it"
    # Examples sharing no trigram with the query are prompt length, not
    # grounding: the account list already supplies the label vocabulary.
    assert "SAFEWAY GROCERY" not in user_message
    for account in ACCOUNTS:
        assert account in user_message


def test_examples_are_ordered_nearest_last(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.8, reason="coffee"),
    )
    ctx = LedgerContext(
        accounts=ACCOUNTS,
        examples=(
            LabeledExample("SQ *COFFEE 4TH ST", "Expenses:Food:Coffee"),
            LabeledExample("SQ *BAKERY 9TH ST", "Expenses:Food:Groceries"),
        ),
    )

    make_categorizer().predict(TXN, ctx)

    user_message = sent_body(httpx_mock)["messages"][1]["content"]
    # Nearest last: the closest confirmed example sits immediately before the
    # transaction being asked about, where an 8B attends most reliably.
    assert user_message.index("SQ *BAKERY 9TH ST") < user_message.index(
        "SQ *COFFEE 4TH ST"
    )


def test_retrieval_is_capped_and_drops_unrelated_examples():
    noise = tuple(
        LabeledExample(f"UNRELATED MERCHANT {i:03d}", "Expenses:Food:Groceries")
        for i in range(50)
    )
    coffee = tuple(
        LabeledExample(f"SQ *COFFEE 4TH ST {i:04d}", "Expenses:Food:Coffee")
        for i in range(30)
    )
    ctx = LedgerContext(accounts=ACCOUNTS, examples=noise + coffee)

    nearest = make_categorizer(max_examples=20).nearest_examples(TXN, ctx)

    assert len(nearest) == 20
    assert nearest[-1].account == "Expenses:Food:Coffee"
    assert sum(1 for e in nearest if e.account == "Expenses:Food:Coffee") == 20


def test_parses_a_well_formed_response(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(
            account="Expenses:Food:Coffee",
            confidence=0.82,
            reason="matches three confirmed SQ *COFFEE charges",
        ),
    )

    prediction = make_categorizer().predict(TXN, CTX)

    assert prediction is not None
    assert prediction.account == "Expenses:Food:Coffee"
    assert prediction.confidence == pytest.approx(0.82)
    assert prediction.tier is Tier.LLM
    assert "SQ *COFFEE" in prediction.rationale
    assert predict_is_valid(prediction, CTX)


def test_self_reported_confidence_out_of_range_is_clamped_not_rejected(httpx_mock):
    # The account is enum-constrained and is the load-bearing part of the
    # answer; §5.5 does not treat this float as a probability anyway.
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=17, reason="sure"),
    )

    prediction = make_categorizer().predict(TXN, CTX)

    assert prediction is not None
    assert prediction.confidence == 1.0
    assert predict_is_valid(prediction, CTX)


def test_missing_confidence_still_yields_a_prediction(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=raw_reply(json.dumps({"account": "Expenses:Food:Coffee"})),
    )

    prediction = make_categorizer().predict(TXN, CTX)

    assert prediction is not None
    assert 0.0 <= prediction.confidence <= 1.0
    assert prediction.rationale


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "",
        "[1, 2, 3]",
        '{"account": ',
        '"Expenses:Food:Coffee"',
        "null",
    ],
)
def test_malformed_content_abstains(httpx_mock, content):
    httpx_mock.add_response(url=CHAT_URL, method="POST", json=raw_reply(content))

    assert make_categorizer().predict(TXN, CTX) is None


def test_response_without_a_message_abstains(httpx_mock):
    httpx_mock.add_response(url=CHAT_URL, method="POST", json={"error": "model not found"})

    assert make_categorizer().predict(TXN, CTX) is None


def test_non_json_response_body_abstains(httpx_mock):
    httpx_mock.add_response(url=CHAT_URL, method="POST", text="<html>502</html>")

    assert make_categorizer().predict(TXN, CTX) is None


def test_error_status_abstains(httpx_mock):
    httpx_mock.add_response(url=CHAT_URL, method="POST", status_code=500, text="boom")

    assert make_categorizer().predict(TXN, CTX) is None


def test_timeout_abstains(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=CHAT_URL, method="POST")

    assert make_categorizer(timeout=0.01).predict(TXN, CTX) is None


def test_ollama_not_running_abstains(httpx_mock):
    # The §6 Phase 3 requirement: no Ollama must degrade the cascade, not
    # break a sync.
    httpx_mock.add_exception(
        httpx.ConnectError("connection refused"), url=CHAT_URL, method="POST"
    )

    assert make_categorizer().predict(TXN, CTX) is None


def test_out_of_enum_account_abstains(httpx_mock):
    # `format` should make this unreachable; the constraint lives in the
    # server, so an older build that ignored it must not be able to write an
    # invented account into the ledger.
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(
            account="Expenses:Food:Coffee:Speciality", confidence=0.99, reason="sure"
        ),
    )

    assert make_categorizer().predict(TXN, CTX) is None


def test_unknown_catch_all_answer_abstains(httpx_mock):
    httpx_mock.add_response(
        url=CHAT_URL,
        method="POST",
        json=ollama_reply(account="Expenses:Unknown", confidence=0.4, reason="no idea"),
    )
    ctx = LedgerContext(accounts=(*ACCOUNTS, "Expenses:Unknown"), examples=CTX.examples)

    assert make_categorizer().predict(TXN, ctx) is None


def test_empty_account_set_makes_no_request(httpx_mock):
    empty = LedgerContext(accounts=(), examples=CTX.examples)

    assert make_categorizer().predict(TXN, empty) is None
    assert httpx_mock.get_requests() == []


def test_construction_contacts_nothing(httpx_mock):
    LlmCategorizer(normalizer=trivial_normalizer, model="phi4:14b", think=True)

    assert httpx_mock.get_requests() == []


def test_constructs_without_an_injected_normalizer(httpx_mock):
    # The default normalizer is resolved by a deferred import, so nothing but
    # a test constructing one this way notices if that import target moves.
    categorizer = LlmCategorizer()

    assert isinstance(categorizer._normalize("SQ *COFFEE 4TH ST 8829"), str)
    assert httpx_mock.get_requests() == []


def test_base_url_and_model_come_from_the_environment(monkeypatch, httpx_mock):
    monkeypatch.setenv("BOOKKEEPER_OLLAMA_URL", "http://ollama.internal:9999/")
    monkeypatch.setenv("BOOKKEEPER_OLLAMA_MODEL", "phi4:14b")
    url = "http://ollama.internal:9999/api/chat"
    httpx_mock.add_response(
        url=url,
        method="POST",
        json=ollama_reply(account="Expenses:Food:Coffee", confidence=0.5, reason="ok"),
    )

    categorizer = make_categorizer()
    assert categorizer.chat_url == url

    prediction = categorizer.predict(TXN, CTX)
    assert prediction is not None
    assert sent_body(httpx_mock)["model"] == "phi4:14b"
