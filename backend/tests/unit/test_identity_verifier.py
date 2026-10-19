# Byte copy of AA-CIS-App tests/unit/test_identity_verifier.py up to its CIS identity_config section, with only the
# import path changed (api.core -> app.auth). The verifier itself (app/auth/identity_verifier.py) is a byte copy, drift-
# checked by CI "Vendored CIS copies"; the Booking issuer configuration is tested in test_identity_config.py.
"""AA-854 / AA-816 — multi-issuer staff token verifier (root ADR 0006 §8).

A local RSA "issuer" serves a fake JWKS and mints Cognito-shaped access tokens; no network, no AWS.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.identity_verifier import (
    COLD_RETRY_S,
    JWKS_TTL_S,
    UNKNOWN_KID_REFETCH_S,
    AuthUnavailable,
    InvalidToken,
    IssuerConfig,
    MultiIssuerVerifier,
    Principal,
    http_fetcher,
)

ISS_A = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPOOLA"
ISS_B = "https://cis.bridge.test"
CIS_ADMIN = "cisadminclient000000000001"
BREAKGLASS = "breakglassclient0000000001"
BOOKING_STAFF = "bookingstaffclient00000001"


def _rsa_key(bits: int = 2048) -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class LocalIssuer:
    """One issuer: RSA keys by kid, a JWKS document and token minting."""

    def __init__(self, iss: str, kid: str = "k1", key: Any = None) -> None:
        self.iss = iss
        self.keys: dict[str, Any] = {kid: key or _rsa_key()}
        self.kid = kid

    def jwks(self) -> dict[str, Any]:
        out = []
        for kid, key in self.keys.items():
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": out}

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        base: dict[str, Any] = {
            "iss": self.iss,
            "sub": "4a1b2c3d-0000-4000-8000-000000000001",
            "client_id": CIS_ADMIN,
            "token_use": "access",
            "scope": "openid email profile",
            "username": "4a1b2c3d-0000-4000-8000-000000000001",
            "cognito:groups": ["cis-admin", "booking-admin"],
            "email": "staff@adventure.asia",
            "iat": now,
            "auth_time": now,
            "exp": now + 900,
            "jti": "j-1",
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not _DROP}

    def token(self, *, kid: str | None = None, key: Any = None, headers: dict[str, Any] | None = None,
              **claims: Any) -> str:
        use_kid = kid or self.kid
        signing = key or self.keys[use_kid]
        # JWS-level encode: no client-side claim validation, so malformed claims (e.g. a numeric iss) can be minted.
        payload = json.dumps(self.claims(**claims)).encode()
        return jwt.api_jws.PyJWS().encode(payload, signing, algorithm="RS256",
                                          headers={"kid": use_kid, **(headers or {})})


_DROP = object()


class FakeFetcher:
    """Serves JWKS documents by URL and records every fetch; can be told to fail."""

    def __init__(self, *issuers: LocalIssuer) -> None:
        self.issuers = {f"{i.iss}/.well-known/jwks.json": i for i in issuers}
        self.calls: list[str] = []
        self.fail = False
        self.gate: asyncio.Event | None = None  # when set, every fetch waits for the event (slow JWKS)

    async def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise OSError("NAT blip")
        if url not in self.issuers:
            raise OSError(f"unexpected URL {url}")
        return self.issuers[url].jwks()


def cognito_cfg(iss: str = ISS_A, **kw: Any) -> IssuerConfig:
    return IssuerConfig(issuer=iss, client_ids=frozenset({CIS_ADMIN, BREAKGLASS}), **kw)


def bridge_cfg(iss: str = ISS_B) -> IssuerConfig:
    return IssuerConfig(issuer=iss, client_ids=frozenset({"booking"}), audience_claim="aud", token_use=None,
                        groups_claim="cis_role")


@pytest.fixture(scope="module")
def issuer_a() -> LocalIssuer:
    return LocalIssuer(ISS_A, kid="ka")


@pytest.fixture(scope="module")
def issuer_b() -> LocalIssuer:
    return LocalIssuer(ISS_B, kid="kb")


def _verifier(*cfgs: IssuerConfig, fetcher: FakeFetcher, clock: Clock | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier(cfgs, fetcher, clock=clock or Clock())


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_cis_admin_access_token_returns_principal(issuer_a):
    fetcher = FakeFetcher(issuer_a)
    p = await _verifier(cognito_cfg(), fetcher=fetcher).verify(issuer_a.token())
    assert p == Principal(
        iss=ISS_A,
        sub="4a1b2c3d-0000-4000-8000-000000000001",
        client_id=CIS_ADMIN,
        groups=("cis-admin", "booking-admin"),
        email="staff@adventure.asia",
        email_verified=None,
        username="4a1b2c3d-0000-4000-8000-000000000001",
    )
    assert fetcher.calls == [f"{ISS_A}/.well-known/jwks.json"]
    with pytest.raises(AttributeError):
        p.sub = "other"  # type: ignore[misc]  # frozen


@pytest.mark.asyncio
async def test_breakglass_client_accepted_and_missing_optional_claims(issuer_a):
    v = _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a))
    p = await v.verify(issuer_a.token(client_id=BREAKGLASS, email=_DROP, username=_DROP, **{"cognito:groups": _DROP}))
    assert (p.client_id, p.groups, p.email, p.username) == (BREAKGLASS, (), None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim, expected", [(True, True), (False, False), ("true", True), ("false", False),
                                             ("True", None), ("yes", None), (1, None), (_DROP, None)])
async def test_email_verified_accepts_bools_and_exact_trigger_strings_only(issuer_a, claim, expected):
    # The EXTERNAL pre token trigger emits "true"/"false" strings (identity_triggers/external.py).
    v = _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a))
    assert (await v.verify(issuer_a.token(email_verified=claim))).email_verified is expected


@pytest.mark.asyncio
async def test_overlong_email_or_username_is_dropped_not_truncated(issuer_a):
    v = _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a))
    long_email = "a" * 310 + "@adventure.asia"
    p = await v.verify(issuer_a.token(email=long_email, username="u" * 321))
    assert (p.email, p.username) == (None, None)
    p = await v.verify(issuer_a.token(email="  Staff@Adventure.Asia  "))
    assert p.email == "Staff@Adventure.Asia"


@pytest.mark.asyncio
async def test_aud_claim_is_not_checked_for_cognito_access_tokens(issuer_a):
    v = _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a))
    p = await v.verify(issuer_a.token(aud="something-else"))
    assert p.client_id == CIS_ADMIN


# ---------------------------------------------------------------- rule 1: algorithm and headers


def _unsigned(header: dict[str, Any], claims: dict[str, Any]) -> str:
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}."


def _hs256(header: dict[str, Any], claims: dict[str, Any], secret: bytes) -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    sig = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


@pytest.mark.asyncio
async def test_alg_none_rejected_without_fetching(issuer_a):
    fetcher = FakeFetcher(issuer_a)
    token = _unsigned({"alg": "none", "typ": "JWT", "kid": "ka"}, issuer_a.claims())
    with pytest.raises(InvalidToken, match="unexpected algorithm"):
        await _verifier(cognito_cfg(), fetcher=fetcher).verify(token)
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_hs256_key_confusion_with_public_key_rejected(issuer_a):
    """Classic RS->HS confusion: HMAC-sign with the issuer's public key PEM as the secret."""
    pem = issuer_a.keys["ka"].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    token = _hs256({"alg": "HS256", "typ": "JWT", "kid": "ka"}, issuer_a.claims(), pem)
    with pytest.raises(InvalidToken, match="unexpected algorithm"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("alg", ["HS256", "HS512", "RS384", "RS512", "PS256", "ES256"])
async def test_other_algorithms_rejected(issuer_a, alg):
    header = {"alg": alg, "typ": "JWT", "kid": "ka"}
    token = _hs256(header, issuer_a.claims(), b"x" * 64)  # signature irrelevant: rejected on the header
    with pytest.raises(InvalidToken, match="unexpected algorithm"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(token)


@pytest.mark.asyncio
async def test_embedded_jwk_header_is_never_used(issuer_a):
    attacker = _rsa_key()
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(attacker.public_key(), as_dict=True)
    v = _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a))
    # Attacker kid not in the issuer's JWKS -> unknown key.
    with pytest.raises(InvalidToken, match="unknown signing key"):
        await v.verify(issuer_a.token(kid="evil", key=attacker, headers={"jwk": {**jwk, "kid": "evil"}}))
    # Real kid, attacker signature, attacker key embedded -> signature check against the JWKS key fails.
    with pytest.raises(InvalidToken, match="InvalidSignatureError"):
        await v.verify(issuer_a.token(kid="ka", key=attacker, headers={"jwk": {**jwk, "kid": "ka"}}))


@pytest.mark.asyncio
async def test_jku_and_x5u_headers_are_never_fetched(issuer_a):
    attacker = _rsa_key()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher)
    headers = {"jku": "https://evil.test/jwks.json", "x5u": "https://evil.test/cert.pem"}
    with pytest.raises(InvalidToken):
        await v.verify(issuer_a.token(kid="evil", key=attacker, headers=headers))
    assert all(url == f"{ISS_A}/.well-known/jwks.json" for url in fetcher.calls)
    # A genuine token carrying the same headers still verifies against the issuer's own JWKS.
    assert (await v.verify(issuer_a.token(headers=headers))).client_id == CIS_ADMIN


@pytest.mark.asyncio
async def test_missing_kid_rejected(issuer_a):
    token = jwt.encode(issuer_a.claims(), issuer_a.keys["ka"], algorithm="RS256")
    with pytest.raises(InvalidToken, match="missing kid"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", None, "x" * 9000])
async def test_malformed_tokens_rejected(issuer_a, token):
    with pytest.raises(InvalidToken, match="malformed"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(token)


def _pyjwt_version() -> tuple[int, ...]:
    return tuple(int(part) for part in jwt.__version__.split(".")[:2] if part.isdigit())


@pytest.mark.asyncio
@pytest.mark.skipif(_pyjwt_version() < (2, 15), reason="PyJWT < 2.15 ignores the per-call "
                    "enforce_minimum_key_length option; requirements.txt pins 2.15.1 (CI)")
async def test_small_rsa_key_rejected():
    weak = LocalIssuer(ISS_A, kid="weak", key=_rsa_key(1024))
    with pytest.raises(InvalidToken):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(weak)).verify(weak.token())


# ---------------------------------------------------------------- rule 2: issuer selection


@pytest.mark.asyncio
@pytest.mark.parametrize("iss", ["https://evil.test", ISS_A + "/", "https://aa365-staff-auth-dev.auth.us-west-1"
                                 ".amazoncognito.com", 12345, _DROP])
async def test_unknown_or_missing_issuer_rejected_without_fetching(issuer_a, iss):
    fetcher = FakeFetcher(issuer_a)
    with pytest.raises(InvalidToken, match="unknown issuer"):
        await _verifier(cognito_cfg(), fetcher=fetcher).verify(issuer_a.token(iss=iss))
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_kid_from_the_other_issuer_is_not_looked_up(issuer_a, issuer_b):
    fetcher = FakeFetcher(issuer_a, issuer_b)
    v = _verifier(cognito_cfg(), bridge_cfg(), fetcher=fetcher)
    await v.prefetch()
    # Claims the Cognito issuer, signed with the bridge key under the bridge kid.
    forged = issuer_b.token(kid="kb", iss=ISS_A)
    with pytest.raises(InvalidToken, match="unknown signing key"):
        await v.verify(forged)
    # And the reverse: claims the bridge issuer with the Cognito kid.
    forged_b = issuer_a.token(kid="ka", iss=ISS_B, aud="booking")
    with pytest.raises(InvalidToken, match="unknown signing key"):
        await v.verify(forged_b)


@pytest.mark.asyncio
async def test_same_kid_in_two_issuers_still_uses_only_the_claimed_issuer_key():
    a = LocalIssuer(ISS_A, kid="shared")
    b = LocalIssuer(ISS_B, kid="shared")
    v = _verifier(cognito_cfg(), bridge_cfg(), fetcher=FakeFetcher(a, b))
    with pytest.raises(InvalidToken, match="InvalidSignatureError"):
        await v.verify(b.token(iss=ISS_A))
    assert (await v.verify(a.token())).iss == ISS_A


# ---------------------------------------------------------------- rules 4-6: claims


@pytest.mark.asyncio
async def test_id_token_rejected(issuer_a):
    id_token = issuer_a.token(token_use="id", aud=CIS_ADMIN, client_id=_DROP)
    with pytest.raises(InvalidToken):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(id_token)
    with pytest.raises(InvalidToken, match="wrong token_use"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(issuer_a.token(token_use="id"))


@pytest.mark.asyncio
async def test_missing_token_use_rejected(issuer_a):
    with pytest.raises(InvalidToken, match="MissingRequiredClaimError"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(issuer_a.token(token_use=_DROP))


@pytest.mark.asyncio
@pytest.mark.parametrize("client_id", [BOOKING_STAFF, "", ["x"], CIS_ADMIN.upper()])
async def test_client_not_in_allowlist_rejected(issuer_a, client_id):
    with pytest.raises(InvalidToken, match="client not allowed"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(issuer_a.token(client_id=client_id))


@pytest.mark.asyncio
async def test_missing_client_id_rejected(issuer_a):
    with pytest.raises(InvalidToken, match="MissingRequiredClaimError"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(issuer_a.token(client_id=_DROP))


@pytest.mark.asyncio
async def test_expired_token_rejected_but_within_leeway_accepted(issuer_a):
    v = _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a))
    now = int(time.time())
    with pytest.raises(InvalidToken, match="ExpiredSignatureError"):
        await v.verify(issuer_a.token(exp=now - 120))
    assert (await v.verify(issuer_a.token(exp=now - 10))).client_id == CIS_ADMIN


@pytest.mark.asyncio
async def test_missing_exp_rejected(issuer_a):
    with pytest.raises(InvalidToken, match="MissingRequiredClaimError"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(issuer_a.token(exp=_DROP))


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["", "   ", "s" * 129])
async def test_bad_sub_rejected(issuer_a, sub):
    with pytest.raises(InvalidToken):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(issuer_a.token(sub=sub))


@pytest.mark.asyncio
async def test_tampered_payload_rejected(issuer_a):
    head, _, sig = issuer_a.token().split(".")
    payload = _b64(json.dumps(issuer_a.claims(**{"cognito:groups": ["cis-admin", "owner"]})).encode())
    with pytest.raises(InvalidToken, match="InvalidSignatureError"):
        await _verifier(cognito_cfg(), fetcher=FakeFetcher(issuer_a)).verify(f"{head}.{payload}.{sig}")


@pytest.mark.asyncio
async def test_bridge_profile_checks_aud_and_skips_token_use(issuer_b):
    v = _verifier(bridge_cfg(), fetcher=FakeFetcher(issuer_b))
    claims = {"aud": "booking", "token_use": _DROP, "client_id": _DROP, "cis_role": "admin",
              "cognito:groups": _DROP}
    p = await v.verify(issuer_b.token(**claims))
    assert (p.iss, p.client_id, p.groups) == (ISS_B, "booking", ("admin",))
    with pytest.raises(InvalidToken, match="InvalidAudienceError"):
        await v.verify(issuer_b.token(**{**claims, "aud": "other"}))
    with pytest.raises(InvalidToken, match="MissingRequiredClaimError"):
        await v.verify(issuer_b.token(**{**claims, "aud": _DROP}))


# ---------------------------------------------------------------- rule 3: JWKS cache


@pytest.mark.asyncio
async def test_jwks_outage_keeps_last_good_keys(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    assert await v.prefetch() == {ISS_A: 1}
    fetcher.fail = True
    clock.now += JWKS_TTL_S + 1  # key set is stale: a refresh is attempted and fails
    assert (await v.verify(issuer_a.token())).client_id == CIS_ADMIN
    assert len(fetcher.calls) == 2
    # Unknown kid during the outage is still a 401, not a 503 (a key set is held).
    with pytest.raises(InvalidToken, match="unknown signing key"):
        await v.verify(issuer_a.token(kid="ka", headers={"kid": "rotated"}))


@pytest.mark.asyncio
async def test_bad_jwks_document_keeps_last_good_keys(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    await v.prefetch()
    original = issuer_a.jwks
    try:
        issuer_a.jwks = lambda: {"keys": [{"kty": "oct", "kid": "x", "k": "AAAA"}]}  # type: ignore[method-assign]
        clock.now += JWKS_TTL_S + 1
        assert (await v.verify(issuer_a.token())).client_id == CIS_ADMIN
    finally:
        issuer_a.jwks = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_unknown_kid_refetch_is_rate_limited(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    await v.prefetch()
    attacker = _rsa_key()
    for i in range(5):
        with pytest.raises(InvalidToken, match="unknown signing key"):
            await v.verify(issuer_a.token(kid=f"random-{i}", key=attacker))
        clock.now += 1
    assert len(fetcher.calls) == 1  # prefetch only: refetch window not yet open
    clock.now += UNKNOWN_KID_REFETCH_S
    with pytest.raises(InvalidToken, match="unknown signing key"):
        await v.verify(issuer_a.token(kid="random-x", key=attacker))
    with pytest.raises(InvalidToken, match="unknown signing key"):
        await v.verify(issuer_a.token(kid="random-y", key=attacker))
    assert len(fetcher.calls) == 2  # exactly one more fetch per window


@pytest.mark.asyncio
async def test_key_rotation_picked_up_on_unknown_kid():
    issuer = LocalIssuer(ISS_A, kid="old")
    clock = Clock()
    fetcher = FakeFetcher(issuer)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    await v.prefetch()
    issuer.keys["new"] = _rsa_key()
    clock.now += UNKNOWN_KID_REFETCH_S
    assert (await v.verify(issuer.token(kid="new"))).iss == ISS_A
    assert (await v.verify(issuer.token(kid="old"))).iss == ISS_A
    assert len(fetcher.calls) == 2


@pytest.mark.asyncio
async def test_no_keys_ever_is_auth_unavailable_with_short_cold_retry(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    fetcher.fail = True
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    assert await v.prefetch() == {ISS_A: 0}  # never raises
    with pytest.raises(AuthUnavailable):
        await v.verify(issuer_a.token())
    assert len(fetcher.calls) == 1  # within the cold retry window: no new fetch
    fetcher.fail = False
    clock.now += COLD_RETRY_S
    assert (await v.verify(issuer_a.token())).client_id == CIS_ADMIN
    assert len(fetcher.calls) == 2


@pytest.mark.asyncio
async def test_lazy_first_fetch_without_prefetch(issuer_a):
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher)
    await v.verify(issuer_a.token())
    await v.verify(issuer_a.token())
    assert len(fetcher.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_cold_requests_fetch_once(issuer_a):
    fetcher = FakeFetcher(issuer_a)
    fetcher.gate = asyncio.Event()
    v = _verifier(cognito_cfg(), fetcher=fetcher)
    pending = asyncio.gather(*(v.verify(issuer_a.token()) for _ in range(10)))
    await asyncio.sleep(0)
    fetcher.gate.set()
    principals = await pending
    assert {p.client_id for p in principals} == {CIS_ADMIN}
    assert len(fetcher.calls) == 1  # single flight


@pytest.mark.asyncio
async def test_stale_key_set_refreshed_after_ttl(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    await v.prefetch()
    clock.now += JWKS_TTL_S + 1
    assert (await v.verify(issuer_a.token())).client_id == CIS_ADMIN
    assert len(fetcher.calls) == 2  # stale -> one refresh
    await v.verify(issuer_a.token())
    assert len(fetcher.calls) == 2  # fresh again: no fetch


@pytest.mark.asyncio
async def test_stale_key_served_without_waiting_for_an_in_flight_refresh(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    await v.prefetch()
    clock.now += JWKS_TTL_S + 1
    fetcher.gate = asyncio.Event()  # the refresh hangs (slow NAT)
    first = asyncio.ensure_future(v.verify(issuer_a.token()))
    await asyncio.sleep(0)
    assert len(fetcher.calls) == 2 and not first.done()
    # Other requests holding a known kid answer from the stale set at once.
    assert (await asyncio.wait_for(v.verify(issuer_a.token()), timeout=1)).client_id == CIS_ADMIN
    fetcher.gate.set()
    assert (await first).client_id == CIS_ADMIN
    assert len(fetcher.calls) == 2


@pytest.mark.asyncio
async def test_failed_refresh_does_not_make_every_request_wait(issuer_a):
    clock = Clock()
    fetcher = FakeFetcher(issuer_a)
    v = _verifier(cognito_cfg(), fetcher=fetcher, clock=clock)
    await v.prefetch()
    fetcher.fail = True
    clock.now += JWKS_TTL_S + 1
    await v.verify(issuer_a.token())  # one refresh attempt, fails, stale key used
    fetcher.gate = asyncio.Event()  # any further fetch would hang
    for _ in range(5):
        clock.now += 1
        assert (await asyncio.wait_for(v.verify(issuer_a.token()), timeout=1)).client_id == CIS_ADMIN
    assert len(fetcher.calls) == 2  # no new attempt inside the 60 s window
    fetcher.gate.set()


@pytest.mark.asyncio
async def test_prefetch_loads_every_issuer_separately(issuer_a, issuer_b):
    fetcher = FakeFetcher(issuer_a, issuer_b)
    v = _verifier(cognito_cfg(), bridge_cfg(), fetcher=fetcher)
    assert await v.prefetch() == {ISS_A: 1, ISS_B: 1}
    assert sorted(fetcher.calls) == sorted([f"{ISS_A}/.well-known/jwks.json", f"{ISS_B}/.well-known/jwks.json"])


@pytest.mark.asyncio
async def test_http_fetcher_refuses_plain_http():
    with pytest.raises(ValueError, match="https"):
        await http_fetcher()("http://example.test/.well-known/jwks.json")


def test_http_fetcher_refuses_redirects():
    from app.auth.identity_verifier import _RefuseRedirects

    req = urllib.request.Request("https://cognito.test/.well-known/jwks.json")
    with pytest.raises(urllib.error.HTTPError, match="redirect refused"):
        _RefuseRedirects().redirect_request(req, None, 302, "Found", {}, "http://evil.test/jwks.json")


# ---------------------------------------------------------------- configuration


def test_issuer_config_validation():
    with pytest.raises(ValueError, match="https"):
        IssuerConfig(issuer="http://cognito.test/pool", client_ids=frozenset({"a"}))
    with pytest.raises(ValueError, match="trailing slash"):
        IssuerConfig(issuer=ISS_A + "/", client_ids=frozenset({"a"}))
    with pytest.raises(ValueError, match="client id"):
        IssuerConfig(issuer=ISS_A, client_ids=frozenset({""}))
    with pytest.raises(ValueError, match="leeway"):
        IssuerConfig(issuer=ISS_A, client_ids=frozenset({"a"}), leeway_s=61)
    with pytest.raises(ValueError, match="audience_claim"):
        IssuerConfig(issuer=ISS_A, client_ids=frozenset({"a"}), audience_claim="azp")
    assert IssuerConfig(issuer=ISS_A, client_ids=frozenset({"a"})).jwks_url == f"{ISS_A}/.well-known/jwks.json"


def test_duplicate_or_missing_issuers_rejected():
    with pytest.raises(ValueError, match="twice"):
        MultiIssuerVerifier([cognito_cfg(), cognito_cfg()], FakeFetcher())
    with pytest.raises(ValueError, match="at least one"):
        MultiIssuerVerifier([], FakeFetcher())

