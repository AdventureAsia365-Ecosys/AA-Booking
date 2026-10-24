"""Operations endpoints for machine callers (AA-751 B3). Not staff routes: no staff JWT, no ``staff_users`` row.

- ``GET /booking/ops/deploy-gate``: what a deploy would interrupt right now, for the ``preflight`` job of
  ``.github/workflows/deploy-dev.yml``. Authenticated **only** by the deploy workflow's GitHub OIDC token
  (``app.auth.ci``): 401 for any other token, 503 when GitHub's JWKS cannot be loaded. Returns
  ``{active, by_status, by_kind, safe_to_deploy, exempt_kinds}``; ``safe_to_deploy`` is ``active == 0`` over
  ``booking.job`` rows ``queued`` / ``running``, kinds in :data:`app.jobs.kinds.DEPLOY_GATE_EXEMPT` not counted.
  The body is declared as :class:`DeployGate` (AA-899) so the breaking-change check sees a removed field.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.ci import DeployCaller, deploy_caller
from app.auth.staff import get_pool
from app.jobs import queue
from app.jobs.kinds import DEPLOY_GATE_EXEMPT

router = APIRouter(prefix="/booking/ops", tags=["ops"])

Caller = Annotated[DeployCaller, Depends(deploy_caller)]


class DeployGate(BaseModel):
    """Read by ``.github/scripts/booking-deploy-preflight.sh``. ``by_status`` / ``by_kind`` count active jobs."""

    active: int
    by_status: dict[str, int]
    by_kind: dict[str, int]
    safe_to_deploy: bool
    exempt_kinds: list[str]


@router.get("/deploy-gate", response_model=DeployGate)
async def deploy_gate(request: Request, _: Caller) -> dict[str, Any]:
    # The pool is taken after the token is verified, so an unauthenticated call never touches the database.
    pool = await get_pool(request)
    exempt = sorted(DEPLOY_GATE_EXEMPT)
    counts = await queue.active_count(pool, exempt)
    return {**counts, "safe_to_deploy": counts["active"] == 0, "exempt_kinds": exempt}
