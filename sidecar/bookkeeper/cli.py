"""Command-line entry point.

CONTRACT — this file is owned by the team lead. Workers must NOT restructure it.
Each subcommand delegates to one module function with the signature below; fill in
your own module, leave the dispatch table alone. Keeping the CLI at parity with the
future chat tools is a design requirement (PLAN.md §9), not an afterthought.

    claim    -> bookkeeper.simplefin.claim:claim_token(setup_token) -> str
    sync     -> bookkeeper.ingest.sync:run_sync(since=None, demo=False) -> SyncResult
    backfill -> bookkeeper.ingest.backfill:run_backfill(from_date, to_date=None,
                     dry_run=True, restart=False, max_requests=None, demo=False)
                     -> BackfillResult
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
    month-end-> bookkeeper.reports.monthend:month_end_report(month=None) -> MonthEndReport
    budget   -> bookkeeper.reports.budget:budget_report(from_date=None, to_date=None)
                     -> BudgetReport
    trends   -> bookkeeper.reports.trends:trends_report(from_date=None, to_date=None)
                     -> TrendsReport
    allocate -> bookkeeper.envelope.allocate:allocate_to_envelope(envelope, amount,
                     currency="USD", allocated_on=None) -> AllocateResult
    reconcile-> bookkeeper.reconcile.compare:run_reconcile(account, balance=None, on=None,
                     statement=None, since=None, count=None, window=3,
                     flip_signs=False) -> ReconcileResult
    suggest-rules-> bookkeeper.tuning.rulesuggest:suggest_rules(limit=None,
                     min_occurrences=MIN_OCCURRENCES) -> RuleSuggestions
    envelope-gaps-> bookkeeper.tuning.gaps:envelope_gaps() -> EnvelopeGaps
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


def _cmd_backfill(args: argparse.Namespace) -> int:
    from bookkeeper.ingest.backfill import run_backfill

    if args.dry_run and args.execute:
        print("backfill failed: --dry-run and --execute are mutually exclusive")
        return 1
    result = run_backfill(
        args.from_date,
        args.to,
        dry_run=not args.execute,
        restart=args.restart,
        max_requests=args.max_requests,
        demo=args.demo,
    )
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


def _cmd_month_end(args: argparse.Namespace) -> int:
    from bookkeeper.reports.monthend import month_end_report

    # Same shape as `report`: an unparseable month raises rather than
    # returning a failed result, because a caller who named a month that
    # cannot be parsed has no month to be given a report about.
    try:
        report = month_end_report(args.month)
    except ValueError as exc:
        print(f"month-end report failed: {exc}")
        return 1
    print(report.render())
    return 0 if report.ok else 1


def _cmd_budget(args: argparse.Namespace) -> int:
    from bookkeeper.reports.budget import budget_report

    # Same shape as `report`: an unparseable date raises rather than
    # returning a failed result, so it is caught here and answered with a
    # message instead of a traceback.
    try:
        report = budget_report(args.from_date, args.to)
    except ValueError as exc:
        print(f"budget report failed: {exc}")
        return 1
    print(report.render())
    return 0 if report.ok else 1


def _cmd_trends(args: argparse.Namespace) -> int:
    from bookkeeper.reports.trends import trends_report

    try:
        report = trends_report(args.from_date, args.to)
    except ValueError as exc:
        print(f"trends report failed: {exc}")
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


def _cmd_suggest_rules(args: argparse.Namespace) -> int:
    from bookkeeper.tuning.rulesuggest import suggest_rules

    result = suggest_rules(limit=args.limit, min_occurrences=args.min_occurrences)
    print(result.render())
    return 0 if result.ok else 1


def _cmd_envelope_gaps(args: argparse.Namespace) -> int:
    from bookkeeper.tuning.gaps import envelope_gaps

    result = envelope_gaps()
    print(result.render())
    return 0 if result.ok else 1


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from bookkeeper.reconcile.compare import run_reconcile

    # No try/except here, unlike `report` and its siblings: `run_reconcile`
    # answers an unreadable statement, an unknown account and an unparseable
    # date with a failed result rather than an exception, because the useful
    # thing to print in every one of those cases is a reconciliation report
    # that says why it could not run.
    result = run_reconcile(
        args.account,
        balance=args.balance,
        on=args.on,
        statement=args.statement,
        since=args.since,
        count=args.count,
        window=args.window,
        flip_signs=args.flip_signs,
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

    # Backfill previews by default and requires --execute to spend a request
    # (PLAN.md §3.1: ~24/day, hard). Same shape as `categorize --apply`, and
    # for a sharper reason: a preview that should have been a run costs a
    # retype, a run that should have been a preview costs a fifth of the day's
    # budget and cannot be given back. `--dry-run` is accepted explicitly so a
    # caller who states their intent need not know the default.
    bf = sub.add_parser("backfill", help="Fetch history in <=90-day windows (§3.1)")
    bf.add_argument("--from", dest="from_date", required=True, help="ISO date; inclusive")
    bf.add_argument("--to", default=None, help="ISO date; inclusive. Defaults to today (UTC)")
    bf.add_argument(
        "--dry-run",
        action="store_true",
        help="List the windows that would be requested and spend nothing (the default)",
    )
    bf.add_argument(
        "--execute", action="store_true", help="Actually spend requests against the daily budget"
    )
    bf.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Send at most N requests, then stop and record where to resume",
    )
    bf.add_argument(
        "--restart",
        action="store_true",
        help="Re-fetch windows a previous run already completed (spends real budget)",
    )
    bf.add_argument("--demo", action="store_true", help="Use the SimpleFIN demo server")
    bf.set_defaults(func=_cmd_backfill)

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

    me = sub.add_parser(
        "month-end", help="Composite month-end report: envelopes, budget vs actual, unmapped"
    )
    me.add_argument(
        "--month",
        default=None,
        help="YYYY-MM; defaults to the month of the ledger's last transaction",
    )
    me.set_defaults(func=_cmd_month_end)

    bg = sub.add_parser("budget", help="Show allocated vs actually spent per envelope")
    bg.add_argument("--from", dest="from_date", default=None, help="ISO date; inclusive")
    bg.add_argument("--to", default=None, help="ISO date; inclusive")
    bg.set_defaults(func=_cmd_budget)

    # No `--period`: trends are monthly and only monthly. An envelope budget
    # is a monthly instrument (§5.2), and a granularity flag would let a
    # caller pick the one that produced the answer they wanted.
    tr = sub.add_parser("trends", help="Show spending direction and unusual transactions")
    tr.add_argument("--from", dest="from_date", default=None, help="ISO date; inclusive")
    tr.add_argument("--to", default=None, help="ISO date; inclusive")
    tr.set_defaults(func=_cmd_trends)

    # Amounts stay strings all the way into `allocate_to_envelope`, which
    # does its own `Decimal` conversion. `type=float` here would round the
    # money before the module that cares about cents ever saw it.
    al = sub.add_parser("allocate", help="Move money into an envelope")
    al.add_argument("envelope", help="An envelope the ledger already maps an account to")
    al.add_argument("amount", help="Positive amount, e.g. 125.50")
    al.add_argument("--currency", default="USD")
    al.add_argument("--on", default=None, help="ISO date the allocation is recorded under")
    al.set_defaults(func=_cmd_allocate)

    # Phase 6 tuning. Both are read-only advisors and neither has an --apply
    # flag, deliberately: review-everything is the shipped default (decision
    # 5) and §5.5 raises autonomy only on measured evidence, so a command
    # that wrote the rules it invented would be raising it on none.
    sr = sub.add_parser(
        "suggest-rules", help="Propose rules.yaml entries for recurring merchants"
    )
    sr.add_argument("--limit", type=int, default=None, help="Show at most N suggestions")
    # No default spelled out here: resolving it would mean importing the
    # tuning module (and beancount behind it) to build a parser, on every
    # invocation of every subcommand. The value actually used is echoed in
    # the report's first line and in its `min_occurrences` field, so it is
    # discoverable where it matters rather than only in --help.
    sr.add_argument(
        "--min-occurrences",
        type=int,
        default=None,
        dest="min_occurrences",
        help="Transactions a merchant needs before a rule is proposed",
    )
    sr.set_defaults(func=_cmd_suggest_rules)

    eg = sub.add_parser(
        "envelope-gaps", help="Expense accounts with spending and no envelope mapping"
    )
    eg.set_defaults(func=_cmd_envelope_gaps)

    # Reconciliation against a real bank statement (§5.2 item 3, Phase 6's
    # exit criterion). Its own command rather than a `verify` check because it
    # cannot run without a statement, and `verify` runs unattended on every
    # sync -- a check with no input there is either a permanent no-op or a
    # permanent failure, and both teach the user to ignore a red `verify`.
    #
    # `--balance` stays a string all the way in, exactly as `allocate`'s does:
    # `type=float` would round the statement's cents before the module that
    # exists to compare cents ever saw them.
    rc = sub.add_parser("reconcile", help="Check an account against a bank statement (§5.2)")
    rc.add_argument("account", help="Account or its leaf, e.g. Checking")
    rc.add_argument("--balance", default=None, help="Statement closing balance, e.g. 4212.55")
    rc.add_argument("--on", default=None, help="ISO date the closing balance is for")
    rc.add_argument("--statement", default=None, help="Path to the bank's CSV export")
    rc.add_argument(
        "--since", default=None, help="First ISO date the statement covers (default: its first line)"
    )
    rc.add_argument(
        "--count", type=int, default=None, help="Transaction count the statement states"
    )
    rc.add_argument(
        "--window",
        type=int,
        default=3,
        help="Days apart two records may be dated and still be one transaction (default 3)",
    )
    rc.add_argument(
        "--flip-signs",
        action="store_true",
        dest="flip_signs",
        help="Negate every statement amount (for exports where withdrawals are positive)",
    )
    rc.set_defaults(func=_cmd_reconcile)

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
