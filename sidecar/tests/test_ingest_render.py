from __future__ import annotations

from datetime import date
from decimal import Decimal

from bookkeeper.ingest.normalize import NormalizedBalance, NormalizedTransaction
from bookkeeper.ingest.render import render_balance_line, render_transaction


def test_render_transaction_is_valid_beancount_shape():
    txn = NormalizedTransaction(
        simplefin_id="TXN-1",
        posted_date=date(2026, 7, 15),
        amount=Decimal("-42.10"),
        currency="USD",
        description="SQ *COFFEE 4TH ST",
        asset_account="Assets:SimpleFIN:Checking-abc123",
    )
    rendered = render_transaction(txn)

    assert rendered == (
        '2026-07-15 * "SQ *COFFEE 4TH ST"\n'
        '  simplefin-id: "TXN-1"\n'
        '  simplefin-account: "Assets:SimpleFIN:Checking-abc123"\n'
        "  Assets:SimpleFIN:Checking-abc123   -42.10 USD\n"
        "  Expenses:Unknown\n"
        "\n"
    )


def test_render_transaction_escapes_quotes_and_backslashes_in_description():
    txn = NormalizedTransaction(
        simplefin_id="TXN-1",
        posted_date=date(2026, 7, 15),
        amount=Decimal("-1.00"),
        currency="USD",
        description='Weird "quoted" desc \\ with backslash',
        asset_account="Assets:SimpleFIN:Checking-abc123",
    )
    rendered = render_transaction(txn)

    assert '\\"quoted\\"' in rendered
    assert "\\\\ with backslash" in rendered
    # And the escaped text still round-trips to a single well-formed line.
    header_line = rendered.splitlines()[0]
    assert header_line.startswith('2026-07-15 * "')
    assert header_line.endswith('"')


def test_render_transaction_preserves_exact_decimal_string():
    txn = NormalizedTransaction(
        simplefin_id="TXN-1",
        posted_date=date(2026, 1, 1),
        amount=Decimal("100.00"),  # trailing zeros matter for byte-identical output
        currency="USD",
        description="x",
        asset_account="Assets:SimpleFIN:Checking-abc123",
    )
    assert "100.00 USD" in render_transaction(txn)


def test_render_transaction_includes_optional_metadata_when_present():
    txn = NormalizedTransaction(
        simplefin_id="TXN-1",
        posted_date=date(2026, 7, 15),
        amount=Decimal("-42.10"),
        currency="USD",
        description="SQ *COFFEE 4TH ST",
        asset_account="Assets:Checking",
        mcc="5411",
        payee="Square Inc",
        memo="in-store purchase",
        extra={"category": "food"},
    )
    rendered = render_transaction(txn)

    assert rendered == (
        '2026-07-15 * "SQ *COFFEE 4TH ST"\n'
        '  simplefin-id: "TXN-1"\n'
        '  simplefin-account: "Assets:Checking"\n'
        '  simplefin-mcc: "5411"\n'
        '  simplefin-payee: "Square Inc"\n'
        '  simplefin-memo: "in-store purchase"\n'
        '  simplefin-extra: "{\\"category\\": \\"food\\"}"\n'
        "  Assets:Checking   -42.10 USD\n"
        "  Expenses:Unknown\n"
        "\n"
    )


def test_render_transaction_omits_optional_metadata_when_absent():
    # A real bank feed sending none of mcc/payee/memo/extra must render
    # exactly as it did before these fields existed.
    txn = NormalizedTransaction(
        simplefin_id="TXN-1",
        posted_date=date(2026, 7, 15),
        amount=Decimal("-42.10"),
        currency="USD",
        description="SQ *COFFEE 4TH ST",
        asset_account="Assets:Checking",
    )
    rendered = render_transaction(txn)

    assert 'simplefin-account: "Assets:Checking"' in rendered  # always present, unlike the rest
    assert "simplefin-mcc" not in rendered
    assert "simplefin-payee" not in rendered
    assert "simplefin-memo" not in rendered
    assert "simplefin-extra" not in rendered


def test_render_balance_line_format():
    bal = NormalizedBalance(
        account="Assets:SimpleFIN:Checking-abc123",
        amount=Decimal("1234.56"),
        currency="USD",
        assertion_date=date(2026, 7, 16),
    )
    assert (
        render_balance_line(bal)
        == "2026-07-16 balance Assets:SimpleFIN:Checking-abc123   1234.56 USD"
    )
