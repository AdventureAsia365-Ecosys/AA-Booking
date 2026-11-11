"""Departure operations manifest (AA-1075 bk-13 step 1; S275 study item bk-13).

``GET /booking/b2b-desk/departures/{id}/manifest`` (``ops.read``; JSON, or ``format=html`` for the printable page):
one departure, every confirmed agency booking on it (a confirmed hold with a live line on the departure, the case not
cancelled), per booking the agency, its reference, the trip case code, the travellers by age band (what was sold
less partial cancellations), the room set-up and the line status; then the supplier bookings of those cases and of
the departure itself (code, supplier, date, time, pick-up, status and the supplier's confirmation ref). Totals by
age band and rooms by type.

**Step 1 has no traveller identity at all**: Booking stores counts only (DM-6.8, root ADR 0019 d11: the agency's CRM
holds the travellers, AA is processor). Names, room sharing and nationality on the manifest (step 2, a rooming list
pushed by the CRM) wait for the data-standard amendment the S275 study names; nothing here reads or stores them.
"""

from __future__ import annotations

import html
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

import asyncpg
from fastapi.responses import HTMLResponse

from app.b2b import confirmation

DEPARTURE_SQL = """
SELECT d.id::text AS id, d.start_date, d.capacity, d.status, d.min_to_operate, p.source_id
FROM booking.departures d
JOIN booking.trip_plans p ON p.id = d.trip_plan_id
WHERE d.id = $1::uuid
"""
TITLE_SQL = "SELECT title::text AS title FROM catalog.v_tour WHERE source_id = $1"
BOOKINGS_SQL = """
SELECT c.id::text AS trip_case_id, c.code, c.status AS case_status, c.entered_via, pa.agency_name, h.proposal_ref,
       hl.line_ref, hl.status AS line_status, ql.rooms::text AS rooms,
       ql.pax_adult - coalesce(r.adults, 0) AS adults, ql.pax_child - coalesce(r.children, 0) AS children,
       ql.pax_infant - coalesce(r.infants, 0) AS infants
FROM booking.b2b_hold_lines hl
JOIN booking.b2b_holds h ON h.id = hl.hold_id AND h.status = 'confirmed'
JOIN booking.trip_cases c ON c.id = h.trip_case_id
JOIN booking.b2b_partners pa ON pa.id = h.partner_id
JOIN booking.b2b_quote_lines ql ON ql.quote_id = h.quote_id AND ql.line_ref = hl.line_ref
LEFT JOIN (
    SELECT hold_id, line_ref, sum(removed_adults) AS adults, sum(removed_children) AS children,
           sum(removed_infants) AS infants
    FROM booking.b2b_line_reductions GROUP BY hold_id, line_ref
) r ON r.hold_id = h.id AND r.line_ref = hl.line_ref
WHERE hl.departure_id = $1::uuid AND hl.status IN ('available', 'on_request', 'confirmed')
  AND c.status <> 'cancelled' AND c.deleted_at IS NULL
ORDER BY pa.agency_name, c.code, hl.line_ref
"""
SUPPLIERS_SQL = """
SELECT sb.code, sb.trip_case_id::text AS trip_case_id, sb.item_description, sb.service_date, sb.service_time,
       sb.pickup_point, sb.status, sb.confirmation_ref, s.company_name AS supplier_name
FROM booking.supplier_bookings sb
JOIN booking.suppliers s ON s.id = sb.supplier_id
WHERE (sb.trip_case_id = ANY($1::uuid[]) OR sb.departure_id = $2::uuid) AND sb.status <> 'declined'
ORDER BY sb.service_date NULLS LAST, sb.service_time NULLS LAST, sb.code
"""


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


async def load(conn: asyncpg.Connection, departure_id: str, *, now: datetime) -> dict[str, Any] | None:
    ident = _uuid(departure_id)
    dep = await conn.fetchrow(DEPARTURE_SQL, ident) if ident else None
    if dep is None:
        return None
    try:
        async with conn.transaction():  # a savepoint: a catalog read error never fails the manifest
            title = await conn.fetchval(TITLE_SQL, dep["source_id"])
    except (asyncpg.PostgresError, TimeoutError):
        title = None
    rows = await conn.fetch(BOOKINGS_SQL, ident)
    bookings = []
    rooms_total: Counter[str] = Counter()
    totals = {"adult": 0, "child": 0, "infant": 0}
    for r in rows:
        rooms = confirmation._rooms(r["rooms"])
        pax = {"adult": int(r["adults"]), "child": int(r["children"]), "infant": int(r["infants"])}
        for k, v in pax.items():
            totals[k] += v
        for room in rooms:
            rooms_total[room["occupancy"]] += room["count"]
        bookings.append(
            {
                "trip_case_id": r["trip_case_id"],
                "trip_case_code": r["code"],
                "agency": r["agency_name"],
                "agency_ref": r["proposal_ref"],
                "entered_via": r["entered_via"],
                "line_ref": r["line_ref"],
                "line_status": confirmation._line_status(r["line_status"]),
                "travellers": pax,
                "rooms": rooms,
            }
        )
    case_ids = sorted({b["trip_case_id"] for b in bookings})
    codes = {b["trip_case_id"]: b["trip_case_code"] for b in bookings}
    suppliers = [
        {
            "code": r["code"],
            "trip_case_code": codes.get(r["trip_case_id"]) if r["trip_case_id"] else None,
            "service": r["item_description"],
            "supplier": r["supplier_name"],
            "service_date": r["service_date"],
            "service_time": r["service_time"].strftime("%H:%M") if r["service_time"] else None,
            "pickup_point": r["pickup_point"],
            "status": r["status"],
            "confirmation_ref": r["confirmation_ref"],
        }
        for r in await conn.fetch(SUPPLIERS_SQL, case_ids, ident)
    ]
    return {
        "departure": {
            "id": dep["id"],
            "start_date": dep["start_date"],
            "status": dep["status"],
            "capacity": int(dep["capacity"]),
            "min_to_operate": int(dep["min_to_operate"]),
            "source_id": dep["source_id"],
            "title": title,
        },
        "generated_at": now,
        "totals": {**totals, "seats": totals["adult"] + totals["child"], "bookings": len({*case_ids})},
        "rooms": [{"occupancy": k, "count": v} for k, v in sorted(rooms_total.items())],
        "bookings": bookings,
        "supplier_bookings": suppliers,
    }


def _rooms_text(rooms: list[dict[str, Any]]) -> str:
    return ", ".join(f"{r['count']} x {r['occupancy']}" for r in rooms) or "-"


def render_html(doc: dict[str, Any]) -> str:
    e = html.escape
    dep = doc["departure"]
    t = doc["totals"]
    head = (
        f"{e(dep['title'] or dep['source_id'])} · {e(confirmation._day(dep['start_date']))} · {e(dep['status'])}"
        f" · {t['seats']} of {dep['capacity']} seats"
    )
    booking_rows = "".join(
        "<tr>"
        f"<td>{e(b['agency'])}</td><td>{e(b['agency_ref'])}</td><td>{e(b['trip_case_code'])}</td>"
        f"<td>{b['travellers']['adult']}</td><td>{b['travellers']['child']}</td><td>{b['travellers']['infant']}</td>"
        f"<td>{e(_rooms_text(b['rooms']))}</td>"
        f"<td>{e(confirmation.STATUS_LABEL[b['line_status']])}</td>"
        "</tr>"
        for b in doc["bookings"]
    )
    supplier_rows = "".join(
        "<tr>"
        f"<td>{e(s['code'])}</td><td>{e(s['trip_case_code'] or 'Departure')}</td><td>{e(s['service'])}</td>"
        f"<td>{e(s['supplier'])}</td><td>{e(confirmation._day(s['service_date']))}</td>"
        f"<td>{e(s['service_time'] or '-')}</td><td>{e(s['pickup_point'] or '-')}</td><td>{e(s['status'])}</td>"
        f"<td>{e(s['confirmation_ref'] or '-')}</td>"
        "</tr>"
        for s in doc["supplier_bookings"]
    )
    rooms = _rooms_text(doc["rooms"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Manifest {e(str(dep['start_date']))}</title><style>{confirmation.STYLE}</style></head><body>"
        f"<h1>Departure manifest</h1><p>{head}</p>"
        f'<p class="muted">Generated {e(doc["generated_at"].strftime("%d %b %Y %H:%M"))} UTC. Adults {t["adult"]},'
        f" children {t['child']}, infants {t['infant']}; rooms {e(rooms)}.</p>"
        "<h2>Bookings</h2><table><thead><tr><th>Agency</th><th>Agency ref</th><th>Booking</th><th>Adults</th>"
        "<th>Children</th><th>Infants</th><th>Rooms</th><th>Status</th></tr></thead>"
        f"<tbody>{booking_rows}</tbody></table>"
        "<h2>Supplier bookings</h2><table><thead><tr><th>Code</th><th>Booking</th><th>Service</th><th>Supplier</th>"
        "<th>Date</th><th>Time</th><th>Pick-up</th><th>Status</th><th>Confirmation</th></tr></thead>"
        f"<tbody>{supplier_rows}</tbody></table>"
        "<footer><p>Counts only: traveller names stay with the agencies.</p></footer></body></html>"
    )


def html_response(doc: dict[str, Any]) -> HTMLResponse:
    return HTMLResponse(
        render_html(doc),
        headers={
            "Content-Disposition": f'inline; filename="manifest-{doc["departure"]["start_date"]}.html"',
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "Cache-Control": "no-store",
        },
    )
