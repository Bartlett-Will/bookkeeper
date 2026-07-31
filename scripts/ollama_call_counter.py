#!/usr/bin/env python3
"""A counting reverse proxy for Ollama, for measuring Phase 4 exit criterion 3.

    "approving 40 transactions makes zero additional LLM calls"

is a claim about a *negative*. It cannot be argued from the code -- an
argument only covers the call paths you thought of -- so it has to be
measured on the wire. This process sits between the app and Ollama and
counts every request that arrives:

    app  --OLLAMA_BASE_URL/BOOKKEEPER_OLLAMA_URL-->  this  -->  ollama:11434

Observing the socket rather than instrumenting our own provider seam is the
point. An instrumented seam can only report calls that went through the seam;
this reports calls that went through the *network*, which is the failure mode
worth testing for -- a stray call from a path nobody remembered (the title
model, a suggestion generator, an embedding lookup in a library).

Requests are counted **on arrival, before forwarding**, so a call still
counts when Ollama is not running. An attempted LLM call is an LLM call; a
proxy that only counted successes would report zero for a batch that tried
forty times and failed forty times.

Only the standard library is used (no new dependencies, per the phase rules).

Usage
-----

    python3 scripts/ollama_call_counter.py --port 11435 &
    OLLAMA_BASE_URL=http://127.0.0.1:11435/api pnpm dev
    BOOKKEEPER_OLLAMA_URL=http://127.0.0.1:11435 uv run bookkeeper ...

    curl -s localhost:11435/__counter/stats     # read counts
    curl -sX POST localhost:11435/__counter/reset

The control endpoints live under `/__counter/`, a prefix Ollama's own API
does not use, and are never proxied.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Paths that mean "a model was asked to do something". Matched as suffixes
#: of the request path so that a base URL carrying a prefix still classifies.
#: Embeddings are included deliberately: they are model inference and they
#: cost time, even though they are not chat completions. If a confirm path
#: ever starts embedding descriptions, that must show up as a nonzero count
#: rather than slipping past a chat-only filter.
INFERENCE_PATHS = (
    "/api/chat",
    "/api/generate",
    "/api/embed",
    "/api/embeddings",
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/embeddings",
)

#: Paths that talk to the Ollama server without running a model: listing
#: installed models, health, model metadata. Counted and reported, but
#: separately -- the exit criterion is about LLM calls, and a UI that checks
#: whether Ollama is alive has not invoked a model.
METADATA_PATHS = (
    "/api/tags",
    "/api/ps",
    "/api/show",
    "/api/version",
    "/v1/models",
)

CONTROL_PREFIX = "/__counter/"

#: Hop-by-hop headers, which are meaningful only to a single connection and
#: must not be relayed (RFC 9110 7.6.1). `Transfer-Encoding` and
#: `Content-Length` are re-decided per response by `_relay_body`.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


class CallLog:
    """Thread-safe record of every request that reached the proxy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[dict[str, Any]] = []
        self._started = time.time()

    def record(self, method: str, path: str, body: bytes) -> dict[str, Any]:
        entry = {
            "at": time.time(),
            "method": method,
            "path": path,
            "kind": classify(path),
            "model": _model_of(body),
            "bytes": len(body),
        }
        with self._lock:
            self._calls.append(entry)
        return entry

    def stats(self) -> dict[str, Any]:
        with self._lock:
            calls = list(self._calls)
        by_kind: dict[str, int] = {}
        by_path: dict[str, int] = {}
        for call in calls:
            by_kind[call["kind"]] = by_kind.get(call["kind"], 0) + 1
            by_path[call["path"]] = by_path.get(call["path"], 0) + 1
        return {
            "llm_calls": by_kind.get("inference", 0),
            "metadata_calls": by_kind.get("metadata", 0),
            "other_calls": by_kind.get("other", 0),
            "total_requests": len(calls),
            "by_kind": by_kind,
            "by_path": by_path,
            "since": self._started,
            "calls": calls,
        }

    def reset(self) -> dict[str, Any]:
        previous = self.stats()
        with self._lock:
            self._calls.clear()
            self._started = time.time()
        return {"reset": True, "discarded": previous["total_requests"]}


def classify(path: str) -> str:
    """`inference` (a model ran), `metadata` (server chatter), or `other`."""
    bare = path.split("?", 1)[0].rstrip("/") or "/"
    for suffix in INFERENCE_PATHS:
        if bare.endswith(suffix):
            return "inference"
    for suffix in METADATA_PATHS:
        if bare.endswith(suffix):
            return "metadata"
    return "other"


def _model_of(body: bytes) -> str | None:
    """The `model` field, if the body is JSON that has one. Best effort."""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str):
            return model
    return None


class CountingProxyHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 so streaming responses can be relayed chunked rather than
    # forcing a connection close per turn, which would change the latency
    # profile of the thing being measured.
    protocol_version = "HTTP/1.1"

    server_version = "OllamaCallCounter/1.0"

    # Injected by `serve`.
    log: CallLog
    upstream: str
    audit_path: str | None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    # -- control plane ----------------------------------------------------

    def _handle(self) -> None:
        if self.path.startswith(CONTROL_PREFIX):
            self._control()
            return
        self._proxy()

    def _control(self) -> None:
        action = self.path[len(CONTROL_PREFIX) :].split("?", 1)[0].strip("/")
        if action == "stats":
            self._json(200, self.log.stats())
        elif action == "reset" and self.command == "POST":
            self._json(200, self.log.reset())
        elif action == "health":
            self._json(200, {"ok": True, "upstream": self.upstream})
        else:
            self._json(404, {"error": f"no counter endpoint {action!r}"})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- proxy ------------------------------------------------------------

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # Counted before the forward, so a call to a dead upstream still
        # counts. This ordering is the whole point of the instrument.
        entry = self.log.record(self.command, self.path, body)
        self._audit(entry)

        request = urllib.request.Request(
            url=self.upstream.rstrip("/") + self.path,
            data=body if body else None,
            method=self.command,
        )
        for name, value in self.headers.items():
            if name.lower() not in _HOP_BY_HOP:
                request.add_header(name, value)
        if body:
            request.add_header("Content-Length", str(len(body)))

        try:
            with urllib.request.urlopen(request) as upstream:  # noqa: S310 - fixed local upstream
                self._relay(upstream.status, upstream.headers.items(), upstream)
        except urllib.error.HTTPError as exc:
            # An upstream 4xx/5xx is a real answer; relay it verbatim so the
            # client sees what Ollama actually said.
            with exc:
                self._relay(exc.code, exc.headers.items(), exc)
        except OSError as exc:
            self._json(502, {"error": f"upstream {self.upstream} unreachable: {exc}"})

    def _relay(self, status: int, headers: Any, upstream: Any) -> None:
        self.send_response(status)
        forwarded = {}
        for name, value in headers:
            if name.lower() in _HOP_BY_HOP:
                continue
            forwarded[name.lower()] = value
            self.send_header(name, value)

        content_length = forwarded.get("content-length")
        if content_length is None:
            # Streaming (Ollama's NDJSON / SSE): re-frame as chunked rather
            # than buffering, so token-by-token delivery is preserved and the
            # proxy does not turn a streaming turn into a stalled one.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._relay_chunked(upstream)
        else:
            self.end_headers()
            self._relay_sized(upstream, int(content_length))

    def _relay_chunked(self, upstream: Any) -> None:
        while True:
            chunk = upstream.read(8192)
            if not chunk:
                break
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _relay_sized(self, upstream: Any, remaining: int) -> None:
        while remaining > 0:
            chunk = upstream.read(min(8192, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            self.wfile.write(chunk)
        self.wfile.flush()

    def _audit(self, entry: dict[str, Any]) -> None:
        if not self.audit_path:
            return
        with open(self.audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler logs every request to stderr by default,
        # which buries the counts under proxy noise. The CallLog is the
        # record that matters; `--verbose` puts the noise back.
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class CountingProxy:
    """A running proxy. Usable as a context manager from a test."""

    def __init__(
        self,
        upstream: str = "http://127.0.0.1:11434",
        port: int = 0,
        audit_path: str | None = None,
        verbose: bool = False,
    ) -> None:
        self.log = CallLog()
        self.upstream = upstream

        handler = type(
            "BoundCountingProxyHandler",
            (CountingProxyHandler,),
            {"log": self.log, "upstream": upstream, "audit_path": audit_path},
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self._server.verbose = verbose  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """What to set `BOOKKEEPER_OLLAMA_URL` to (no `/api` suffix)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def api_url(self) -> str:
        """What to set `OLLAMA_BASE_URL` to (the web app appends nothing)."""
        return f"{self.base_url}/api"

    @property
    def llm_calls(self) -> int:
        return self.log.stats()["llm_calls"]

    def start(self) -> CountingProxy:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> CountingProxy:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=11435, help="port to listen on (default 11435)")
    parser.add_argument(
        "--upstream",
        default="http://127.0.0.1:11434",
        help="real Ollama base URL (default http://127.0.0.1:11434)",
    )
    parser.add_argument("--audit", default=None, help="append every request to this NDJSON file")
    parser.add_argument("--verbose", action="store_true", help="log each proxied request to stderr")
    args = parser.parse_args(argv)

    proxy = CountingProxy(
        upstream=args.upstream, port=args.port, audit_path=args.audit, verbose=args.verbose
    )
    print(f"counting proxy on {proxy.base_url} -> {args.upstream}", file=sys.stderr)
    print(f"  OLLAMA_BASE_URL={proxy.api_url}", file=sys.stderr)
    print(f"  BOOKKEEPER_OLLAMA_URL={proxy.base_url}", file=sys.stderr)
    print(f"  stats: {proxy.base_url}/__counter/stats", file=sys.stderr)
    proxy.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stats = proxy.log.stats()
        print(json.dumps({k: v for k, v in stats.items() if k != "calls"}, indent=2))
        proxy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
