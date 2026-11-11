"""Service vouchers of a "Book with AA" booking for the agency (AA-1075 bk-12, bk-047; contract booking.b2b.v1 1.7).

One voucher per **confirmed supplier booking** of a confirmed B2B booking: what the agency's client shows the
supplier on the day. Generated from the stored data, never uploaded (B2B cases take no document, DM-6.8):

- the service (the supplier booking's description), the date and local time, the pick-up and drop-off points and the
  voucher note (``supplier_bookings`` bk-047 columns, kept on the Suppliers tab of the case);
- the supplier's name and booking phone (``suppliers.contact_phone``, a business line) and its confirmation ref;
- the travellers of the agency line it covers by age band (what was sold less partial cancellations), else the case;
- the AA operations contact (setting ``b2b_ops_contact``; no line when it is empty).

**No traveller names and no prices.** The agency's CRM prints its lead traveller's name on its own copy (CRM-side
merge). A cancelled booking has no valid voucher (the list is empty). JSON (the contract ``BookingVouchers``) or
``format=html``: one self-contained printable file, one voucher per page (inline CSS, no script, every value escaped,
CSP ``default-src 'none'``), downloaded as an attachment. Agency route ``GET /booking/b2b/v1/bookings/{id}/vouchers``
(scope ``read``, its own bookings), staff route ``GET /booking/b2b-desk/bookings/{ref}/vouchers`` (``b2b_desk.read``).
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

import asyncpg
from fastapi.responses import HTMLResponse

from app.b2b import confirmation

SETTING = "b2b_ops_contact"
VOUCHERS_SQL = """
SELECT sb.code, coalesce(sb.b2b_line_ref, ql.line_ref) AS line_ref, sb.item_description, sb.service_date,
       sb.service_time, sb.pickup_point, sb.dropoff_point, sb.voucher_note, sb.confirmation_ref, sb.pax,
       s.company_name AS supplier_name, s.contact_phone AS supplier_phone
FROM booking.supplier_bookings sb
JOIN booking.suppliers s ON s.id = sb.supplier_id
LEFT JOIN booking.trip_case_version_itinerary it ON it.id = sb.version_item_id
LEFT JOIN booking.b2b_quote_lines ql ON ql.quote_id = $2::uuid AND ql.line_no = it.position
WHERE sb.trip_case_id = $1::uuid AND sb.status = 'confirmed'
ORDER BY sb.service_date NULLS LAST, sb.service_time NULLS LAST, sb.code
"""


async def ops_contact(conn: asyncpg.Connection) -> dict[str, str] | None:
    """The ``b2b_ops_contact`` setting as ``{label, phone, email}`` (only the filled keys); None when empty."""
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", SETTING)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {k: str(data[k]).strip()[:200] for k in ("label", "phone", "email") if str(data.get(k) or "").strip()}
    return out or None


async def load(conn: asyncpg.Connection, ref: str, *, partner_id: str | None, now: datetime) -> dict[str, Any] | None:
    """The vouchers of one confirmed B2B booking (``ref`` = booking id, or for staff also the case code), or None."""
    doc = await confirmation.load(conn, ref, partner_id=partner_id, now=now)
    if doc is None:
        return None
    pax_by_line = {line["line_ref"]: line["pax"] for line in doc["lines"]}
    vouchers: list[dict[str, Any]] = []
    if doc["status"] == "confirmed":
        quote_id = await conn.fetchval(
            "SELECT quote_id::text FROM booking.b2b_holds WHERE id = $1::uuid", doc["hold_id"]
        )
        for r in await conn.fetch(VOUCHERS_SQL, doc["booking_id"], quote_id):
            vouchers.append(
                {
                    "voucher_no": r["code"],
                    "line_ref": r["line_ref"],
                    "service": " ".join(str(r["item_description"]).split())[:500],
                    "service_date": r["service_date"],
                    "service_time": r["service_time"].strftime("%H:%M") if r["service_time"] else None,
                    "pickup_point": r["pickup_point"],
                    "dropoff_point": r["dropoff_point"],
                    "supplier": {"name": r["supplier_name"], "phone": r["supplier_phone"]},
                    "confirmation_ref": r["confirmation_ref"],
                    "travellers": dict(pax_by_line.get(r["line_ref"]) or doc["travellers"]),
                    "note": r["voucher_note"],
                }
            )
    return {
        "booking_id": doc["booking_id"],
        "trip_case_code": doc["trip_case_code"],
        "proposal_ref": doc["proposal_ref"],
        "status": doc["status"],
        "issued_at": now,
        "start_date": doc["start_date"],
        "end_date": doc["end_date"],
        "ops_contact": await ops_contact(conn),
        "vouchers": vouchers,
        "_agency_name": doc.get("_agency_name"),
        "_issuer": doc.get("issuer"),
    }


def public(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


VOUCHER_STYLE = (
    confirmation.STYLE
    + """
.voucher{page-break-after:always;border:1px solid #d1d9e0;border-radius:8px;padding:20px;margin-bottom:24px}
.voucher:last-child{page-break-after:auto}.no{font-family:monospace;font-size:15px}
"""
)


def render_html(doc: dict[str, Any]) -> str:
    """One page per voucher (inline CSS, no script). Every value is escaped."""
    e = html.escape
    contact = doc["ops_contact"]
    contact_text = (
        " · ".join(v for v in (contact.get("label"), contact.get("phone"), contact.get("email")) if v)
        if contact
        else ""
    )
    pages = []
    for v in doc["vouchers"]:
        facts = [
            ("Service", v["service"]),
            ("Date", confirmation._day(v["service_date"])),
            ("Time", v["service_time"] or "-"),
            ("Pick-up", v["pickup_point"] or "-"),
            ("Drop-off", v["dropoff_point"] or "-"),
            ("Travellers", confirmation._travellers(v["travellers"])),
            ("Supplier", v["supplier"]["name"]),
            ("Supplier booking phone", v["supplier"]["phone"] or "-"),
            ("Supplier confirmation", v["confirmation_ref"] or "-"),
            ("Booking reference", doc["trip_case_code"]),
            ("Agency reference", doc["proposal_ref"]),
        ]
        if contact_text:
            facts.append(("AA operations (24 h)", contact_text))
        rows = "".join(f"<dt>{e(k)}</dt><dd>{e(str(val))}</dd>" for k, val in facts)
        note = f"<p><strong>Note:</strong> {e(v['note'])}</p>" if v["note"] else ""
        pages.append(
            '<section class="voucher">'
            f'<p class="muted">{e(doc.get("_issuer") or "")}</p>'
            f'<h1>Service voucher <span class="no">{e(v["voucher_no"])}</span></h1>'
            f"<dl>{rows}</dl>{note}"
            '<p class="muted">Lead traveller: ______________________ (filled in by the agency)</p>'
            "</section>"
        )
    body = (
        "".join(pages)
        if pages
        else '<p class="muted">No voucher: the booking is cancelled or no service is confirmed by a supplier yet.</p>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Vouchers {e(doc['trip_case_code'])}</title><style>{VOUCHER_STYLE}</style></head><body>"
        f"{body}"
        "<footer><p>Traveller names are held by the agency and are not part of this voucher. Prices are not shown."
        '</p><p class="noprint">To keep a PDF, print this page and choose "Save as PDF".</p></footer>'
        "</body></html>"
    )


def html_response(doc: dict[str, Any]) -> HTMLResponse:
    safe = "".join(ch for ch in doc["trip_case_code"] if ch.isalnum() or ch in "-_") or "booking"
    return HTMLResponse(
        render_html(doc),
        headers={
            "Content-Disposition": f'attachment; filename="vouchers-{safe}.html"',
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "Cache-Control": "no-store",
        },
    )
