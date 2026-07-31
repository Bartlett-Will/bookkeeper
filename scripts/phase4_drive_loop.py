#!/usr/bin/env python3
"""Drive Phase 4 exit criterion 1 end to end: sync -> review -> correct.

    a full natural-language sync -> review -> correct loop with no
    hand-editing of ledger files

Every step here goes through the running Next.js app, in the order and by
the route a person would: the sync and the review are *sentences* sent to
`POST /api/chat`, and the corrections are the direct
`POST /api/bookkeeper/review/confirm` call that `ReviewCard`'s buttons make
(PLAN.md §5.3 rule 2). Nothing touches the sidecar directly and nothing
touches a `.beancount` file.

"No hand-editing" is then evidenced structurally rather than asserted. The
ledger tree is a git repo committed clean before the loop starts; this
script never writes into it; so anything that differs afterwards was
written by the app. The report prints `git status` and `git log` so the
claim can be checked rather than believed.

The sync is served by `scripts/simplefin_replay.py` replaying a captured
archive, so no live banking endpoint is contacted and the ~24 requests/day
rate limit (PLAN.md §3.1) is not spent. See that script for why that
substitution is the honest one.

Only the standard library is used (no new dependencies, per the phase rules).

Usage
-----

    python3 scripts/phase4_drive_loop.py \
        --web http://127.0.0.1:3100 \
        --counter http://127.0.0.1:11437 \
        --root /path/to/fixture/tree
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase4_measure_confirm import Counter, WebClient  # noqa: E402

SYNC_MESSAGE = "sync my accounts"
REVIEW_MESSAGE = "show me the review queue"

#: Where a transaction goes when the cascade offered nothing. The demo feed's
#: only unsuggested description is its paycheck, and a human looking at
#: `Pay day! +2000.00` would file it exactly here. Applied only when
#: `suggested_account` is null, so it never overrides a real prediction.
FALLBACK_INCOME_ACCOUNT = "Income:Salary"


def stream_events(raw: bytes) -> list[dict[str, Any]]:
    """The `data:` frames of an AI SDK UI message stream, as objects."""
    events = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except ValueError:
            continue
    return events


def say(web: WebClient, counter: Counter, chat_id: str, text: str) -> dict[str, Any]:
    """Send one chat message and report what the turn did.

    The LLM-call delta is captured per turn rather than for the run as a
    whole, because the interesting number is different for each leg: a chat
    turn is *expected* to invoke the model, and the confirm is expected not
    to. Reporting one total would hide both.
    """
    before = counter.llm_calls
    status, raw = web.request(
        "POST",
        "/api/chat",
        {
            "id": chat_id,
            "message": {
                "id": str(uuid.uuid4()),
                "parts": [{"text": text, "type": "text"}],
                "role": "user",
            },
            "selectedChatModel": "chat-model",
            "selectedVisibilityType": "private",
        },
        timeout=300,
    )
    if status != 200:
        raise SystemExit(f"chat turn {text!r} failed: HTTP {status}: {raw[:300]!r}")

    events = stream_events(raw)
    tools = [e.get("toolName") for e in events if e.get("type") == "tool-input-available"]
    outputs = [e.get("output") for e in events if e.get("type") == "tool-output-available"]
    text_out = "".join(e.get("delta", "") for e in events if e.get("type") == "text-delta")

    return {
        "message": text,
        "tools_invoked": tools,
        "outputs": outputs,
        "assistant_text": text_out,
        "llm_calls": counter.llm_calls - before,
    }


def await_sync(web: WebClient, counter: Counter, job_id: str) -> dict[str, Any]:
    """Poll the job the way the UI polls it (PLAN.md §5.3 rule 3)."""
    before = counter.llm_calls
    deadline = time.time() + 300
    snapshot: dict[str, Any] = {}
    while time.time() < deadline:
        snapshot = web.json("GET", f"/api/bookkeeper/sync/status/{job_id}")
        if snapshot.get("state") in {"succeeded", "failed", "done", "error"}:
            break
        time.sleep(1.0)
    else:
        raise SystemExit(f"sync job {job_id} did not finish within 300s")

    return {
        "state": snapshot.get("state"),
        "result": snapshot.get("result"),
        "llm_calls_while_polling": counter.llm_calls - before,
    }


def git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def bean_check(root: Path) -> tuple[bool, str]:
    """`bean-check`, including the generated balance assertions.

    Run as the tool a user would reach for rather than through the loader
    API, because a passing exit criterion should mean the ledger is valid to
    beancount itself, not merely to our wrapper.
    """
    out = subprocess.run(
        [sys.executable, "-m", "beancount.scripts.check", str(root / "ledger" / "main.beancount")],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--web", default="http://127.0.0.1:3100")
    parser.add_argument("--counter", default="http://127.0.0.1:11437")
    parser.add_argument("--root", required=True, help="the BOOKKEEPER_ROOT the sidecar serves")
    parser.add_argument("--limit", type=int, default=500, help="review queue page size")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    counter = Counter(args.counter)
    web = WebClient(args.web)
    web.authenticate()
    counter.reset()

    report: dict[str, Any] = {"root": str(root)}
    report["git_status_before"] = git(root, "status", "--porcelain")
    report["git_log_before"] = git(root, "log", "--format=%h %s")

    chat_id = str(uuid.uuid4())

    # -- sync, as a sentence -------------------------------------------------
    sync_turn = say(web, counter, chat_id, SYNC_MESSAGE)
    if "sync_accounts" not in sync_turn["tools_invoked"]:
        raise SystemExit(f"{SYNC_MESSAGE!r} did not reach sync_accounts: {sync_turn}")
    job_id = None
    for output in sync_turn["outputs"]:
        if isinstance(output, dict):
            job_id = (output.get("data") or {}).get("job_id") or output.get("job_id")
    if not job_id:
        raise SystemExit(f"no job id in sync tool output: {sync_turn['outputs']}")
    report["sync_turn"] = {
        "message": sync_turn["message"],
        "tools_invoked": sync_turn["tools_invoked"],
        "assistant_text": sync_turn["assistant_text"],
        "llm_calls": sync_turn["llm_calls"],
        "job_id": job_id,
    }
    report["sync_job"] = await_sync(web, counter, job_id)

    # -- review, as a sentence ----------------------------------------------
    review_turn = say(web, counter, chat_id, REVIEW_MESSAGE)
    if "get_review_queue" not in review_turn["tools_invoked"]:
        raise SystemExit(f"{REVIEW_MESSAGE!r} did not reach get_review_queue: {review_turn}")
    report["review_turn"] = {
        "message": review_turn["message"],
        "tools_invoked": review_turn["tools_invoked"],
        "assistant_text": review_turn["assistant_text"],
        "llm_calls": review_turn["llm_calls"],
    }

    # -- correct, as clicks --------------------------------------------------
    queue = web.json("GET", f"/api/bookkeeper/review-queue?limit={args.limit}")
    entries = queue.get("queue", {}).get("entries", [])
    if not entries:
        raise SystemExit("the review queue is empty; the sync leg produced nothing to correct")

    confirmations = [
        {
            "account": entry.get("suggested_account") or FALLBACK_INCOME_ACCOUNT,
            "asset_account": entry["asset_account"],
            "simplefin_id": entry["simplefin_id"],
        }
        for entry in entries
    ]

    before_confirm = counter.llm_calls
    confirmed = web.json(
        "POST", "/api/bookkeeper/review/confirm", {"confirmations": confirmations}
    )
    report["confirm"] = {
        "submitted": len(confirmations),
        "confirmed": confirmed.get("confirmed"),
        "learned": confirmed.get("learned"),
        "ok": confirmed.get("ok"),
        "commit": (confirmed.get("commit") or {}).get("sha"),
        "llm_calls": counter.llm_calls - before_confirm,
    }

    # -- the loop's product --------------------------------------------------
    report["queue_after"] = web.json(
        "GET", "/api/bookkeeper/review-queue?limit=1"
    ).get("queue", {}).get("total")
    report["verify"] = web.json("GET", "/api/bookkeeper/verify")

    ok, output = bean_check(root)
    report["bean_check"] = {"ok": ok, "output": output[:2000]}
    report["git_status_after"] = git(root, "status", "--porcelain")
    report["git_log_after"] = git(root, "log", "--format=%h %s")
    report["counter_totals"] = {
        k: v for k, v in counter.stats().items() if k not in {"calls", "since"}
    }

    print(json.dumps(report, indent=2))

    failures = []
    if report["confirm"]["llm_calls"] != 0:
        failures.append(f"{report['confirm']['llm_calls']} LLM call(s) during confirm")
    if not ok:
        failures.append("bean-check failed")
    if report["queue_after"] != 0:
        failures.append(f"{report['queue_after']} transactions still awaiting review")
    if failures:
        print("\nFAIL: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("\nPASS: sync -> review -> correct completed through the app.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
