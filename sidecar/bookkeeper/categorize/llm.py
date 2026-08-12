"""Tier 4: single-shot, schema-constrained classification by a local model.

This is the tier PLAN.md §6 Phase 3 puts "behind a swappable interface": it is
just another `Categorizer`, constructing one never contacts Ollama, and every
failure mode -- Ollama not installed, not running, slow, or answering
nonsense -- is an abstention, so the cascade degrades to tiers 1-3 instead of
raising into the middle of a sync.

Four decisions carry the reliability of this tier, all from the plan:

**The native `/api/chat` endpoint, not the OpenAI-compatible `/v1` one.** The
§5.3 amendment measured `qwen3:8b` on this machine at 32s with thinking on
versus 2s with `think: false`, on the same prompt and the same correct answer.
`think` is an Ollama-native request parameter that `/v1/chat/completions` does
not accept, so a `baseURL` swap cannot turn thinking off. Talking to the
native endpoint is what makes the choice available at all.

**`think` is per-request, not baked in.** Off by default, because 32s is not
an interactive latency. Available on, because §5.3 explicitly reserves it for
batch categorization, where latency is irrelevant and reasoning may earn its
keep on a genuinely novel merchant string.

**The account is an `enum` in a JSON Schema built from the ledger.** Ollama's
`format` parameter constrains decoding, so an account that is not open in the
ledger is not merely unlikely, it is unrepresentable (§5.4). The enum is built
from `ctx.accounts` at call time and never from a list written down here --
a hardcoded set would drift from the ledger and quietly become a
hallucination surface again.

**Retrieve, then ask.** The prompt carries the nearest previously confirmed
categorizations as few-shot grounding, because §5.4's finding is that small
models are much better at "which of these known patterns does this resemble"
than at cold-start classification, and because it grounds the answer in the
user's own account naming.

And one deliberate omission: no tools, no chaining, no follow-up turn. §3.3
measures small models compounding errors across steps and degrading over
turns; this is one request and one answer, which is the shape they are
actually good at.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from bookkeeper.categorize.models import (
    UNKNOWN_ACCOUNTS,
    CategorizationInput,
    LabeledExample,
    LedgerContext,
    Prediction,
    Tier,
)

BASE_URL_ENV = "BOOKKEEPER_OLLAMA_URL"
MODEL_ENV = "BOOKKEEPER_OLLAMA_MODEL"

_DEFAULT_BASE_URL = "http://localhost:11434"

#: §5.3's model choice: ~13% tool-calling failure at 8B versus ~76.6% for
#: Llama-3.1-8B, and it fits the §3.2 memory budget alongside the rest of the
#: stack. Overridable so the Phase 5 bake-off can point this at a 14B.
_DEFAULT_MODEL = "qwen3:8b"

#: Generous for the 2s interactive case, and short enough that a wedged or
#: swapping Ollama abstains rather than stalling a sync. Callers enabling
#: `think` should raise it: the measured thinking run took 32s.
_DEFAULT_TIMEOUT = 30.0

#: §5.4's "~20 nearest". Enough for the model to see the shape of the user's
#: conventions; few enough that the prompt stays short on an 8B's context.
_DEFAULT_MAX_EXAMPLES = 20

#: Used when the model omits or garbles its self-reported confidence. The
#: account is the load-bearing part of the answer and is enum-constrained;
#: throwing away a valid account because a float was missing would trade a
#: real prediction for nothing. §5.5 does not trust this number as a
#: probability in any case.
_UNSTATED_CONFIDENCE = 0.5

_TRIGRAM_SIZE = 3

_SYSTEM_PROMPT = (
    "You categorize bank transactions into a ledger. You are given one "
    "transaction and a closed list of accounts. Choose the single account "
    "that best fits, and answer only with the requested JSON object.\n"
    "- The account must be one of the listed accounts, copied exactly.\n"
    "- The examples are prior categorizations recorded in this ledger. If "
    "the transaction resembles one, follow it.\n"
    "- Bank descriptions are abbreviated and carry store numbers, terminal "
    "ids and processor prefixes. Categorize by the merchant, not the noise.\n"
    "- Report your own confidence between 0 and 1."
)


def _default_normalizer() -> Callable[[str], str]:
    """Resolve the shared description normalizer at construction time.

    Deferred rather than imported at module scope so that a caller supplying
    its own normalizer -- notably a test isolating this tier's wire format
    from normalization behaviour -- never pulls in the shared module at all.
    """
    from bookkeeper.categorize.normalize import normalize_description

    return normalize_description


def _trigrams(text: str) -> frozenset[str]:
    padded = f" {' '.join(text.lower().split())} "
    return frozenset(
        padded[i : i + _TRIGRAM_SIZE]
        for i in range(len(padded) - _TRIGRAM_SIZE + 1)
    )


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Character-trigram Jaccard.

    §5.4 offers "trigram or embedding similarity". Trigrams win here: an
    embedding model would be a second model resident alongside the 8B, which
    §3.2 rules out, and set overlap on short merchant strings is exactly the
    signal that survives the store numbers and processor prefixes of §3.1.
    """
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _account_enum(ctx: LedgerContext) -> list[str]:
    """The closed label set, as the schema `enum` will carry it.

    `LedgerContext.accounts` already excludes the `Unknown` catch-alls; they
    are filtered again here because letting one into the enum would make
    "successfully categorized as Unknown" a decodable answer, which is an
    abstention dressed up as coverage.
    """
    seen: dict[str, None] = {}
    for account in ctx.accounts:
        if account not in UNKNOWN_ACCOUNTS:
            seen.setdefault(account, None)
    return list(seen)


class LlmCategorizer:
    """Tier 4 of the §5.4 cascade: one constrained request to a local model.

    Constructing this does not contact Ollama and does not check that it is
    installed. That is what lets the cascade be assembled identically whether
    or not a model is available: an absent Ollama shows up as an abstention on
    the first `predict`, not as an error at wiring time.
    """

    tier = Tier.LLM

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        think: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
        max_examples: int = _DEFAULT_MAX_EXAMPLES,
        normalizer: Callable[[str], str] | None = None,
    ) -> None:
        self.model = model or os.environ.get(MODEL_ENV) or _DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV) or _DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.think = think
        self.timeout = timeout
        self.max_examples = max_examples
        self._normalize = normalizer if normalizer is not None else _default_normalizer()

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/api/chat"

    def nearest_examples(
        self, txn: CategorizationInput, ctx: LedgerContext
    ) -> list[LabeledExample]:
        """The most similar confirmed examples, nearest **last**.

        Ordered so the closest match sits immediately before the question
        rather than at the top of a twenty-item list: an 8B attends most
        reliably to the end of its prompt. Examples with no trigram overlap
        at all are dropped -- they are not grounding, they are prompt length,
        and the account list already supplies the label vocabulary.
        """
        query = _trigrams(self._normalize(txn.description))
        scored = [
            (_similarity(query, _trigrams(example.normalized_description)), index, example)
            for index, example in enumerate(ctx.examples)
            if example.account in ctx.accounts
        ]
        ranked = sorted(
            (entry for entry in scored if entry[0] > 0.0),
            key=lambda entry: (-entry[0], entry[1]),
        )[: self.max_examples]
        return [example for _, _, example in reversed(ranked)]

    def build_request(
        self, txn: CategorizationInput, ctx: LedgerContext, accounts: Sequence[str]
    ) -> dict[str, Any]:
        """The exact `/api/chat` body. Public so the eval harness can log it."""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._user_message(txn, ctx, accounts),
                },
            ],
            "stream": False,
            # Native parameter, sent on every request rather than configured
            # once on the client: the OpenAI-compatible endpoint rejects it
            # outright, and §5.3 wants interactive and batch callers to differ
            # on this one value alone.
            "think": self.think,
            "format": {
                "type": "object",
                "properties": {
                    "account": {"type": "string", "enum": list(accounts)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["account", "confidence", "reason"],
                "additionalProperties": False,
            },
            # Classification, not composition: sampling here only adds a way
            # for the same transaction to be categorized two ways on two runs.
            "options": {"temperature": 0},
        }

    def _user_message(
        self, txn: CategorizationInput, ctx: LedgerContext, accounts: Sequence[str]
    ) -> str:
        blocks = ["Accounts:"]
        blocks.extend(f"- {account}" for account in accounts)

        examples = self.nearest_examples(txn, ctx)
        if examples:
            blocks.append("")
            blocks.append("Already categorized in this ledger (most similar last):")
            blocks.extend(
                f"- {(example.description or example.normalized_description)!r}"
                f" -> {example.account}"
                for example in examples
            )

        blocks.append("")
        blocks.append("Transaction:")
        blocks.append(f"- description: {txn.description}")
        blocks.append(f"- amount: {txn.amount} {txn.currency}")
        blocks.append(f"- date: {txn.posted_date.isoformat()}")
        for label, value in (("payee", txn.payee), ("memo", txn.memo), ("mcc", txn.mcc)):
            if value:
                blocks.append(f"- {label}: {value}")
        return "\n".join(blocks)

    def predict(
        self, txn: CategorizationInput, ctx: LedgerContext
    ) -> Prediction | None:
        accounts = _account_enum(ctx)
        if not accounts:
            # An empty enum is not a valid JSON Schema constraint and there is
            # no answer that could be valid anyway. Abstain without spending a
            # request on it.
            return None

        payload = self._decode(self._post(self.build_request(txn, ctx, accounts)))
        if payload is None:
            return None

        account = payload.get("account")
        if not isinstance(account, str) or account not in accounts:
            # Defensive: `format` should have made this unreachable, but the
            # constraint lives in the server, and an older or differently
            # configured Ollama that ignored it must not be able to write an
            # invented account into the ledger.
            return None

        return Prediction(
            account=account,
            confidence=_read_confidence(payload.get("confidence")),
            tier=Tier.LLM,
            rationale=_read_reason(payload.get("reason"), self.model),
        )

    def _post(self, body: dict[str, Any]) -> httpx.Response | None:
        """One request, no retries. Any transport failure is an abstention.

        Retrying would be the first step of the multi-step behaviour §3.3
        warns about, and a sync that stalls on a wedged Ollama is worse than
        one that routes the transaction to the review queue.
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.chat_url, json=body)
        except httpx.HTTPError:
            return None
        return response if response.status_code == 200 else None

    @staticmethod
    def _decode(response: httpx.Response | None) -> dict[str, Any] | None:
        """The model's JSON object, or `None` if anything about it is wrong."""
        if response is None:
            return None
        try:
            content = response.json()["message"]["content"]
            payload = json.loads(content)
        except (ValueError, KeyError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None


def _read_confidence(value: Any) -> float:
    """The model's self-reported confidence, clamped into [0, 1].

    Captured because §5.5 needs something to bucket by, and explicitly *not*
    trusted as a probability: it is a self-assessment, and the auto-apply
    threshold comes from measured precision per bucket rather than from any
    number the model reports about itself. Clamped rather than rejected so a
    model that answers `1.5` still yields a usable prediction.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _UNSTATED_CONFIDENCE
    number = float(value)
    if not math.isfinite(number):
        return _UNSTATED_CONFIDENCE
    return min(1.0, max(0.0, number))


def _read_reason(value: Any, model: str) -> str:
    if isinstance(value, str) and value.strip():
        return f"{model}: {value.strip()}"
    return f"{model} (single-shot, schema-constrained)"
