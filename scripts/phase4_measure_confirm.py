#!/usr/bin/env python3
"""Measure Phase 4 exit criterion 3 through the *web* stack.

    "approving 40 transactions makes zero additional LLM calls"

`sidecar/tests/test_phase4_e2e.py` measures the sidecar half of this: what
happens once a confirmation leaves the browser. That is necessary and not
sufficient. The Accept button is in a Next.js app with its own model
provider, and an accidental LLM call is at least as likely to hide in that
process as in Python -- a title generator, a suggestion hook, a stray
`streamText` on a route the click happens to touch. So the batch is driven
through `POST /api/bookkeeper/review/confirm` on the Next.js server, which
is the exact path `ReviewCard`'s buttons take, and the count is read off
`scripts/ollama_call_counter.py` sitting between Next.js and Ollama.

The measurement is only worth the paper it is printed on if the proxy is
actually in the web process's path, because "the app made no model calls"
and "the instrument was never connected" produce identical numbers. So
`--positive-control` sends a real chat turn first and requires the count to
move. Run it. A zero reported without it is not evidence.

Only the standard library is used (no new dependencies, per the phase rules).

Usage
-----

    # 1. a fixture ledger, so nothing here can touch the real one
    python3 scripts/phase4_measure_confirm.py --build-fixture /tmp/p4

    # 2. start the counter, a sidecar rooted at the fixture, and the web app
    #    (see docs/phase4-verification.md for the exact invocations)

    # 3. measure
    python3 scripts/phase4_measure_confirm.py \
        --web http://127.0.0.1:3100 \
        --counter http://127.0.0.1:11437 \
        --positive-control

Exits non-zero if any LLM call is observed during the approval, or if the
positive control fails to observe one.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import importlib.util
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A message that must reach the model: it is not one of the commands
#: `lib/ai/pre-route.ts` matches, so nothing intercepts it before inference.
#: Pre-routing exists precisely to skip the model (PLAN.md §5.3 rule 4), so a
#: positive control that said "review" would prove the opposite of what it
#: needs to.
CONTROL_MESSAGE = "hello there, introduce yourself in one short sentence"


def load_harness() -> Any:
    """Import the pytest module that owns the forty transactions.

    The fixture tree and the labels are defined in
    `sidecar/tests/test_phase4_e2e.py` and imported here rather than copied,
    so the batch this script approves is the same batch the hermetic tests
    approve. Two hand-maintained lists of forty transactions would drift,
    and the first symptom of the drift would be a confirmation that matches
    nothing -- which reports as "confirmed 0 of 40", i.e. as an empty queue
    rather than as a bug.
    """
    module_path = REPO_ROOT / "sidecar" / "tests" / "test_phase4_e2e.py"
    sys.path.insert(0, str(REPO_ROOT / "sidecar"))
    spec = importlib.util.spec_from_file_location("phase4_harness", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase4_harness"] = module
    spec.loader.exec_module(module)
    return module


class WebClient:
    """The Next.js app, driven the way a browser drives it.

    A cookie jar is not optional here: every `/api/bookkeeper/*` route
    307-redirects to `/api/auth/local`, which creates the single local user
    of PLAN.md §5.6 and sets a session cookie. Without the jar every call
    reads as a redirect and nothing is ever measured.
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: int = 120,
    ) -> tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url=self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def authenticate(self) -> None:
        status, _ = self.request("GET", "/api/auth/local")
        if status != 200:
            raise SystemExit(f"auth failed: HTTP {status} from /api/auth/local")
        if not len(self.jar):
            raise SystemExit("auth returned 200 but set no session cookie")

    def json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, raw = self.request(method, path, body)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise SystemExit(f"{method} {path} -> HTTP {status}, non-JSON: {raw[:400]!r}") from None
        if status != 200:
            raise SystemExit(f"{method} {path} -> HTTP {status}: {json.dumps(payload)[:400]}")
        return payload


class Counter:
    """The counting proxy's control plane."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _call(self, path: str, method: str = "GET") -> dict[str, Any]:
        request = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return json.loads(response.read())
        except OSError as exc:
            raise SystemExit(f"counting proxy at {self.base} unreachable: {exc}") from None

    def stats(self) -> dict[str, Any]:
        return self._call("/__counter/stats")

    def reset(self) -> None:
        self._call("/__counter/reset", method="POST")

    @property
    def llm_calls(self) -> int:
        return int(self.stats()["llm_calls"])


def positive_control(web: WebClient, counter: Counter) -> int:
    """Prove the instrument is in the web process's path before trusting a zero.

    Returns the number of inference calls the control turn produced, which
    must be at least one. The turn's *answer* is irrelevant and is not
    checked -- an 8B model saying something unhelpful still proves the
    request reached a model through the proxy, which is the only claim being
    made here.
    """
    before = counter.llm_calls
    chat_id = str(uuid.uuid4())
    status, raw = web.request(
        "POST",
        "/api/chat",
        {
            "id": chat_id,
            "message": {
                "id": str(uuid.uuid4()),
                "parts": [{"text": CONTROL_MESSAGE, "type": "text"}],
                "role": "user",
            },
            "selectedChatModel": "chat-model",
            "selectedVisibilityType": "private",
        },
        timeout=180,
    )
    observed = counter.llm_calls - before
    if observed < 1:
        raise SystemExit(
            "POSITIVE CONTROL FAILED: a chat turn produced no inference call at the "
            f"proxy (HTTP {status}, {raw[:300]!r}). The web app is not talking to "
            "Ollama through the counter, so a zero below would measure nothing."
        )
    return observed


def measure(web: WebClient, counter: Counter, harness: Any) -> dict[str, Any]:
    """Approve forty transactions through Next.js and count model calls.

    The queue read is deliberately inside the measured window. Rendering
    forty review cards is part of "approving 40 transactions" as a user
    experiences it, and §5.3 rule 3 says that path must not run a model
    either. So is the reload afterwards, which is what the UI does once a
    batch lands.
    """
    truth = {description: account for description, _amt, _mcc, account in harness.FORTY}

    counter.reset()

    queue = web.json("GET", "/api/bookkeeper/review-queue?limit=100")
    entries = queue.get("queue", {}).get("entries", [])
    if len(entries) != 40:
        raise SystemExit(
            f"expected 40 transactions awaiting review, found {len(entries)}. "
            "Is the sidecar rooted at the fixture tree built by --build-fixture?"
        )

    confirmations = [
        {
            "account": truth[entry["description"]],
            "asset_account": entry["asset_account"],
            "simplefin_id": entry["simplefin_id"],
        }
        for entry in entries
    ]

    confirmed = web.json(
        "POST", "/api/bookkeeper/review/confirm", {"confirmations": confirmations}
    )
    reloaded = web.json("GET", "/api/bookkeeper/review-queue?limit=100")

    stats = counter.stats()
    return {
        "approved": len(confirmations),
        "confirmed": confirmed.get("confirmed"),
        "learned": confirmed.get("learned"),
        "ok": confirmed.get("ok"),
        "commit": (confirmed.get("commit") or {}).get("sha"),
        "queue_after": reloaded.get("queue", {}).get("total"),
        "llm_calls": stats["llm_calls"],
        "metadata_calls": stats["metadata_calls"],
        "other_calls": stats["other_calls"],
        "total_requests_at_proxy": stats["total_requests"],
        "by_path": stats["by_path"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--build-fixture", metavar="DIR", help="build a fixture tree and exit")
    parser.add_argument("--web", default="http://127.0.0.1:3100", help="Next.js base URL")
    parser.add_argument(
        "--counter", default="http://127.0.0.1:11437", help="counting proxy base URL"
    )
    parser.add_argument(
        "--positive-control",
        action="store_true",
        help="send a real chat turn first and require the counter to move",
    )
    args = parser.parse_args(argv)

    harness = load_harness()

    if args.build_fixture:
        root = Path(args.build_fixture).resolve()
        root.mkdir(parents=True, exist_ok=True)
        harness.build_fixture_tree(root)
        harness.git_init(root)
        print(json.dumps({"fixture": str(root), "transactions": len(harness.FORTY)}, indent=2))
        return 0

    counter = Counter(args.counter)
    web = WebClient(args.web)
    web.authenticate()

    report: dict[str, Any] = {}
    if args.positive_control:
        report["positive_control_llm_calls"] = positive_control(web, counter)

    report.update(measure(web, counter, harness))
    print(json.dumps(report, indent=2))

    if report["llm_calls"] != 0:
        print(
            f"\nFAIL: {report['llm_calls']} LLM call(s) during a 40-transaction approval.",
            file=sys.stderr,
        )
        return 1
    print("\nPASS: zero LLM calls during a 40-transaction approval.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
