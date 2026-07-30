"""The cascade orchestrator (PLAN.md §5.4, §6 Phase 3).

First-hit-wins over an ordered sequence of independent `Categorizer` tiers.
`build_default_cascade` is the one PLAN.md ships with: memory -> rule ->
mcc -> statistical -> llm. The statistical and LLM tiers are optional --
imported lazily and tolerantly, since they are written independently of
this module and the LLM tier additionally depends on Ollama being
installed and reachable, which is never guaranteed. A missing optional
tier degrades the cascade (fewer tiers fire, coverage drops -- a §5.5 eval
number), it never crashes it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from bookkeeper.categorize.mcc import MccCategorizer
from bookkeeper.categorize.memory import MemoryCategorizer
from bookkeeper.categorize.models import (
    CASCADE_ORDER,
    CategorizationInput,
    Categorizer,
    LedgerContext,
    Prediction,
)
from bookkeeper.categorize.rules import RuleCategorizer

logger = logging.getLogger(__name__)


class Cascade:
    """Runs each tier in turn and returns the first non-abstaining answer.

    Ordering always comes from `models.CASCADE_ORDER`, never from the order
    `categorizers` happened to be passed in -- so `Cascade([llm, memory])`
    and `Cascade([memory, llm])` behave identically. A tier whose `.tier`
    isn't in `CASCADE_ORDER` is simply never consulted, rather than being a
    construction-time error: that's what lets a caller build a
    partial/test cascade with only the tiers it cares about.
    """

    def __init__(self, categorizers: Sequence[Categorizer]) -> None:
        by_tier = {c.tier: c for c in categorizers}
        self._categorizers = tuple(by_tier[tier] for tier in CASCADE_ORDER if tier in by_tier)

    @property
    def tiers(self) -> tuple[Categorizer, ...]:
        """The individual tiers, in cascade (evaluation) order.

        The §5.5 eval harness measures each tier's coverage and precision
        independently, not just the first-hit-wins result `predict` gives
        -- this is the seam it needs for that, without reaching into a
        private attribute.
        """
        return self._categorizers

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        for categorizer in self._categorizers:
            prediction = categorizer.predict(txn, ctx)
            if prediction is not None:
                return prediction
        return None


def build_default_cascade(use_llm: bool = True) -> Cascade:
    """The full PLAN.md §5.4 cascade, tolerant of tiers that aren't ready yet.

    `use_llm=False` skips the LLM tier outright (the `categorize --no-llm`
    / `eval` CLI flags, §5.5's CI requirement to stay hermetic) without
    needing Ollama to be absent for that to work.
    """
    categorizers: list[Categorizer] = [
        MemoryCategorizer(),
        RuleCategorizer(),
        MccCategorizer(),
    ]

    try:
        from bookkeeper.categorize.statistical import StatisticalCategorizer

        categorizers.append(StatisticalCategorizer())
    except Exception:  # noqa: BLE001 - optional tier, degrade don't crash
        logger.info("statistical categorizer unavailable; continuing without it")

    if use_llm:
        try:
            from bookkeeper.categorize.llm import LlmCategorizer

            categorizers.append(LlmCategorizer())
        except Exception:  # noqa: BLE001 - optional tier, degrade don't crash
            logger.info("LLM categorizer unavailable; continuing without it")

    return Cascade(categorizers)
