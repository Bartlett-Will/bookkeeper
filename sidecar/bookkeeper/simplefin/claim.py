"""SimpleFIN claim flow (PLAN.md 3.1, 9).

A base64 setup token decodes to a one-time claim URL. POSTing to it returns
an Access URL with embedded HTTP Basic Auth credentials -- a long-lived
banking credential. It is written to `paths.access_url_file()` at mode
0600 and returned only as a `Path`: the URL itself is never logged, never
printed, and never embedded in an exception message, including in the
error paths below.

A 403 from the claim endpoint means the token does not exist or was
already claimed by someone else, which can indicate compromise -- it is
raised as a distinct exception type so callers can treat it as an alert,
not a retryable failure.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from bookkeeper import paths

# Reusable demo token per simplefin.org/protocol.html. Decodes to
# https://bridge.simplefin.org/simplefin/claim/demo
DEMO_TOKEN = "aHR0cHM6Ly9icmlkZ2Uuc2ltcGxlZmluLm9yZy9zaW1wbGVmaW4vY2xhaW0vZGVtbw=="

_CLAIM_TIMEOUT = 15.0


class ClaimError(RuntimeError):
    """The claim request failed for a reason other than 'already claimed'."""


class AlreadyClaimedError(ClaimError):
    """403 from the claim endpoint -- token unknown or already claimed.

    Per protocol docs this can mean the token was compromised; surface it
    distinctly rather than folding it into a generic error.
    """


def claim_token(setup_token: str) -> Path:
    """Exchange a base64 setup token for an Access URL.

    Writes the Access URL to `paths.access_url_file()` at mode 0600 and
    returns that path. Raises `AlreadyClaimedError` on a 403, `ClaimError`
    for any other failure (bad token, network error, unexpected response).
    """
    try:
        claim_url = base64.b64decode(setup_token, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ClaimError("setup token is not valid base64") from exc

    parsed = urlsplit(claim_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ClaimError("decoded setup token is not a valid http(s) URL")

    try:
        # Deliberately do not follow redirects: bridge.simplefin.org is
        # known to 302 to a bare marketing root that drops the claim path
        # entirely (verified live), which is never a legitimate claim
        # response. Treat any redirect as an unexpected-response error
        # rather than silently chasing it somewhere unrelated.
        with httpx.Client(follow_redirects=False, timeout=_CLAIM_TIMEOUT) as client:
            response = client.post(claim_url)
    except httpx.HTTPError as exc:
        raise ClaimError(
            f"claim request to {parsed.netloc} failed: {exc.__class__.__name__}"
        ) from exc

    if response.status_code == 403:
        raise AlreadyClaimedError(
            "claim token does not exist or was already claimed by someone else "
            "-- this can indicate the token was compromised"
        )
    if response.status_code != 200:
        raise ClaimError(
            f"unexpected {response.status_code} response from claim endpoint "
            f"at {parsed.netloc}"
        )

    access_url = response.text.strip()
    access_parsed = urlsplit(access_url)
    if (
        access_parsed.scheme not in ("http", "https")
        or not access_parsed.hostname
        or access_parsed.username is None
    ):
        raise ClaimError(
            "claim endpoint returned a 200 response that is not a valid Access URL"
        )

    dest = paths.access_url_file()
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Create with restrictive permissions from the start -- no window where
    # the file exists world/group-readable before a later chmod.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(access_url)
    os.chmod(dest, 0o600)
    return dest
