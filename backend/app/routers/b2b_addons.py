"""``GET /booking/b2b/v1/add-ons`` (booking.b2b.v1 1.11, AA-1152): the AA add-ons an agency can quote, for the CRM's
"AA add-on" drawer and the agency's own "from" prices (``app.b2b.addon_list``).

The same machine-only chain as ``app.routers.b2b`` (an aa-m2m token with ``booking.b2b/read`` and the agency's
``X-AA-Tenant-Id``); its own file only to keep ``app.routers.b2b`` within the code health limits. Never a net, a
margin or a supplier identity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from app.auth.m2m import SCOPE_READ
from app.b2b import addon_list
from app.b2b.access import B2BContext, require_scope
from app.b2b.errors import PREFIX
from app.routers.b2b import Currency, Money, Strict

router = APIRouter(prefix=PREFIX, tags=["b2b"])

CanRead = Annotated[B2BContext, Depends(require_scope(SCOPE_READ))]
AddonKind = Literal["transfer", "guide", "activity", "entrance", "meal"]


class AgencyAddonPlace(Strict):
    destination_id: str = Field(min_length=1, max_length=64)
    role: Literal["operates", "pickup"]
    name: str | None = Field(default=None, max_length=300)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")


class AgencyAddon(Strict):
    component_id: str = Field(min_length=1, max_length=64)
    offer_id: str = Field(min_length=1, max_length=64)
    kind: AddonKind
    title: str = Field(min_length=1, max_length=300)
    unit: Literal["person", "group", "vehicle", "day"]
    duration_minutes: int | None = Field(default=None, ge=0)
    max_group_size: int | None = Field(default=None, ge=1)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    min_pax: int = Field(ge=1)
    cutoff_hours: int = Field(ge=0)
    operating_weekdays: list[int]
    start_times: list[str]
    inventory_kind: Literal["free_sale", "allotment", "on_request"]
    featured: bool
    first_date: date
    places: list[AgencyAddonPlace]
    b2b_price_per_person: Money | None
    rrp_per_person: Money | None


class AgencyAddonPage(Strict):
    currency: Currency
    priced_for_pax: int = Field(ge=1)
    date: date | None
    truncated: bool
    items: list[AgencyAddon] = Field(max_length=addon_list.MAX_ITEMS)


@router.get("/add-ons", response_model=AgencyAddonPage)
async def list_addons(
    ctx: CanRead,
    destination_id: Annotated[str | None, Query(pattern=r"^[0-9a-fA-F-]{36}$")] = None,
    on_date: Annotated[date | None, Query(alias="date")] = None,
    pax: Annotated[int, Query(ge=1, le=addon_list.PAX_MAX)] = 2,
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "USD",
) -> dict:
    """1.11 (AA-1152): the published add-ons (of one place, of one day, or all), each with its places and the calling
    agency's price per traveller for ``pax`` adults (``app.b2b.addon_list``)."""
    async with ctx.pool.acquire() as conn:
        return await addon_list.list_addons(
            conn,
            partner_id=ctx.partner_id,
            currency=currency,
            pax=pax,
            destination_id=destination_id,
            on_date=on_date,
            today=datetime.now(UTC).date(),
        )
