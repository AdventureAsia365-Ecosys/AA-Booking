"""Deploy gate (AA-751 B3): the GitHub OIDC verifier against a local RSA key + fake JWKS, the HTTP route, and the
``active_count`` aggregation over a scripted pool. Real SQL is in tests/integration/test_job_deploy_gate.py."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import ci
from app.auth.ci import DEPLOY_GATE_AUDIENCE, GITHUB_JWKS_URL, GITHUB_OIDC_ISSUER, GithubDeployVerifier
from app.auth.tokens import AuthUnavailable, InvalidToken, JwksCache
from app.db import Database
from app.jobs import queue
from app.jobs.kinds import DEPLOY_GATE_EXEMPT, EXPECTED_SECONDS
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalRsaIssuer, LocalStaff
from tests.unit.fakes import ScriptedConn

SETTINGS = Settings(database_url="postgresql://booking:x@db.test/aa", environment="test")

GOOD_CLAIMS: dict[str, Any] = {
    "repository": "AdventureAsia365-Ecosys/AA-Booking",
    "repository_owner": "AdventureAsia365-Ecosys",
    "repository_id": "1418188499",
    "repository_owner_id": "276539096",
    "ref": "refs/heads/main",
    "environment": "dev",
    "event_name": "push",
    "job_workflow_ref": "AdventureAsia365-Ecosys/AA-Booking/.github/workflows/deploy-dev.yml@refs/heads/main",
    "workflow_ref": "AdventureAsia365-Ecosys/AA-Booking/.github/workflows/deploy-dev.yml@refs/heads/main",
    "sub": "repo:AdventureAsia365-Ecosys/AA-Booking:environment:dev",
    "run_id": "123456789",
    "sha": "0123abc",
    "actor": "nghiep",
}


class LocalGithub:
    """Stands in for token.actions.githubusercontent.com: a local RSA key, its JWKS and OIDC tokens signed with it."""

    def __init__(self, kid: str = "gh1") -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetches = 0
        self.down = False

    async def fetch(self) -> dict[str, Any]:
        self.fetches += 1
        if self.down:
            raise OSError("GitHub JWKS down")
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def token(
        self,
        *,
        aud: Any = DEPLOY_GATE_AUDIENCE,
        iss: str = GITHUB_OIDC_ISSUER,
        exp_in: int = 300,
        drop: tuple[str, ...] = (),
        key: Any = None,
        algorithm: str = "RS256",
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {**GOOD_CLAIMS, "aud": aud, "iss": iss, "iat": now, "nbf": now, "exp": now + exp_in}
        claims.update(overrides)
        for name in drop:
            claims.pop(name, None)
        return jwt.encode(claims, key or self.private_key, algorithm=algorithm, headers={"kid": self.kid})

    def verifier(self) -> GithubDeployVerifier:
        return GithubDeployVerifier(JwksCache(self.fetch))


# ── verifier ─────────────────────────────────────────────────────────────────


def test_constants_match_the_plan() -> None:
    assert GITHUB_OIDC_ISSUER == "https://token.actions.githubusercontent.com"
    assert GITHUB_JWKS_URL == "https://token.actions.githubusercontent.com/.well-known/jwks"
    assert DEPLOY_GATE_AUDIENCE == "aa-booking-deploy-gate"
    assert (ci.DEPLOY_REPOSITORY_ID, ci.DEPLOY_REPOSITORY_OWNER_ID) == ("1418188499", "276539096")


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
async def test_valid_token_yields_the_caller(event: str) -> None:
    gh = LocalGithub()
    caller = await gh.verifier().verify(gh.token(event_name=event))
    assert caller.event_name == event
    assert (caller.run_id, caller.sha, caller.actor) == ("123456789", "0123abc", "nghiep")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"repository": "AdventureAsia365-Ecosys/AA-CIS-App"},
        {"repository": "someone/AA-Booking"},
        {"repository_id": "999"},
        {"repository_id": 1418188499},
        {"drop": ("repository_id",)},
        {"repository_owner_id": "999"},
        {"repository_owner_id": 276539096},
        {"drop": ("repository_owner_id",)},
        {"ref": "refs/heads/feature"},
        {"ref": "refs/pull/7/merge"},
        {"environment": "prod"},
        {"drop": ("environment",)},
        {"event_name": "pull_request"},
        {"event_name": "pull_request_target"},
        {"event_name": ["push"]},
        {"job_workflow_ref": "AdventureAsia365-Ecosys/AA-Booking/.github/workflows/ci.yml@refs/heads/main"},
        {"job_workflow_ref": "AdventureAsia365-Ecosys/AA-Booking/.github/workflows/deploy-dev.yml@refs/heads/main-x"},
        {"drop": ("job_workflow_ref",)},
        {"aud": "sts.amazonaws.com"},
        {"aud": "booking"},
        {"iss": "https://evil.test"},
        {"exp_in": -120},
        {"drop": ("exp",)},
        {"drop": ("iat",)},
        {"drop": ("aud",)},
    ],
    ids=[
        "other-repo",
        "fork-repo",
        "re-registered-repo-id",
        "repo-id-not-string",
        "no-repo-id",
        "re-registered-owner-id",
        "owner-id-not-string",
        "no-owner-id",
        "branch",
        "pull-ref",
        "env-prod",
        "no-env",
        "pull-request",
        "pull-request-target",
        "event-list",
        "other-workflow",
        "workflow-prefix-branch",
        "no-workflow-ref",
        "aud-sts",
        "aud-staff",
        "iss",
        "expired",
        "no-exp",
        "no-iat",
        "no-aud",
    ],
)
async def test_rejected_tokens(kwargs: dict[str, Any]) -> None:
    gh = LocalGithub()
    with pytest.raises(InvalidToken):
        await gh.verifier().verify(gh.token(**kwargs))


@pytest.mark.asyncio
async def test_token_signed_by_another_key_is_rejected() -> None:
    gh = LocalGithub()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        await gh.verifier().verify(gh.token(key=other))


@pytest.mark.asyncio
async def test_hs256_token_is_rejected_before_any_key_lookup() -> None:
    gh = LocalGithub()
    with pytest.raises(InvalidToken, match="algorithm"):
        await gh.verifier().verify(gh.token(key="a-shared-secret-of-sufficient-length-1234", algorithm="HS256"))
    assert gh.fetches == 0


@pytest.mark.asyncio
async def test_staff_jwt_is_not_a_deploy_token() -> None:
    """A staff-shaped token from another key (aud=booking) never opens the gate, even with GitHub-shaped claims."""
    gh = LocalGithub()
    with pytest.raises(InvalidToken):
        await gh.verifier().verify(LocalRsaIssuer(kid=gh.kid).token(**GOOD_CLAIMS))


@pytest.mark.asyncio
async def test_jwks_down_is_auth_unavailable() -> None:
    gh = LocalGithub()
    gh.down = True
    with pytest.raises(AuthUnavailable):
        await gh.verifier().verify(gh.token())


def test_default_verifier_targets_github() -> None:
    verifier = GithubDeployVerifier.default()
    assert isinstance(verifier, GithubDeployVerifier)
    assert verifier._issuer == GITHUB_OIDC_ISSUER


# ── active_count ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_count_aggregates_by_status_and_kind() -> None:
    conn = ScriptedConn().on(
        "FROM booking.job",
        [
            {"status": "queued", "kind": "bk_notify_email", "n": 2},
            {"status": "running", "kind": "bk_notify_email", "n": 1},
            {"status": "queued", "kind": "bk_inquiry_process", "n": 3},
        ],
    )
    counts = await queue.active_count(conn, frozenset({"bk_inquiry_pull", "bk_a"}))
    assert counts == {
        "active": 6,
        "by_status": {"queued": 5, "running": 1},
        "by_kind": {"bk_inquiry_process": 3, "bk_notify_email": 3},
    }
    (sql, args), *_ = conn.calls
    assert "status IN ('queued', 'running')" in sql
    assert "NOT (kind = ANY($1::text[]))" in sql
    assert "GROUP BY status, kind" in sql
    assert args == (["bk_a", "bk_inquiry_pull"],)


@pytest.mark.asyncio
async def test_active_count_empty_queue() -> None:
    assert await queue.active_count(ScriptedConn()) == {"active": 0, "by_status": {}, "by_kind": {}}


def test_exempt_kinds_are_real_kinds() -> None:
    assert DEPLOY_GATE_EXEMPT == frozenset({"bk_inquiry_pull"})
    assert DEPLOY_GATE_EXEMPT <= EXPECTED_SECONDS.keys()


# ── GET /booking/ops/deploy-gate ─────────────────────────────────────────────


class FakeDatabase(Database):
    def __init__(self, conn: ScriptedConn, configured: bool = True) -> None:
        super().__init__(SETTINGS)
        self.conn = conn
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    async def pool(self):  # type: ignore[override]
        return self.conn


def client(conn: ScriptedConn, gh: LocalGithub, configured: bool = True) -> TestClient:
    app = create_app(SETTINGS, FakeDatabase(conn, configured), LocalStaff().verifier(), gh.verifier())
    return TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_gate_safe_when_only_exempt_kinds_are_active() -> None:
    gh = LocalGithub()
    conn = ScriptedConn()
    with client(conn, gh) as c:
        r = c.get("/booking/ops/deploy-gate", headers=auth(gh.token()))
    assert r.status_code == 200
    assert r.json() == {
        "active": 0,
        "by_status": {},
        "by_kind": {},
        "safe_to_deploy": True,
        "exempt_kinds": ["bk_inquiry_pull"],
    }
    assert conn.sql("FROM booking.job") == [(["bk_inquiry_pull"],)]


def test_gate_not_safe_with_active_jobs() -> None:
    gh = LocalGithub()
    conn = ScriptedConn().on("FROM booking.job", [{"status": "queued", "kind": "bk_x6_probe", "n": 1}])
    with client(conn, gh) as c:
        r = c.get("/booking/ops/deploy-gate", headers=auth(gh.token(event_name="workflow_dispatch")))
    assert r.status_code == 200
    body = r.json()
    assert body["safe_to_deploy"] is False
    assert body["active"] == 1
    assert body["by_status"] == {"queued": 1}
    assert body["by_kind"] == {"bk_x6_probe": 1}


def test_gate_without_token_is_401_and_never_reads_the_db() -> None:
    conn = ScriptedConn()
    with client(conn, LocalGithub()) as c:
        r = c.get("/booking/ops/deploy-gate")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert conn.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [{"ref": "refs/heads/dev"}, {"aud": "booking"}, {"exp_in": -120}, {"repository": "x/y"}, {"repository_id": "1"}],
)
def test_gate_with_wrong_token_is_401(kwargs: dict[str, Any]) -> None:
    gh = LocalGithub()
    conn = ScriptedConn()
    with client(conn, gh) as c:
        r = c.get("/booking/ops/deploy-gate", headers=auth(gh.token(**kwargs)))
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid_token"}
    assert conn.calls == []


def test_gate_with_staff_token_is_401() -> None:
    gh = LocalGithub()
    with client(ScriptedConn(), gh) as c:
        r = c.get("/booking/ops/deploy-gate", headers=auth(LocalStaff().token()))
    assert r.status_code == 401


def test_gate_when_github_jwks_is_down_is_503() -> None:
    gh = LocalGithub()
    gh.down = True
    conn = ScriptedConn()
    with client(conn, gh) as c:
        r = c.get("/booking/ops/deploy-gate", headers=auth(gh.token()))
    assert r.status_code == 503
    assert r.json() == {"detail": "auth_unavailable"}
    assert conn.calls == []


def test_gate_without_database_is_503() -> None:
    gh = LocalGithub()
    with client(ScriptedConn(), gh, configured=False) as c:
        r = c.get("/booking/ops/deploy-gate", headers=auth(gh.token()))
    assert r.status_code == 503
    assert r.json() == {"detail": "database_not_configured"}


def test_gate_is_not_a_staff_route() -> None:
    """The deploy token is the only credential: no booking.staff_users lookup or provisioning happens."""
    gh = LocalGithub()
    conn = ScriptedConn()
    with client(conn, gh) as c:
        c.get("/booking/ops/deploy-gate", headers=auth(gh.token()))
    assert not conn.sql("staff_users")


def test_module_constants_are_used_by_the_app() -> None:
    assert create_app(SETTINGS).state.deploy_verifier is not None
    assert ci.DEPLOY_WORKFLOW_REF.endswith("/.github/workflows/deploy-dev.yml@refs/heads/main")
