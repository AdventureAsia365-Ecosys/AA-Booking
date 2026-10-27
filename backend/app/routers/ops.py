"""Operations endpoints for machine callers (AA-751 B3). Not staff routes: no staff JWT, no ``staff_users`` row.

- ``GET /booking/ops/deploy-gate``: may a deploy roll the Booking task now? Read by the ``preflight`` job of
  ``.github/workflows/deploy-dev.yml``. Authenticated **only** by the deploy workflow's GitHub OIDC token
  (``app.auth.ci``): 401 for any other token, 503 when GitHub's JWKS cannot be loaded.
  Since AA-993 G5 (design D1.4) ``safe_to_deploy`` is false only while a row of a kind in
  :data:`app.jobs.kinds.DEPLOY_BLOCKING_KINDS` is ``running`` (``blocking_running`` per kind, reasons in
  ``blocked_by``). Queued / running rows of every other kind no longer block (the worker releases a running job on
  shutdown and the new task re-claims it); they stay in ``active`` / ``by_status`` / ``by_kind`` as information,
  kinds in :data:`app.jobs.kinds.DEPLOY_GATE_EXEMPT` not counted there. Booking does not read CIS rerun waves
  (waves only drive ``shared.job``; design Q4).
  The body is declared as :class:`DeployGate` (AA-899) so the breaking-change check sees a removed field.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.ci import DeployCaller, deploy_caller
from app.auth.staff import get_pool
from app.jobs import queue
from app.jobs.kinds import DEPLOY_BLOCKING_KINDS, DEPLOY_GATE_EXEMPT

router = APIRouter(prefix="/booking/ops", tags=["ops"])

Caller = Annotated[DeployCaller, Depends(deploy_caller)]


class DeployGate(BaseModel):
    """Read by ``.github/scripts/booking-deploy-preflight.sh``. ``by_status`` / ``by_kind`` count active jobs
    (information); ``blocking_running`` counts the running rows of the deploy-blocking kinds, which decide
    ``safe_to_deploy``."""

    active: int
    by_status: dict[str, int]
    by_kind: dict[str, int]
    safe_to_deploy: bool
    exempt_kinds: list[str]
    blocking_kinds: list[str]
    blocking_running: dict[str, int]
    blocked_by: list[str]


@router.get("/deploy-gate", response_model=DeployGate)
async def deploy_gate(request: Request, _: Caller) -> dict[str, Any]:
    # The pool is taken after the token is verified, so an unauthenticated call never touches the database.
    pool = await get_pool(request)
    exempt = sorted(DEPLOY_GATE_EXEMPT)
    blocking = sorted(DEPLOY_BLOCKING_KINDS)
    counts = await queue.active_count(pool, exempt)
    running = await queue.running_count(pool, blocking)
    blocked_by = [f"{n} {kind} job(s) running (deploy-blocking kind)" for kind, n in running.items()]
    return {
        **counts,
        "exempt_kinds": exempt,
        "blocking_kinds": blocking,
        "blocking_running": running,
        "blocked_by": blocked_by,
        "safe_to_deploy": not blocked_by,
    }
