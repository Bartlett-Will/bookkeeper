"""Reconciliation: proving the ledger agrees with a bank statement.

PLAN.md §5.2 item 3 names cash reconciliation as the guard that survives the
envelope model -- "if we ever drop or duplicate a transaction, `bean-check`
fails at the next assertion date". That guarantee holds only against the
figures SimpleFIN itself reports. This package checks the ledger against an
*independent* source: the statement the bank mailed you.

Two modules, split the way `envelope` and `ingest` are:

    statement.py   what a statement is, and how to read one off a CSV
    compare.py     the pure comparison, plus the impure ledger-loading wrapper
"""
