"""Command-line entry point.

CONTRACT — this file is owned by the team lead. Workers must NOT restructure it.
Each subcommand delegates to one module function with the signature below; fill in
your own module, leave the dispatch table alone. Keeping the CLI at parity with the
future chat tools is a design requirement (PLAN.md §9), not an afterthought.

    claim    -> bookkeeper.simplefin.claim:claim_token(setup_token) -> str
    sync     -> bookkeeper.ingest.sync:run_sync(since=None, demo=False) -> SyncResult
    verify   -> bookkeeper.envelope.verify:run_verify() -> VerifyResult
    envelopes-> bookkeeper.envelope.compute:envelope_report(asof=None) -> EnvelopeReport
    categorize-> bookkeeper.categorize.apply:run_categorize(apply=False, limit=None,
                     use_llm=True) -> CategorizeResult
    review   -> bookkeeper.categorize.review:review_queue(limit=None) -> ReviewQueue
    eval     -> bookkeeper.categorize.evaluate:run_eval(corpus=None, use_llm=False)
                     -> EvalReport
    search   -> bookkeeper.reports.search:search_transactions(q, limit=None)
                     -> TransactionSearch
    report   -> bookkeeper.reports.spending:spending_report(from_date=None, to_date=None,
                     period="month") -> SpendingReport
    allocate -> bookkeeper.envelope.allocate:allocate_to_envelope(envelope, amount,
                     currency="USD", allocated_on=None) -> AllocateResult
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


def _cmd_categorize(args: argparse.Namespace) -> int:
    from bookkeeper.categorize.apply import run_categorize

    result = run_categorize(apply=args.apply, limit=args.limit, use_llm=not args.no_llm)
    print(result.render())
    return 0 if result.ok else 1


def _cmd_review(args: argparse.Namespace) -> int:
    from bookkeeper.categorize.review import review_queue

    queue = review_queue(limit=args.limit)
    print(queue.render())
    return 0 if queue.ok else 1


def _cmd_eval(args: argparse.Namespace) -> int:
    from bookkeeper.categorize.evaluate import run_eval

    report = run_eval(corpus=args.corpus, use_llm=args.llm)
    print(report.render())
    return 0 if report.ok else 1


def _cmd_search(args: argparse.Namespace) -> int:
    from bookkeeper.reports.search import search_transactions

    result = search_transactions(args.query, limit=args.limit)
    print(result.render())
    return 0 if result.ok else 1


def _cmd_report(args: argparse.Namespace) -> int:
    from bookkeeper.reports.spending import spending_report

    # `spending_report` raises on an unparseable date rather than returning a
    # failed result, because a caller that passed a date it cannot parse has
    # no report to be given. Caught here so the CLI answers with a message
    # instead of a traceback.
    try:
        report = spending_report(args.from_date, args.to, args.period)
    except ValueError as exc:
        print(f"report failed: {exc}")
        return 1
    print(report.render())
    return 0 if report.ok else 1


def _cmd_allocate(args: argparse.Namespace) -> int:
    from bookkeeper.envelope.allocate import allocate_to_envelope

    result = allocate_to_envelope(
        args.envelope,
        args.amount,
        currency=args.currency,
        allocated_on=args.on,
    )
    print(result.render())
    return 0 if result.ok else 1


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

    # Categorization (PLAN.md §5.4). `categorize` defaults to a dry run:
    # writing to the ledger is opt-in via --apply, because review-everything
    # is the shipped default (decision 5) and an unattended reclassification
    # of every transaction is exactly the "silently corrupts the ledger" risk.
    cat = sub.add_parser("categorize", help="Predict accounts for uncategorized transactions")
    cat.add_argument(
        "--apply",
        action="store_true",
        help="Write predictions into the ledger (default: dry run, print only)",
    )
    cat.add_argument("--limit", type=int, default=None, help="Only process the first N")
    cat.add_argument(
        "--no-llm", action="store_true", help="Deterministic + statistical tiers only"
    )
    cat.set_defaults(func=_cmd_categorize)

    rv = sub.add_parser("review", help="Show transactions awaiting human categorization")
    rv.add_argument("--limit", type=int, default=None, help="Show at most N entries")
    rv.set_defaults(func=_cmd_review)

    ev = sub.add_parser("eval", help="Measure per-tier categorization accuracy (§5.5)")
    ev.add_argument("--corpus", default=None, help="Path to a labeled corpus JSON")
    ev.add_argument(
        "--llm",
        action="store_true",
        help="Include the LLM tier (slow; requires Ollama). Off by default so CI stays hermetic.",
    )
    ev.set_defaults(func=_cmd_eval)

    # PLAN.md §9: every operation the chat can invoke must also run headless.
    # These three back `search_transactions`, `get_spending_report` and
    # `allocate_to_envelope` (§5.3), so no capability exists only behind a
    # model.
    se = sub.add_parser("search", help="Search transactions by narration, payee or account")
    se.add_argument("query", help="Free text; matched as a literal, not a regex")
    se.add_argument("--limit", type=int, default=None, help="Show at most N matches")
    se.set_defaults(func=_cmd_search)

    rp = sub.add_parser("report", help="Show spending by envelope over time")
    rp.add_argument("--from", dest="from_date", default=None, help="ISO date; inclusive")
    rp.add_argument("--to", default=None, help="ISO date; inclusive")
    rp.add_argument(
        "--period",
        default="month",
        help="Granularity: month (default) or year",
    )
    rp.set_defaults(func=_cmd_report)

    # Amounts stay strings all the way into `allocate_to_envelope`, which
    # does its own `Decimal` conversion. `type=float` here would round the
    # money before the module that cares about cents ever saw it.
    al = sub.add_parser("allocate", help="Move money into an envelope")
    al.add_argument("envelope", help="An envelope the ledger already maps an account to")
    al.add_argument("amount", help="Positive amount, e.g. 125.50")
    al.add_argument("--currency", default="USD")
    al.add_argument("--on", default=None, help="ISO date the allocation is recorded under")
    al.set_defaults(func=_cmd_allocate)

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
