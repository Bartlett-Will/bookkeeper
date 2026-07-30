"""SimpleFIN `/accounts` client.

One call per sync, covering every account -- the protocol's rate limit is
~24 requests/day (PLAN.md 3.1), so this must never be invoked per-account
or in a loop. Every raw response is archived verbatim to
`paths.raw_dir()` *before* parsing, so re-parsing a past fetch never costs
an API request.

The Access URL carries embedded Basic Auth credentials; it is passed
straight to httpx (which extracts and sends the userinfo as an
Authorization header) and is never included in logs or error messages --
only the host, which is not sensitive.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from bookkeeper import paths
from bookkeeper.simplefin.models import AccountSet

_FETCH_TIMEOUT = 30.0


class FetchError(RuntimeError):
    """The `/accounts` request failed. Never includes the Access URL."""


def _redact_host(access_url: str) -> str:
    return urlsplit(access_url).hostname or "<unknown-host>"


def fetch_accounts(
    access_url: str,
    *,
    start_date: int | None = None,
    end_date: int | None = None,
) -> AccountSet:
    """Fetch every account and its transactions in a single request.

    `start_date` / `end_date` are Unix epoch seconds, per protocol. Pending
    transactions are not requested (the `pending` query param is omitted),
    since PLAN.md's risk table calls out pending->posted id instability --
    ingest additionally filters defensively in case the server ever
    includes one anyway.
    """
    url = access_url.rstrip("/") + "/accounts"
    params: dict[str, int] = {}
    if start_date is not None:
        params["start-date"] = start_date
    if end_date is not None:
        params["end-date"] = end_date

    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT) as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise FetchError(
            f"request to {_redact_host(access_url)} failed: {exc.__class__.__name__}"
        ) from exc

    if response.status_code != 200:
        raise FetchError(
            f"unexpected {response.status_code} response from "
            f"{_redact_host(access_url)}/accounts"
        )

    raw_dir = paths.raw_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    archive_path = raw_dir / f"simplefin-{ts}.json"
    # Verbatim bytes, archived before any parsing is attempted.
    archive_path.write_bytes(response.content)

    payload = json.loads(response.content)
    return AccountSet.model_validate(payload)
