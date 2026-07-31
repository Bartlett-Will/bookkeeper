#!/usr/bin/env python3
"""Replay a captured SimpleFIN response as a local server, for Phase 4 verification.

Criterion 1 needs a **sync**, and the sync must not be a real one. The
demo bridge is rate-limited to ~24 requests/day (PLAN.md §3.1), its claim
endpoint is broken server-side (§3.1 amendment), and pointing a
verification run at a live banking endpoint is how someone accidentally
syncs the real ledger. What the criterion is actually about is the loop —
that a sync lands transactions the app can then review and correct — not
about whether HTTPS works.

So this serves a previously-captured `data/raw/simplefin-*.json` at
`/accounts`, and the sidecar is pointed at it with

    BOOKKEEPER_SIMPLEFIN_ACCESS_URL=http://127.0.0.1:8899

Nothing in the sidecar is stubbed or monkeypatched: `fetch_accounts` does
its ordinary `GET {access_url}/accounts`, archives the bytes, parses,
normalizes, dedups and renders exactly as it would against the bridge.
The only thing replaced is the far end of the socket, which is the one
part of the path the exit criterion says nothing about.

Only the standard library is used (no new dependencies, per the phase rules).

Usage
-----

    python3 scripts/simplefin_replay.py --archive data/raw/simplefin-....json --port 8899
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ReplayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SimpleFINReplay/1.0"

    payload: bytes
    hits: list[str]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.endswith("/accounts"):
            self.send_error(404, "only /accounts is served")
            return
        # Recorded so a verification run can assert the sync really made a
        # fetch rather than short-circuiting on a cache somewhere.
        self.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("replay: %s\n" % (fmt % args))


class ReplayServer:
    def __init__(self, archive: Path, port: int = 8899) -> None:
        payload = archive.read_bytes()
        # Parsed once here so a corrupt archive fails at startup with a clear
        # message, rather than as a confusing ingest error later.
        json.loads(payload)
        self.hits: list[str] = []

        handler = type(
            "BoundReplayHandler",
            (ReplayHandler,),
            {"payload": payload, "hits": self.hits},
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def access_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> ReplayServer:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> ReplayServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--archive", required=True, help="a captured data/raw/simplefin-*.json")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args(argv)

    server = ReplayServer(Path(args.archive).resolve(), port=args.port).start()
    print(f"replaying {args.archive} at {server.access_url}/accounts", file=sys.stderr)
    print(f"  BOOKKEEPER_SIMPLEFIN_ACCESS_URL={server.access_url}", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
