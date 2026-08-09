#!/usr/bin/env python3
"""Turn `model_bakeoff.sh`'s raw logs into the table PLAN.md §6 Phase 5 asks for.

Reads only what the harnesses actually printed. Nothing here recomputes a
figure or fills a gap -- a model whose logs are missing shows as absent rather
than as a blank that could be misread as a zero, which matters when the whole
point of the table is to justify a choice between them.

    usage: scripts/bakeoff_table.py [logdir]
"""

from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

LATENCY = re.compile(r"\s(\d+)ms\s")
STANDARD = re.compile(r"(\d+)/(\d+) correct \(([\d.]+)%\)")
HARD = re.compile(r"(\d+)/(\d+) defensible on the hard set \(([\d.]+)%\)")
#: `bookkeeper eval`'s per-tier table line for the LLM tier.
LLM_TIER = re.compile(r"^llm\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%", re.MULTILINE)
CASCADE = re.compile(r"^cascade[^\n]*?([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%", re.MULTILINE)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def latencies(text: str) -> list[int]:
    return [int(m) for m in LATENCY.findall(text)]


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".omc/bakeoff")
    if not out.is_dir():
        print(f"no such log directory: {out}", file=sys.stderr)
        return 2

    slugs = sorted({p.name.split(".")[0] for p in out.glob("*.log")})
    if not slugs:
        print(f"no logs in {out} -- run scripts/model_bakeoff.sh first", file=sys.stderr)
        return 2

    rows = []
    for slug in slugs:
        std = read(out / f"{slug}.selection-standard.log")
        hard = read(out / f"{slug}.selection-hard.log")
        ev = read(out / f"{slug}.eval.log")

        s = STANDARD.search(std)
        h = HARD.search(hard)
        ms = latencies(std) + latencies(hard)

        rows.append(
            {
                "model": slug.replace("-", ":", 1),
                "standard": f"{s.group(1)}/{s.group(2)} ({s.group(3)}%)" if s else "—",
                "hard": f"{h.group(1)}/{h.group(2)} ({h.group(3)}%)" if h else "—",
                "median": f"{statistics.median(ms):.0f}ms" if ms else "—",
                "p_max": f"{max(ms)}ms" if ms else "—",
                "llm_tier": (m.group(2) + "%" if (m := LLM_TIER.search(ev)) else "—"),
                "cascade": (c.group(2) + "%" if (c := CASCADE.search(ev)) else "—"),
            }
        )

    headers = [
        ("model", "model"),
        ("standard", "tool: standard"),
        ("hard", "tool: hard"),
        ("median", "median latency"),
        ("p_max", "slowest"),
        ("llm_tier", "LLM tier precision"),
        ("cascade", "cascade precision"),
    ]
    widths = {
        k: max(len(label), *(len(str(r[k])) for r in rows)) for k, label in headers
    }

    print("| " + " | ".join(label.ljust(widths[k]) for k, label in headers) + " |")
    print("|" + "|".join("-" * (widths[k] + 2) for k, _ in headers) + "|")
    for r in rows:
        print("| " + " | ".join(str(r[k]).ljust(widths[k]) for k, _ in headers) + " |")

    missing = [r["model"] for r in rows if "—" in r.values()]
    if missing:
        print(
            "\nEm dashes are missing runs, not zeros. Re-run "
            "scripts/model_bakeoff.sh for: " + ", ".join(sorted(set(missing)))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
