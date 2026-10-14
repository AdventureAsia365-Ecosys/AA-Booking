"""Staff JWT verification against a local RSA key + fake JWKS (no network)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import tokens
from app.auth.tokens import AuthUnavailable, InvalidToken, JwksCache, StaffTokenVerifier, http_fetcher
from tests.staff_tokens import ISSUER, LocalCis

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_valid_token_yields_claims() -> None:
    cis = LocalCis()
    claims = await cis.verifier().verify(cis.token(cis_role="reviewer", email="  A@B.test "))
    assert claims.sub == "6f1c1f9e-0000-4000-8000-000000000001"
    assert claims.cis_role == "reviewer"
    assert claims.email == "A@B.test"
    assert claims.name == "Staff One"


async def test_optional_profile_claims_may_be_absent() -> None:
    cis = LocalCis()
    claims = await cis.verifier().verify(cis.token(email=None, name=None))
    assert claims.email is None and claims.name is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"aud": "cis"},
        {"iss": "https://evil.test"},
        {"exp_in": -120},
        {"cis_role": None},
        {"sub": ""},
        {"sub": "x" * 200},
        {"kid": ""},
        {"kid": "unknown"},
    ],
    ids=["aud", "iss", "expired", "no-cis-role", "empty-sub", "long-sub", "no-kid", "unknown-kid"],
)
async def test_rejected_tokens(kwargs: dict[str, Any]) -> None:
    cis = LocalCis()
    with pytest.raises(InvalidToken):
        await cis.verifier().verify(cis.token(**kwargs))


async def test_token_signed_by_another_key_is_rejected() -> None:
    cis = LocalCis()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        await cis.verifier().verify(cis.token(key=other))


async def test_hs256_token_is_rejected_before_any_key_lookup() -> None:
    cis = LocalCis()
    forged = cis.token(key="a-shared-secret-of-sufficient-length-1234", algorithm="HS256")
    with pytest.raises(InvalidToken, match="algorithm"):
        await cis.verifier().verify(forged)
    assert cis.fetches == 0


async def test_malformed_token() -> None:
    with pytest.raises(InvalidToken, match="malformed"):
        await LocalCis().verifier().verify("not-a-jwt")


async def test_slight_clock_skew_is_tolerated() -> None:
    cis = LocalCis()
    await cis.verifier().verify(cis.token(exp_in=-10))


# ── JWKS cache ───────────────────────────────────────────────────────────────


async def test_jwks_is_cached_for_the_ttl_then_refetched() -> None:
    cis, clock = LocalCis(), Clock()
    cache = JwksCache(cis.fetch, clock=clock)
    await cache.key("k1")
    await cache.key("k1")
    assert cis.fetches == 1
    clock.now += tokens.JWKS_TTL_S
    await cache.key("k1")
    assert cis.fetches == 2


async def test_unknown_kid_refetches_once_per_window() -> None:
    cis, clock = LocalCis(), Clock()
    cache = JwksCache(cis.fetch, clock=clock)
    await cache.key("k1")
    cis.kid = "k2"  # CIS rotated its key
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
    cis, clock = LocalCis(), Clock()
    cache = JwksCache(cis.fetch, clock=clock)
    await cache.key("k1")
    cis.fail_next = True
    clock.now += tokens.JWKS_TTL_S + 1
    assert await cache.key("k1") is not None
    assert cis.fetches == 2


async def test_no_keys_at_all_is_auth_unavailable() -> None:
    cis, clock = LocalCis(), Clock()
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
    cis = LocalCis()
    cis.fail_next = True
    with pytest.raises(AuthUnavailable):
        await StaffTokenVerifier(JwksCache(cis.fetch), ISSUER).verify(cis.token())


async def test_non_rsa_and_broken_keys_are_ignored() -> None:
    cis = LocalCis()
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
