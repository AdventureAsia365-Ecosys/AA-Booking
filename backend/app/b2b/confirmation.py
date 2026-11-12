"""The booking confirmation of a "Book with AA" booking (AA-1037 R4 extras G2; contract booking.b2b.v1 1.3).

One document per confirmed B2B booking (a confirmed hold and its trip case), for the agency
(``GET /booking/b2b/v1/bookings/{booking_id}/confirmation``, scope ``read``, its own bookings only) and for staff
(``GET /booking/b2b-desk/bookings/{ref}/confirmation``, ``b2b_desk.read``):

- the booking reference (the trip case code), the agency's proposal ref and version, the status (``confirmed``, or
  ``cancelled`` once the agency or AA cancelled it), the trip dates and the most travellers of a line by age band;
  the end date covers each tour's days (AA-1088: ``app.b2b.plan_span``, the catalog ``duration_days``), so a case
  stored before AA-1088 with end = start still prints the whole trip;
- every line as sold: the title staff read on the case (the CIS tour title, "hotel · room type" or the service
  title, AA-1013), the service date, nights, pax counts by age band, the room set-up, the line status and the
  supplier confirmation refs of the confirmed supplier bookings that cover it (AA-682);
- the cancellation policy locked on the booking (its tiers: what a cancel costs, by days before departure);
- the issuer: the legal entity of the default billing entity (bk-041), else nothing.

**No traveller names** (none are ever in Booking for a B2B booking: pax are counts) and **no prices**: the agency
sells at its own price, and the document may be passed on to its client.

JSON (the contract ``BookingConfirmation``) or ``format=html``: one self-contained, printable HTML file (inline CSS,
no script, no external link; every value escaped), downloaded as an attachment. Booking has no PDF renderer: the page
prints to PDF from any browser ("Print / Save as PDF"), which keeps a PDF library and its system packages out of the
API image.
"""

from __future__ import annotations

import html
import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from fastapi.responses import HTMLResponse

from app.b2b import cancellation, plan_span

BOOKING_SQL = """
SELECT h.id::text AS hold_id, h.quote_id::text AS quote_id, h.proposal_ref, h.proposal_version, h.confirmed_at,
       h.cancellation_policy_id::text AS cancellation_policy_id, c.id::text AS trip_case_id, c.code, c.status,
       c.start_date, c.end_date, p.agency_name
FROM booking.b2b_holds h
JOIN booking.trip_cases c ON c.id = h.trip_case_id
JOIN booking.b2b_partners p ON p.id = h.partner_id
WHERE h.status = 'confirmed' AND ($2::uuid IS NULL OR h.partner_id = $2::uuid)
"""
BY_ID = BOOKING_SQL + " AND h.trip_case_id = $1::uuid"
BY_CODE = BOOKING_SQL + " AND c.code = $1"
# AA-1075 (bk-14): the travellers of a line are what was sold less its partial cancellations.
LINES_SQL = """
SELECT ql.line_no, ql.line_ref, ql.component_id, ql.component_kind, ql.service_date, ql.nights,
       ql.pax_adult - coalesce(r.adults, 0) AS pax_adult, ql.pax_child - coalesce(r.children, 0) AS pax_child,
       ql.pax_infant - coalesce(r.infants, 0) AS pax_infant, ql.rooms::text AS rooms, hl.status AS line_status,
       it.title
FROM booking.b2b_quote_lines ql
JOIN booking.b2b_hold_lines hl ON hl.hold_id = $2::uuid AND hl.line_ref = ql.line_ref
LEFT JOIN booking.trip_case_versions v ON v.trip_case_id = $3::uuid AND v.version_number = 1
LEFT JOIN booking.trip_case_version_itinerary it ON it.trip_case_version_id = v.id AND it.position = ql.line_no
LEFT JOIN (
    SELECT line_ref, sum(removed_adults) AS adults, sum(removed_children) AS children,
           sum(removed_infants) AS infants
    FROM booking.b2b_line_reductions WHERE hold_id = $2::uuid GROUP BY line_ref
) r ON r.line_ref = ql.line_ref
WHERE ql.quote_id = $1::uuid
ORDER BY ql.line_no
"""
# Confirmed supplier bookings of the case and the agency line each covers: by the B2B line ref it was opened for, else
# by its version item (the B2B confirm writes one item per quote line, position = line number).
SUPPLIER_REFS_SQL = """
SELECT coalesce(sb.b2b_line_ref, ql.line_ref) AS line_ref, sb.confirmation_ref
FROM booking.supplier_bookings sb
LEFT JOIN booking.trip_case_version_itinerary it ON it.id = sb.version_item_id
LEFT JOIN booking.b2b_quote_lines ql ON ql.quote_id = $2::uuid AND ql.line_no = it.position
WHERE sb.trip_case_id = $1::uuid AND sb.status = 'confirmed' AND sb.confirmation_ref IS NOT NULL
ORDER BY sb.confirmed_at, sb.code
"""
CANCELLED_SQL = "SELECT cancelled_at FROM booking.b2b_cancellations WHERE hold_id = $1::uuid"
ISSUER_SQL = "SELECT legal_entity FROM booking.billing_entity WHERE is_default AND active LIMIT 1"

LINE_STATUSES = frozenset({"confirmed", "on_request", "declined", "cancelled"})
OCCUPANCIES = frozenset({"single", "double", "twin", "triple", "family"})
KIND_LABEL = {
    "trip_plan": "Tour",
    "hotel_room": "Hotel room",
    "transfer": "Transfer",
    "guide": "Guide",
    "activity": "Activity",
    "entrance": "Entrance",
    "meal": "Meal",
}
STATUS_LABEL = {
    "confirmed": "Confirmed",
    "on_request": "On request",
    "declined": "Not available",
    "cancelled": "Cancelled",
}


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _rooms(raw: Any) -> list[dict[str, Any]]:
    """The room set-up of a line (occupancy x count); the AA room type id is left out."""
    items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    out = []
    for r in items if isinstance(items, list) else []:
        if isinstance(r, dict) and r.get("occupancy") in OCCUPANCIES:
            out.append({"occupancy": r["occupancy"], "count": max(int(r.get("count") or 1), 1)})
    return out


def _line_status(raw: str | None) -> str:
    # A line still held "available" on a confirmed booking was confirmed with it (the confirm moves it at once).
    if raw == "available":
        return "confirmed"
    return raw if raw in LINE_STATUSES else "on_request"


def _kind(raw: str | None) -> str:
    return raw if raw in KIND_LABEL else "activity"


async def load(conn: asyncpg.Connection, ref: str, *, partner_id: str | None, now: datetime) -> dict[str, Any] | None:
    """The confirmation of one confirmed B2B booking (``ref`` = the booking id, or for staff also the trip case
    code), or None when there is none (another partner's booking reads as none). ``partner_id`` None = staff."""
    ident = _uuid(ref)
    if ident is not None:
        booking = await conn.fetchrow(BY_ID, ident, partner_id)
    elif partner_id is None:
        booking = await conn.fetchrow(BY_CODE, ref, None)
    else:
        booking = None
    if booking is None:
        return None
    lines = await conn.fetch(LINES_SQL, booking["quote_id"], booking["hold_id"], booking["trip_case_id"])
    refs: dict[str, list[str]] = {}
    for row in await conn.fetch(SUPPLIER_REFS_SQL, booking["trip_case_id"], booking["quote_id"]):
        if row["line_ref"] and row["confirmation_ref"] not in refs.setdefault(row["line_ref"], []):
            refs[row["line_ref"]].append(row["confirmation_ref"])
    cancelled_at = await conn.fetchval(CANCELLED_SQL, booking["hold_id"])
    policy = await cancellation.policy_of_booking(
        conn,
        policy_id=booking["cancellation_policy_id"],
        quote_id=booking["quote_id"],
        confirmed_at=booking["confirmed_at"],
    )
    issuer = await conn.fetchval(ISSUER_SQL)
    end_date = await _trip_end(conn, booking, lines)
    out_lines = [
        {
            "line_ref": r["line_ref"],
            "title": " ".join(str(r["title"] or KIND_LABEL[_kind(r["component_kind"])]).split())[:500],
            "kind": _kind(r["component_kind"]),
            "service_date": r["service_date"],
            "nights": r["nights"],
            "pax": {"adult": int(r["pax_adult"]), "child": int(r["pax_child"]), "infant": int(r["pax_infant"] or 0)},
            "rooms": _rooms(r["rooms"]),
            "status": _line_status(r["line_status"]),
            "supplier_confirmations": refs.get(r["line_ref"], []),
        }
        for r in lines
    ]
    biggest = max(
        (line["pax"] for line in out_lines),
        key=lambda p: (p["adult"] + p["child"], p["adult"], p["infant"]),
        default={"adult": 0, "child": 0, "infant": 0},
    )
    return {
        "booking_id": booking["trip_case_id"],
        "trip_case_code": booking["code"],
        "hold_id": booking["hold_id"],
        "proposal_ref": booking["proposal_ref"],
        "proposal_version": int(booking["proposal_version"]),
        "status": "cancelled" if booking["status"] == "cancelled" or cancelled_at is not None else "confirmed",
        "issuer": issuer,
        "issued_at": now,
        "confirmed_at": booking["confirmed_at"],
        "cancelled_at": cancelled_at,
        "start_date": booking["start_date"],
        "end_date": end_date,
        "travellers": dict(biggest),
        "lines": out_lines,
        "cancellation_policy": cancellation.policy_out(policy, basis=cancellation.BASIS_LOCKED),
        # Staff and the HTML file name the agency; the JSON contract answers the agency itself, so it leaves it out.
        "_agency_name": booking["agency_name"],
    }


async def _trip_end(conn: asyncpg.Connection, booking: Any, lines: list[Any]) -> Any:
    """AA-1088 (M13): the trip's last day = the later of the stored case end and every line's ``service_date + its
    days - 1`` (a trip plan line = the tour's days). Cases confirmed before AA-1088 stored end = start for a tour
    priced on day 1 only; a staff-extended end is kept."""
    plan_ids = [p for p in (plan_span.plan_id_of(r.get("component_id")) for r in lines) if p is not None]
    days, _ = await plan_span.plan_days(conn, plan_ids)
    ends = [d for d in (booking["end_date"], plan_span.trip_end(lines, days)) if d is not None]
    return max(ends) if ends else None


def public(doc: dict[str, Any]) -> dict[str, Any]:
    """The contract body (without the staff-only fields, prefixed ``_``)."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


# ── HTML ─────────────────────────────────────────────────────────────────────


def tier_rows(tiers: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(days before departure, fee) rows from tiers sorted by ``min_days`` descending: 60+ days, 30-59 days, ..."""
    rows: list[tuple[str, str]] = []
    upper: int | None = None
    for tier in sorted(tiers, key=lambda t: int(t["min_days"]), reverse=True):
        low = int(tier["min_days"])
        if upper is None:
            span = f"{low}+ days"
        elif upper - 1 == low:
            span = f"{low} day{'s' if low != 1 else ''}"
        else:
            span = f"{low} to {upper - 1} days"
        fee = format(float(tier["fee_pct"]), "g")
        rows.append((span, f"{fee} %"))
        upper = low
    return rows


def _travellers(p: dict[str, int]) -> str:
    parts = [f"{p['adult']} adult{'s' if p['adult'] != 1 else ''}"]
    if p["child"]:
        parts.append(f"{p['child']} child{'ren' if p['child'] != 1 else ''}")
    if p["infant"]:
        parts.append(f"{p['infant']} infant{'s' if p['infant'] != 1 else ''}")
    return ", ".join(parts)


def _day(value: Any) -> str:
    return value.strftime("%d %b %Y") if value is not None else "-"


STYLE = """
body{font-family:Arial,Helvetica,sans-serif;color:#1f2328;margin:32px;font-size:13px;line-height:1.45}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:24px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.muted{color:#59636e}.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:bold}
.ok{background:#dafbe1;color:#116329}.off{background:#ffebe9;color:#a40e26}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #d1d9e0;padding:6px 8px;text-align:left;
vertical-align:top}th{background:#f6f8fa;font-size:12px}dl{display:grid;grid-template-columns:180px 1fr;gap:4px 12px;
margin:0}dt{color:#59636e}dd{margin:0}footer{margin-top:28px;font-size:11px;color:#59636e}
@media print{body{margin:12mm}.noprint{display:none}}
"""


def render_html(doc: dict[str, Any]) -> str:
    """The printable confirmation (one file, inline CSS, no script). Every value is escaped."""
    e = html.escape
    cancelled = doc["status"] == "cancelled"
    badge = '<span class="badge off">Cancelled</span>' if cancelled else '<span class="badge ok">Confirmed</span>'
    facts = [
        ("Booking reference", doc["trip_case_code"]),
        ("Agency", doc.get("_agency_name") or "-"),
        ("Agency reference", f"{doc['proposal_ref']} (version {doc['proposal_version']})"),
        ("Travel dates", f"{_day(doc['start_date'])} to {_day(doc['end_date'])}"),
        ("Travellers", _travellers(doc["travellers"])),
        ("Confirmed on", _day(doc["confirmed_at"])),
    ]
    if cancelled:
        facts.append(("Cancelled on", _day(doc["cancelled_at"])))
    fact_html = "".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in facts)
    line_rows = []
    for line in doc["lines"]:
        rooms = ", ".join(f"{r['count']} x {r['occupancy']}" for r in line["rooms"]) or "-"
        nights = f"{line['nights']} night{'s' if line['nights'] != 1 else ''}" if line["nights"] else "-"
        line_rows.append(
            "<tr>"
            f"<td>{e(line['line_ref'])}</td>"
            f"<td><strong>{e(line['title'])}</strong><br>"
            f'<span class="muted">{e(KIND_LABEL[line["kind"]])}</span></td>'
            f"<td>{e(_day(line['service_date']))}</td><td>{e(nights)}</td>"
            f"<td>{e(_travellers(line['pax']))}</td><td>{e(rooms)}</td>"
            f"<td>{e(STATUS_LABEL[line['status']])}</td>"
            f"<td>{e(', '.join(line['supplier_confirmations']) or '-')}</td>"
            "</tr>"
        )
    policy = doc["cancellation_policy"]
    if policy:
        tiers = "".join(f"<tr><td>{e(span)}</td><td>{e(fee)}</td></tr>" for span, fee in tier_rows(policy["tiers"]))
        policy_html = (
            '<p class="muted">The fee is a share of the booking\'s AA price, by the days left before departure on the '
            "day the agency cancels.</p>"
            f"<table><thead><tr><th>Cancelled before departure</th><th>Cancellation fee</th></tr></thead>"
            f"<tbody>{tiers}</tbody></table>"
        )
    else:
        policy_html = '<p class="muted">No cancellation policy is recorded for this booking: ask AA.</p>'
    issuer = doc.get("issuer")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Booking confirmation {e(doc['trip_case_code'])}</title><style>{STYLE}</style></head><body>"
        f'<p class="muted">{e(issuer) if issuer else ""}</p>'
        f"<h1>Booking confirmation {e(doc['trip_case_code'])} {badge}</h1>"
        f'<p class="muted">Issued {e(doc["issued_at"].strftime("%d %b %Y %H:%M"))} UTC</p>'
        f"<h2>Booking</h2><dl>{fact_html}</dl>"
        "<h2>Services</h2><table><thead><tr><th>Line</th><th>Service</th><th>Date</th><th>Nights</th>"
        "<th>Travellers</th><th>Rooms</th><th>Status</th><th>Supplier confirmation</th></tr></thead>"
        f"<tbody>{''.join(line_rows)}</tbody></table>"
        f"<h2>Cancellation policy</h2>{policy_html}"
        "<footer><p>Traveller names are held by the agency and are not part of this confirmation. Prices are not "
        "shown. Lines on request are confirmed by AA separately.</p>"
        '<p class="noprint">To keep a PDF, print this page and choose "Save as PDF".</p></footer>'
        "</body></html>"
    )


def file_name(doc: dict[str, Any]) -> str:
    safe = "".join(ch for ch in doc["trip_case_code"] if ch.isalnum() or ch in "-_") or "booking"
    return f"booking-confirmation-{safe}.html"


def html_response(doc: dict[str, Any]) -> HTMLResponse:
    """The HTML file as a download (attachment), with a CSP that allows its inline style and nothing else."""
    return HTMLResponse(
        render_html(doc),
        headers={
            "Content-Disposition": f'attachment; filename="{file_name(doc)}"',
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "Cache-Control": "no-store",
        },
    )
