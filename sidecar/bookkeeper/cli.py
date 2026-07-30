"""Command-line entry point.

CONTRACT — this file is owned by the team lead. Workers must NOT restructure it.
Each subcommand delegates to one module function with the signature below; fill in
your own module, leave the dispatch table alone. Keeping the CLI at parity with the
future chat tools is a design requirement (PLAN.md §9), not an afterthought.

    claim    -> bookkeeper.simplefin.claim:claim_token(setup_token) -> str
    sync     -> bookkeeper.ingest.sync:run_sync(since=None, demo=False) -> SyncResult
    verify   -> bookkeeper.envelope.verify:run_verify() -> VerifyResult
    envelopes-> bookkeeper.envelope.compute:envelope_report(asof=None) -> EnvelopeReport
    serve    -> bookkeeper.api:serve(host, port)

Each result object must expose `.ok: bool` and `.render() -> str` so this dispatcher
stays free of formatting logic and the exit code is unambiguous.
"""

from __future__ import annotations

import argparse
import sys


def _cmd_claim(args: argparse.Namespace) -> int:
    from bookkeeper.simplefin.claim import claim_token

    path = claim_token(args.setup_token)
    # Never print the Access URL itself — it is a banking credential.
    print(f"Access URL stored at {path} (mode 0600)")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    from bookkeeper.ingest.sync import run_sync

    result = run_sync(since=args.since, demo=args.demo)
    print(result.render())
    return 0 if result.ok else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    from bookkeeper.envelope.verify import run_verify

    result = run_verify()
    print(result.render())
    return 0 if result.ok else 1


def _cmd_envelopes(args: argparse.Namespace) -> int:
    from bookkeeper.envelope.compute import envelope_report

    report = envelope_report(asof=args.asof)
    print(report.render())
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from bookkeeper.api import serve

    serve(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bookkeeper", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("claim", help="Exchange a SimpleFIN setup token for an Access URL")
    c.add_argument("setup_token", help="base64 setup token (demo token works)")
    c.set_defaults(func=_cmd_claim)

    s = sub.add_parser("sync", help="Fetch transactions and write them into the ledger")
    s.add_argument("--since", default=None, help="ISO date lower bound")
    s.add_argument("--demo", action="store_true", help="Use the SimpleFIN demo server")
    s.set_defaults(func=_cmd_sync)

    v = sub.add_parser("verify", help="Run ledger + envelope integrity checks")
    v.set_defaults(func=_cmd_verify)

    e = sub.add_parser("envelopes", help="Show envelope balances")
    e.add_argument("--asof", default=None, help="ISO date; defaults to today")
    e.set_defaults(func=_cmd_envelopes)

    r = sub.add_parser("serve", help="Run the FastAPI sidecar")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8000)
    r.set_defaults(func=_cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
