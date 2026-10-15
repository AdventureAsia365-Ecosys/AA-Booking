"""Read-only lookups in the CIS catalog read model (CIS migration 225 grants role ``booking`` SELECT on the views).

Today: the place picker of the supplier form (``GET /booking/catalog/destinations``). The catalog pages of wave 2
(UI spec §6.3: destination profiles, countries, trip plans) extend this router.
"""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from app import catalog
from app.auth.staff import Staff, current_staff, get_pool

router = APIRouter(prefix="/booking", tags=["catalog"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]


@router.get("/catalog/destinations")
async def search_destinations(
    _: Me,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=120, description="part of the place name")] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2, description="ISO-2 country code")] = None,
    limit: Annotated[int, Query(ge=1, le=catalog.MAX_SEARCH)] = 20,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await catalog.search_destinations(conn, q=q, country=country, limit=limit)
