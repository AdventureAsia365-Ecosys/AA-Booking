"""The one machine caller: the Booking Deploy Dev workflow, authenticated by its GitHub Actions OIDC token (AA-751 B3).

``GET /booking/ops/deploy-gate`` (``app.routers.ops``) is not a staff route. The ``preflight`` job of
``.github/workflows/deploy-dev.yml`` mints a GitHub OIDC token with audience :data:`DEPLOY_GATE_AUDIENCE` and sends it
as ``Authorization: Bearer``. Booking verifies it with the same RS256 JWKS cache as the staff JWT (``app.auth.tokens``)
against GitHub's key set, then requires every claim below. No shared secret, no Terraform, no variable to set: the
values are code constants, so a change of repository, branch, environment or workflow file is a code change here.

Any failed check is HTTP 401; GitHub's JWKS unreachable with no cached key set is HTTP 503 (the gate fails closed and
the workflow blocks the deploy unless overridden).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import AuthUnavailable, InvalidToken, JwksCache, decode_rs256, http_fetcher

log = logging.getLogger(__name__)

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URL = GITHUB_OIDC_ISSUER + "/.well-known/jwks"
DEPLOY_GATE_AUDIENCE = "aa-booking-deploy-gate"

DEPLOY_REPOSITORY = "AdventureAsia365-Ecosys/AA-Booking"
# Immutable numeric ids (GitHub sends them as strings). Names are reusable: after a rename, transfer or deletion
# someone else could register the old org/repo name and mint a token whose string claims all match, so the ids
# are checked as well (`gh api repos/AdventureAsia365-Ecosys/AA-Booking --jq '.id,.owner.id'`).
DEPLOY_REPOSITORY_ID = "1418188499"
DEPLOY_REPOSITORY_OWNER_ID = "276539096"
DEPLOY_REF = "refs/heads/main"
DEPLOY_ENVIRONMENT = "dev"
DEPLOY_EVENTS = frozenset({"push", "workflow_dispatch"})
DEPLOY_WORKFLOW_REF = f"{DEPLOY_REPOSITORY}/.github/workflows/deploy-dev.yml@{DEPLOY_REF}"

REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud"]

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="GithubDeployOIDC",
    description="GitHub Actions OIDC token (aud=aa-booking-deploy-gate)",
)


@dataclass(frozen=True)
class DeployCaller:
    """Who asked: enough to find the workflow run in the logs."""

    run_id: str | None
    sha: str | None
    actor: str | None
    event_name: str


def _expect(name: str, ok: bool) -> None:
    if not ok:
        raise InvalidToken(f"claim {name} not allowed")


class GithubDeployVerifier:
    def __init__(self, jwks: JwksCache, *, issuer: str = GITHUB_OIDC_ISSUER) -> None:
        self._jwks = jwks
        self._issuer = issuer

    @classmethod
    def default(cls) -> GithubDeployVerifier:
        """Against GitHub's live JWKS (fetched on the first gate request, not at start-up)."""
        return cls(JwksCache(http_fetcher(GITHUB_JWKS_URL)))

    async def verify(self, token: str) -> DeployCaller:
        claims = await decode_rs256(
            self._jwks, token, audience=DEPLOY_GATE_AUDIENCE, issuer=self._issuer, required=REQUIRED_CLAIMS
        )
        _expect("repository", claims.get("repository") == DEPLOY_REPOSITORY)
        _expect("repository_id", claims.get("repository_id") == DEPLOY_REPOSITORY_ID)
        _expect("repository_owner_id", claims.get("repository_owner_id") == DEPLOY_REPOSITORY_OWNER_ID)
        _expect("ref", claims.get("ref") == DEPLOY_REF)
        _expect("environment", claims.get("environment") == DEPLOY_ENVIRONMENT)
        event = claims.get("event_name")
        _expect("event_name", isinstance(event, str) and event in DEPLOY_EVENTS)
        # Plan §3: "starts with" the deploy-dev.yml@refs/heads/main ref. The claim is exactly that string for a
        # workflow that runs as itself, so equality is used: a prefix would also accept "...@refs/heads/main-x".
        _expect("job_workflow_ref", claims.get("job_workflow_ref") == DEPLOY_WORKFLOW_REF)
        return DeployCaller(
            run_id=_text(claims.get("run_id")),
            sha=_text(claims.get("sha")),
            actor=_text(claims.get("actor")),
            event_name=event,
        )


def _text(value: Any) -> str | None:
    return value[:128] if isinstance(value, str) and value else None


async def deploy_caller(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> DeployCaller:
    """FastAPI dependency: the deploy workflow's OIDC token, or 401 / 503."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing_bearer_token", headers={"WWW-Authenticate": "Bearer"})
    try:
        caller = await request.app.state.deploy_verifier.verify(credentials.credentials)
    except InvalidToken as exc:
        log.warning("deploy_gate_rejected reason=%s", exc)
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"}) from exc
    except AuthUnavailable as exc:
        log.warning("deploy_gate_auth_unavailable (GitHub JWKS)")
        raise HTTPException(status_code=503, detail="auth_unavailable") from exc
    log.info(
        "deploy_gate_caller run_id=%s sha=%s actor=%s event=%s",
        caller.run_id,
        caller.sha,
        caller.actor,
        caller.event_name,
    )
    return caller
