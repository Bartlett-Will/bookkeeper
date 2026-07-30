"""Description normalization: the key tier 1 (exact memory) indexes on.

Bank feeds mangle the same merchant a dozen different ways -- a payment-rail
prefix here, a trailing store number or transaction id there. Two postings
from the same coffee shop must normalize to the same key or tier 1 never
fires twice for the same merchant; two genuinely different merchants must
stay distinct, or the memory silently misfiles one under the other's
history. This module only handles the "same merchant, different noise"
half -- distinguishing genuinely different merchants is the caller's job
via whatever key comes out of here.
"""

from __future__ import annotations

import re

# Bank/processor prefixes and payment-rail noise that precedes the actual
# merchant name, matched at the start of the case-folded string. Order
# doesn't matter for correctness (each is anchored and tried until none
# match, see the loop below), but is grouped by rail for readability.
# `[\s:*-]*` after each mops up whatever separator punctuation the bank
# put between the rail marker and the merchant name ("ACH DEBIT - X",
# "POS DEBIT: X", "SQ *X").
_PREFIX_RES = [
    re.compile(r"^pos\s+debit\b[\s:*-]*"),
    re.compile(r"^ach\s+debit\b[\s:*-]*"),
    re.compile(r"^ach\s+credit\b[\s:*-]*"),
    re.compile(r"^checkcard\b[\s:*-]*"),
    re.compile(r"^recurring\s+payment\b[\s:*-]*"),
    # "PURCHASE AUTHORIZED ON 12/01 <merchant>" -- the date itself carries
    # no merchant identity, only when the charge cleared.
    re.compile(r"^purchase\s+authorized\s+on\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b[\s:*-]*"),
    re.compile(r"^sq\s*\*"),
    re.compile(r"^tst\s*\*"),
]

# Trailing store numbers and transaction ids: a run of 3+ digits (with
# optional "#" or separator punctuation) at the very end of the string.
# 3+ specifically -- not 1+ -- so short digit strings that are part of a
# merchant's actual name ("7-ELEVEN", "76 GAS") survive; PLAN.md §5.4's own
# examples only ever show store/transaction numbers as 4+ digit suffixes.
_TRAILING_DIGITS_RE = re.compile(r"[\s#-]*\d{3,}\s*$")

# Final cleanup: collapse whatever punctuation is left ("&", ".", "/") into
# single spaces so two descriptions that differ only in punctuation style
# still produce the same key.
_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def normalize_description(text: str) -> str:
    """Collapse bank-mangled description noise to a stable merchant key.

    `SQ *COFFEE 4TH ST 8829` and `SQ *COFFEE 4TH ST 8831` -> `coffee 4th st`.
    `PG&E WEB ONLINE` and `ACH DEBIT - PG&E WEB ONLINE` -> `pg e web online`.

    Case-folds, strips known payment-rail prefixes (repeatedly, since some
    descriptions chain more than one -- "PURCHASE AUTHORIZED ON 12/01 SQ
    *COFFEE"), strips a trailing digit run, then collapses remaining
    punctuation to spaces. Deliberately conservative: it does not touch
    digits or numbers embedded in the middle of a description, since that
    is exactly where genuine merchant identity lives ("4TH ST", "76 GAS").
    """
    key = text.casefold().strip()

    changed = True
    while changed:
        changed = False
        for prefix_re in _PREFIX_RES:
            stripped = prefix_re.sub("", key)
            if stripped != key:
                key = stripped.lstrip()
                changed = True

    while True:
        stripped = _TRAILING_DIGITS_RE.sub("", key)
        if stripped == key:
            break
        key = stripped

    return _PUNCT_RE.sub(" ", key).strip()
