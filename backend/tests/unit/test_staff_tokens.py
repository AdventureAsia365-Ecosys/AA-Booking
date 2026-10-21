"""Cognito WORKFORCE tokens through the Booking staff verifier (``app.auth.identity_config``), the refusal of the
retired CIS bridge token (AA-911), and the single-issuer JWKS cache of ``app.auth.tokens`` that the deploy gate keeps
using. Local RSA keys, fake JWKS, no
network. The verifier's generic rules are covered by the vendored CIS tests (test_identity_verifier.py)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import identity_verifier, tokens
from app.auth.tokens import AuthUnavailable, InvalidToken, JwksCache, http_fetcher
from tests.staff_tokens import (
    BOOKING_STAFF_CLIENT,
    CIS_ADMIN_CLIENT,
    WORKFORCE_ISSUER,
    WORKFORCE_SUB,
    LocalRsaIssuer,
    LocalStaff,
)

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_valid_workforce_token_yields_a_principal() -> None:
    staff = LocalStaff()
    principal = await staff.verifier().verify(staff.workforce.token(["booking-sales"], email="  A@B.test "))
    assert principal.iss == WORKFORCE_ISSUER
    assert principal.sub == WORKFORCE_SUB
    assert principal.client_id == BOOKING_STAFF_CLIENT
    assert principal.groups == ("booking-sales",)
    assert principal.email == "A@B.test"


async def test_optional_profile_claims_may_be_absent() -> None:
    staff = LocalStaff()
    principal = await staff.verifier().verify(staff.workforce.token([], email=None))
    assert principal.email is None and principal.groups == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"client_id": CIS_ADMIN_CLIENT},
        {"iss": "https://evil.test"},
        {"token_use": "id"},
        {"exp_in": -120},
        {"sub": ""},
        {"sub": "x" * 200},
        {"kid": "unknown"},
    ],
    ids=["cis-admin-client", "iss", "id-token", "expired", "empty-sub", "long-sub", "unknown-kid"],
)
async def test_rejected_tokens(kwargs: dict[str, Any]) -> None:
    staff = LocalStaff()
    with pytest.raises(identity_verifier.InvalidToken):
        await staff.verifier().verify(staff.workforce.token(**kwargs))


async def test_token_signed_by_another_key_is_rejected() -> None:
    staff = LocalStaff()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(identity_verifier.InvalidToken):
        await staff.verifier().verify(staff.workforce.token(key=other))


async def test_hs256_token_is_rejected_before_any_key_lookup() -> None:
    staff = LocalStaff()
    forged = staff.workforce.token(key="a-shared-secret-of-sufficient-length-1234", algorithm="HS256")
    with pytest.raises(identity_verifier.InvalidToken, match="algorithm"):
        await staff.verifier().verify(forged)
    assert staff.fetches == 0


async def test_malformed_token() -> None:
    with pytest.raises(identity_verifier.InvalidToken, match="malformed"):
        await LocalStaff().verifier().verify("not-a-jwt")


async def test_a_correctly_signed_bridge_token_is_refused_without_any_key_lookup() -> None:
    """AA-911: a CIS bridge-shaped token (iss = the CIS API, aud = booking, cis_role), signed by a key whose JWKS is
    reachable at the old CIS JWKS URL, is refused: the issuer is not configured, so its keys are never fetched."""
    cis = LocalRsaIssuer()
    staff = LocalStaff(foreign=cis)
    with pytest.raises(identity_verifier.InvalidToken):
        await staff.verifier().verify(cis.token(cis_role="admin"))
    assert staff.fetches == 0


async def test_a_bridge_token_signed_with_the_workforce_key_is_rejected() -> None:
    """Rule 2: the claimed issuer picks the key set; a bridge issuer has none, whatever key signed the token."""
    staff = LocalStaff()
    forged = LocalRsaIssuer().token(key=staff.workforce.private_key, kid=staff.workforce.kid)
    with pytest.raises(identity_verifier.InvalidToken):
        await staff.verifier().verify(forged)


async def test_slight_clock_skew_is_tolerated() -> None:
    staff = LocalStaff()
    await staff.verifier().verify(staff.workforce.token(exp_in=-10))


# ── app.auth.tokens JWKS cache (deploy gate) ─────────────────────────────────


async def test_jwks_is_cached_for_the_ttl_then_refetched() -> None:
    cis, clock = LocalRsaIssuer(), Clock()
    cache = JwksCache(cis.fetch, clock=clock)
    await cache.key("k1")
    await cache.key("k1")
    assert cis.fetches == 1
    clock.now += tokens.JWKS_TTL_S
    await cache.key("k1")
    assert cis.fetches == 2


async def test_unknown_kid_refetches_once_per_window() -> None:
    cis, clock = LocalRsaIssuer(), Clock()
    cache = JwksCache(cis.fetch, clock=clock)
    await cache.key("k1")
    cis.kid = "k2"  # the issuer rotated its key
    clock.now += tokens.UNKNOWN_KID_REFETCH_S
    assert await cache.key("k2") is not None
    assert cis.fetches == 2
    with pytest.raises(InvalidToken):
        await cache.key("k3")  # inside the window: no new fetch
    assert cis.fetches == 2
    clock.now += tokens.UNKNOWN_KID_REFETCH_S
    with pytest.raises(InvalidToken):
        await cache.key("k3")
    assert cis.fetches == 3


async def test_fetch_failure_keeps_the_last_good_keys() -> None:
    cis, clock = LocalRsaIssuer(), Clock()
    cache = JwksCache(cis.fetch, clock=clock)
    await cache.key("k1")
    cis.fail_next = True
    clock.now += tokens.JWKS_TTL_S + 1
    assert await cache.key("k1") is not None
    assert cis.fetches == 2


async def test_no_keys_at_all_is_auth_unavailable() -> None:
    cis, clock = LocalRsaIssuer(), Clock()
    cis.fail_next = True
    cache = JwksCache(cis.fetch, clock=clock)
    with pytest.raises(AuthUnavailable):
        await cache.key("k1")
    with pytest.raises(AuthUnavailable):  # retried only after the window
        await cache.key("k1")
    assert cis.fetches == 1
    cis.fail_next = False
    clock.now += tokens.UNKNOWN_KID_REFETCH_S
    assert await cache.key("k1") is not None


async def test_verifier_maps_missing_jwks_to_auth_unavailable() -> None:
    staff = LocalStaff()
    staff.fail_next = True
    with pytest.raises(identity_verifier.AuthUnavailable):
        await staff.verifier().verify(staff.token())


async def test_non_rsa_and_broken_keys_are_ignored() -> None:
    cis = LocalRsaIssuer()
    good = cis.jwks()["keys"][0]
    document = {
        "keys": [
            {"kty": "oct", "kid": "sym", "k": "c2VjcmV0"},
            {**good, "kid": "enc", "use": "enc"},
            {**good, "kid": "ps", "alg": "PS256"},
            {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
            {"kty": "RSA"},
            "junk",
            good,
        ]
    }

    async def fetch() -> dict[str, Any]:
        return document

    cache = JwksCache(fetch)
    assert await cache.key("k1") is not None
    for kid in ("sym", "enc", "ps", "broken"):
        with pytest.raises(InvalidToken):
            await cache.key(kid)


async def test_http_fetcher_reads_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> io.BytesIO:
        seen["url"], seen["timeout"] = request.full_url, timeout
        return io.BytesIO(json.dumps({"keys": []}).encode())

    monkeypatch.setattr(tokens.urllib.request, "urlopen", fake_urlopen)
    assert await http_fetcher("https://cis.test/jwks.json")() == {"keys": []}
    assert seen == {"url": "https://cis.test/jwks.json", "timeout": tokens.FETCH_TIMEOUT_S}


async def test_http_fetcher_refuses_oversized_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tokens.urllib.request, "urlopen", lambda request, timeout: io.BytesIO(b" " * (tokens.MAX_JWKS_BYTES + 10))
    )
    with pytest.raises(ValueError, match="too large"):
        await http_fetcher("https://cis.test/jwks.json")()
