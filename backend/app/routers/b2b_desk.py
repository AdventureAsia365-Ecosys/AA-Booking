"""Staff B2B desk ``/booking/b2b-desk`` (bk-022, AA-975): on-request lines and departure incidents (``app.b2b.desk``).

Read ``b2b_desk.read``, write ``b2b_desk.write`` (admin, sales, contracting). Staff tokens only; the agencies see the
outcome as events on ``GET /booking/b2b/v1/events``.

- ``GET /summary``: open / overdue / due-soon on-request lines and open incidents (page header, dashboard).
- ``GET /on-request?state=open|answered|all``: lines of confirmed B2B bookings held on request, with the SLA badge.
- ``POST /on-request/{hold_id}/lines/{line_ref}/accept`` and ``…/decline`` (``decline_code``).
- ``GET /departures?from&to``: departures with seats, the agencies on them and the open incident.
- ``GET /departures/{id}/incidents``, ``POST /departures/{id}/incidents`` (``incident_code``) = "Flag incident on
  departure"; ``POST /incidents/{id}/clear``.
- ``POST /departure-checks/runs`` (``dry_run``) and ``GET /departure-checks/runs`` (AA-1073): queue now / list the
  runs of ``bk_departure_watch`` (guarantee at the minimum, at risk on the decision date) and ``bk_allotment_release``.
- ``GET /bookings/{ref}/confirmation`` (AA-1037 G2; ``ref`` = the trip case id or code): the booking confirmation of
  a confirmed B2B booking as the printable HTML file the agency gets (``format=json`` for the JSON body).

AA-1075 (bk-047):

- ``GET /agencies``, ``GET /entries``, ``POST /entries`` (``b2b_desk.book``, ``Idempotency-Key``): bookings staff
  enter for an agency (``app.b2b.desk_bookings``); ``POST /entries/{id}/approve`` / ``reject`` (``b2b_desk.approve``,
  never the person who entered it) for a price below the floor.
- ``POST /bookings/{ref}/cancel`` (``b2b_desk.book``, ``?dry_run``, ``Idempotency-Key``), ``GET
  /bookings/{ref}/cancellation``, ``POST /bookings/{ref}/cancellation/override`` (``b2b_desk.book``) and ``…/approve``
  / ``…/reject`` (``b2b_desk.approve``), ``POST /bookings/{ref}/lines/{line_ref}/reduce`` and ``GET
  /bookings/{ref}/reductions`` (``app.b2b.amendments``).
- ``GET /bookings/{ref}/vouchers`` (HTML by default, ``format=json``; ``app.b2b.vouchers``).
- ``GET /departures/{id}/manifest`` (``ops.read``; JSON or ``format=html``; ``app.b2b.manifest``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, current_staff, get_pool, require
from app.b2b import amendments, confirmation, departure_watch, desk, desk_bookings, manifest, quotes, vouchers
from app.b2b.components import LineAsk, RoomAsk
from app.errors import NotFound
from app.routers.b2b import QuoteLineRequest

router = APIRouter(prefix="/booking/b2b-desk", tags=["b2b"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("b2b_desk.read"))]
CanWrite = Annotated[Staff, Depends(require("b2b_desk.write"))]
CanBook = Annotated[Staff, Depends(require("b2b_desk.book"))]
CanApprove = Annotated[Staff, Depends(require("b2b_desk.approve"))]
CanOps = Annotated[Staff, Depends(require("ops.read"))]
BookingRef = Annotated[str, Path(min_length=1, max_length=64, description="trip case id or code")]
IdemKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]
LineRef = Annotated[str, Path(min_length=1, max_length=64)]


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decline_code: Literal["sold_out", "supplier_unavailable", "minimum_not_met", "other"]


class IncidentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_code: Literal["weather", "road", "health", "political", "supplier", "other"]


@router.get("/summary")
async def get_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.summary(conn)


@router.get("/on-request")
async def get_on_request(
    _: CanRead,
    pool: Pool,
    state: Literal["open", "answered", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=desk.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.list_on_request(conn, state=state, limit=limit, offset=offset)


@router.post("/on-request/{hold_id}/lines/{line_ref}/accept")
async def accept_line(hold_id: Annotated[uuid.UUID, Path()], line_ref: LineRef, staff: CanWrite, pool: Pool) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.answer_line(conn, staff, str(hold_id), line_ref, accept=True)


@router.post("/on-request/{hold_id}/lines/{line_ref}/decline")
async def decline_line(
    body: DeclineIn, hold_id: Annotated[uuid.UUID, Path()], line_ref: LineRef, staff: CanWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.answer_line(conn, staff, str(hold_id), line_ref, accept=False, decline_code=body.decline_code)


@router.get("/departures")
async def get_departures(
    _: CanRead,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.list_departures(conn, start=start, end=end)


class DepartureChecksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False


@router.post("/departure-checks/runs", status_code=202)
async def post_departure_checks(body: DepartureChecksIn, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    """AA-1073: queue the departure watch (guarantee / at risk) and the allotment release now."""
    async with pool.acquire() as conn, conn.transaction():
        return await departure_watch.enqueue_now(conn, staff, dry_run=body.dry_run)


@router.get("/departure-checks/runs")
async def get_departure_checks(
    _: CanRead, pool: Pool, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await departure_watch.recent_runs(conn, limit)


@router.get("/departures/{departure_id}/incidents")
async def get_incidents(departure_id: Annotated[uuid.UUID, Path()], _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.list_incidents(conn, str(departure_id))


@router.post("/departures/{departure_id}/incidents", status_code=201)
async def flag_incident(
    body: IncidentIn, departure_id: Annotated[uuid.UUID, Path()], staff: CanWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.flag_incident(conn, staff, str(departure_id), body.incident_code)


@router.post("/incidents/{incident_id}/clear")
async def clear_incident(incident_id: Annotated[uuid.UUID, Path()], staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.clear_incident(conn, staff, str(incident_id))


@router.get(
    "/bookings/{ref}/confirmation",
    responses={200: {"content": {"text/html": {"schema": {"type": "string"}}}}},
)
async def get_booking_confirmation(
    ref: Annotated[str, Path(min_length=1, max_length=64, description="trip case id or code")],
    _: CanRead,
    pool: Pool,
    format: Literal["json", "html"] = "html",
) -> Any:
    async with pool.acquire() as conn:
        doc = await confirmation.load(conn, ref, partner_id=None, now=datetime.now(UTC))
    if doc is None:
        raise NotFound("booking_not_found", "no confirmed B2B booking with this id or code")
    if format == "html":
        return confirmation.html_response(doc)
    return {**confirmation.public(doc), "agency_name": doc["_agency_name"]}


# ── AA-1075: staff-entered agency bookings ───────────────────────────────────


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_id: uuid.UUID
    agency_ref: str = Field(min_length=1, max_length=64)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    lines: list[QuoteLineRequest] = Field(min_length=1, max_length=50)
    override_total: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    override_reason: str | None = Field(default=None, max_length=500)


def _ask_lines(lines: list[QuoteLineRequest]) -> list[quotes.AskLine]:
    return [
        quotes.AskLine(
            ask=LineAsk(
                line_ref=line.line_ref,
                component_id=line.component_id,
                service_date=line.service_date,
                nights=line.nights,
                adult=line.pax.adult,
                child=line.pax.child,
                infant=line.pax.infant,
                rooms=tuple(RoomAsk(r.occupancy, r.count, r.room_type_id) for r in line.rooms),
            ),
            rooms=[r.model_dump() for r in line.rooms],
            departure_id=line.departure_id,
        )
        for line in lines
    ]


AGENCIES_SQL = """
SELECT id::text AS partner_id, tenant_id::text AS tenant_id, agency_name, billing_currency::text AS billing_currency,
       tier_code
FROM booking.b2b_partners WHERE status = 'active' ORDER BY agency_name
"""


async def _agency_picker(staff: Annotated[Staff, Depends(current_staff)]) -> Staff:
    """Who picks an agency: staff entering a booking (``b2b_desk.book``) or setting a tour's agency terms
    (``billing_config.write``, finance)."""
    if not (staff.can("b2b_desk.book") or staff.can("billing_config.write")):
        raise HTTPException(status_code=403, detail="forbidden")
    return staff


@router.get("/agencies")
async def get_agencies(_: Annotated[Staff, Depends(_agency_picker)], pool: Pool) -> dict[str, Any]:
    """The active agencies a booking can be entered for (the picker of "New agency booking" and of a tour's terms)."""
    async with pool.acquire() as conn:
        return {"items": [dict(r) for r in await conn.fetch(AGENCIES_SQL)]}


@router.get("/entries")
async def get_entries(
    staff: CanRead,
    pool: Pool,
    status: Literal["awaiting_approval", "booked", "rejected", "lapsed"] | None = None,
    limit: Annotated[int, Query(ge=1, le=desk_bookings.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk_bookings.list_bookings(conn, staff, status=status, limit=limit, offset=offset)


@router.post("/entries", status_code=201)
async def post_entry(body: EntryIn, staff: CanBook, pool: Pool, key: IdemKey) -> dict[str, Any]:
    return await desk_bookings.enter(
        pool,
        staff,
        partner_id=str(body.partner_id),
        agency_ref=body.agency_ref,
        currency=body.currency,
        lines=_ask_lines(body.lines),
        idempotency_key=key,
        override_total=body.override_total,
        override_reason=body.override_reason,
    )


@router.get("/entries/{entry_id}")
async def get_entry(entry_id: Annotated[uuid.UUID, Path()], staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk_bookings.get(conn, staff, str(entry_id))


@router.post("/entries/{entry_id}/approve")
async def approve_entry(entry_id: Annotated[uuid.UUID, Path()], staff: CanApprove, pool: Pool) -> dict[str, Any]:
    return await desk_bookings.approve(pool, staff, str(entry_id))


@router.post("/entries/{entry_id}/reject")
async def reject_entry(entry_id: Annotated[uuid.UUID, Path()], staff: CanApprove, pool: Pool) -> dict[str, Any]:
    return await desk_bookings.reject(pool, staff, str(entry_id))


# ── AA-1075: cancel, fee override, partial cancel for an agency ──────────────


class StaffCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: Literal["client_cancelled", "agency_cancelled", "departure_cancelled", "other"]
    fee_override_pct: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    waive_reason: str | None = Field(default=None, max_length=500)


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fee_override_pct: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    waive_reason: str = Field(min_length=1, max_length=500)


class OverrideApproveIn(BaseModel):
    """The lower fee % the approver saw; a different stored value (asked again in between) is refused (409)."""

    model_config = ConfigDict(extra="forbid")

    fee_override_pct: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class ReduceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adult: int = Field(default=0, ge=0, le=60)
    child: int = Field(default=0, ge=0, le=60)
    infant: int = Field(default=0, ge=0, le=20)
    reason_code: Literal["client_cancelled", "agency_cancelled", "other"]


@router.post("/bookings/{ref}/cancel")
async def cancel_for_agency(
    body: StaffCancelIn, ref: BookingRef, staff: CanBook, pool: Pool, key: IdemKey, dry_run: bool = False
) -> dict[str, Any]:
    return await amendments.staff_cancel(
        pool,
        staff,
        ref,
        reason_code=body.reason_code,
        dry_run=dry_run,
        idempotency_key=key,
        fee_override_pct=body.fee_override_pct,
        waive_reason=body.waive_reason,
    )


@router.get("/bookings/{ref}/cancellation")
async def get_cancellation(ref: BookingRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"cancellation": await amendments.get_cancellation(conn, staff, ref)}


@router.post("/bookings/{ref}/cancellation/override")
async def request_fee_override(body: OverrideIn, ref: BookingRef, staff: CanBook, pool: Pool) -> dict[str, Any]:
    return await amendments.request_override(
        pool, staff, ref, fee_override_pct=body.fee_override_pct, waive_reason=body.waive_reason
    )


@router.post("/bookings/{ref}/cancellation/override/approve")
async def approve_fee_override(
    body: OverrideApproveIn, ref: BookingRef, staff: CanApprove, pool: Pool
) -> dict[str, Any]:
    return await amendments.approve_override(pool, staff, ref, expected_fee_override_pct=body.fee_override_pct)


@router.post("/bookings/{ref}/cancellation/override/reject")
async def reject_fee_override(ref: BookingRef, staff: CanApprove, pool: Pool) -> dict[str, Any]:
    return await amendments.reject_override(pool, staff, ref)


@router.post("/bookings/{ref}/lines/{line_ref}/reduce")
async def reduce_for_agency(
    body: ReduceIn,
    ref: BookingRef,
    line_ref: LineRef,
    staff: CanBook,
    pool: Pool,
    key: IdemKey,
    dry_run: bool = False,
) -> dict[str, Any]:
    return await amendments.staff_reduce(
        pool,
        staff,
        ref,
        line_ref,
        adults=body.adult,
        children=body.child,
        infants=body.infant,
        reason_code=body.reason_code,
        dry_run=dry_run,
        idempotency_key=key,
    )


@router.get("/bookings/{ref}/reductions")
async def get_reductions(ref: BookingRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await amendments.list_reductions(conn, ref)}


# ── AA-1075: vouchers and the departure manifest ─────────────────────────────


@router.get("/bookings/{ref}/vouchers", responses={200: {"content": {"text/html": {"schema": {"type": "string"}}}}})
async def get_vouchers(
    ref: BookingRef,
    _: CanRead,
    pool: Pool,
    format: Literal["json", "html"] = "html",  # noqa: A002 — the query parameter's name
) -> Any:
    async with pool.acquire() as conn:
        doc = await vouchers.load(conn, ref, partner_id=None, now=datetime.now(UTC))
    if doc is None:
        raise NotFound("booking_not_found", "no confirmed B2B booking with this id or code")
    if format == "html":
        return vouchers.html_response(doc)
    return {**vouchers.public(doc), "agency_name": doc["_agency_name"]}


@router.get(
    "/departures/{departure_id}/manifest",
    responses={200: {"content": {"text/html": {"schema": {"type": "string"}}}}},
)
async def get_manifest(
    departure_id: Annotated[uuid.UUID, Path()],
    _: CanOps,
    pool: Pool,
    format: Literal["json", "html"] = "json",  # noqa: A002 — the query parameter's name
) -> Any:
    async with pool.acquire() as conn:
        doc = await manifest.load(conn, str(departure_id), now=datetime.now(UTC))
    if doc is None:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    if format == "html":
        return manifest.html_response(doc)
    return doc
