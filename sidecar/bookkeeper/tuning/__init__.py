"""Tuning rules and envelopes against real spending (PLAN.md §6 Phase 6).

Two read-only advisors that turn a fresh backfill into work a human can
act on, and neither of them acts on its own behalf:

- `rulesuggest` — which recurring merchants are worth a `rules.yaml` entry,
  what that entry would be, and what it would have done to the data already
  on disk.
- `gaps` — which expense accounts have spending but no envelope mapping,
  and what their recorded history *was* per month.

Nothing in this package writes. `rules.yaml`, `memory.json` and the ledger
are all inputs. Review-everything is the shipped default (decision 5) and
§5.5 raises autonomy only on measured evidence; a module that wrote the
rules it invented would be raising autonomy on no evidence at all.
"""

from __future__ import annotations
