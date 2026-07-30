from __future__ import annotations

import base64
import os
import stat

import pytest

from bookkeeper import paths
from bookkeeper.simplefin.claim import AlreadyClaimedError, ClaimError, claim_token

CLAIM_URL = "https://claim.example.com/simplefin/claim/testtoken"
SETUP_TOKEN = base64.b64encode(CLAIM_URL.encode()).decode()


def test_claim_token_success_writes_access_url_at_mode_0600(bookkeeper_root, httpx_mock):
    access_url = "https://user123:secretpass@bridge.example.com/simplefin"
    httpx_mock.add_response(url=CLAIM_URL, method="POST", status_code=200, text=access_url)

    dest = claim_token(SETUP_TOKEN)

    assert dest == paths.access_url_file()
    assert dest.read_text(encoding="utf-8") == access_url
    mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert mode == 0o600


def test_claim_token_403_raises_already_claimed(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(url=CLAIM_URL, method="POST", status_code=403, text="Forbidden")

    with pytest.raises(AlreadyClaimedError):
        claim_token(SETUP_TOKEN)

    assert not paths.access_url_file().exists()


def test_claim_token_unexpected_status_raises_claim_error(bookkeeper_root, httpx_mock):
    # Mirrors what bridge.simplefin.org actually does live: a 302 redirect
    # to a bare marketing root with no claim-relevant body. Must not be
    # mistaken for success or for "already claimed".
    httpx_mock.add_response(url=CLAIM_URL, method="POST", status_code=302, text="")

    with pytest.raises(ClaimError):
        claim_token(SETUP_TOKEN)

    assert not paths.access_url_file().exists()


def test_claim_token_does_not_follow_redirects(bookkeeper_root, httpx_mock):
    # If the client followed the redirect it would issue a second request;
    # pytest-httpx fails the test if a request has no matching mock, which
    # is exactly the assertion we want here.
    httpx_mock.add_response(
        url=CLAIM_URL,
        method="POST",
        status_code=302,
        headers={"location": "https://bridge.example.com/"},
        text="",
    )

    with pytest.raises(ClaimError):
        claim_token(SETUP_TOKEN)


def test_claim_token_rejects_non_url_response_body(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(url=CLAIM_URL, method="POST", status_code=200, text="ok thanks")

    with pytest.raises(ClaimError):
        claim_token(SETUP_TOKEN)

    assert not paths.access_url_file().exists()


def test_claim_token_invalid_base64_raises_without_network_call(bookkeeper_root):
    with pytest.raises(ClaimError):
        claim_token("not-valid-base64!!!")


def test_claim_token_decoded_non_url_raises_without_network_call(bookkeeper_root):
    bad_token = base64.b64encode(b"not a url at all").decode()
    with pytest.raises(ClaimError):
        claim_token(bad_token)


def test_claim_token_never_leaks_response_body_in_exception_message(bookkeeper_root, httpx_mock):
    # Structurally a URL, but with no embedded credentials -- so it fails
    # our Access URL validation. Carries a secret-looking query param to
    # prove that a rejected body never ends up quoted in the error.
    suspicious_body = "https://bridge.example.com/simplefin?leaked_secret=abc123"
    httpx_mock.add_response(url=CLAIM_URL, method="POST", status_code=200, text=suspicious_body)

    with pytest.raises(ClaimError) as exc_info:
        claim_token(SETUP_TOKEN)

    assert "abc123" not in str(exc_info.value)
    assert suspicious_body not in str(exc_info.value)
    assert not paths.access_url_file().exists()
