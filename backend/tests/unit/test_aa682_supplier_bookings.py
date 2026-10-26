"""AA-682 S1 (bk-026) without a database: the pure rules of supplier bookings and operations documents, the settings of
the ops-docs bucket, the permission matrix, and the read-only caps (preview, E2E) on the new write routes. The flows on
a real PostgreSQL are in tests/integration/test_bk026_*.py."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS
from app.errors import Conflict, Invalid
from app.ops import files as ops_files
from app.ops import supplier_bookings as sb
from app.settings import Settings, SettingsError
from app.trip_cases import service as trip_cases
from tests.staff_tokens import BOOKING_STAFF_PREVIEW_CLIENT, LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa910_e2e_read_only import e2e_token
from tests.unit.test_aa911_preview_cap import auth, client

PDF = b"%PDF-1.4\n%%EOF\n"
BOOKING = "00000000-0000-4000-8000-000000000682"


def _item(**over: Any) -> dict[str, Any]:
    return {
        "id": "item-1",
        "position": 1,
        "title": "Two nights",
        "day_from": 2,
        "day_to": 4,
        "hotel_room_type_id": "room-1",
        "service_id": None,
        "supplier_id": "sup-1",
        "supply_label": "Delta Hotel · Deluxe",
        "net_amounts": [Decimal("300.00"), Decimal("100.50")],
        "net_currencies": ["USD"],
        "b2b_hold_id": None,
        "b2b_line_ref": None,
        "b2b_held_as": None,
        "b2b_line_status": None,
        "live_booking": None,
        "b2b_declined": False,
        **over,
    }


CASE = {"id": "case-1", "code": "TC-000001", "status": "confirmed", "channel": "b2c", "start_date": date(2027, 3, 1),
        "pax": 2}  # fmt: skip


# ── Generate rules ───────────────────────────────────────────────────────────


def test_from_item_copies_dates_nights_pax_and_summed_net() -> None:
    out = sb._from_item(CASE, _item())
    assert out["service_date"] == date(2027, 3, 2)  # day 2 of a trip starting 1 March
    assert out["nights"] == 2 and out["pax"] == 2  # B2C days 2-4: in day 2, out day 4, as the room was costed
    assert out["net_amount"] == Decimal("400.50") and out["net_currency"] == "USD"
    assert out["item_description"] == "Delta Hotel · Deluxe"
    assert out["b2b_hold_id"] is None


def test_room_nights_follow_the_pricing_rule_on_b2c_and_the_hold_rule_on_b2b() -> None:
    from app.supply import rates as supply_rates

    for day_from, day_to in ((1, 1), (1, 2), (2, 4), (3, 9)):
        item = _item(day_from=day_from, day_to=day_to)
        b2c = sb._from_item(CASE, item)["nights"]
        # B2C: the nights the net covers (versions default_cost uses the same quantity).
        assert b2c == supply_rates.quantity("room_night", day_from=day_from, day_to=day_to, pax=None)
        # B2B: holds._open_case writes day_to = day_from + nights - 1.
        assert sb._from_item({**CASE, "channel": "b2b"}, item)["nights"] == day_to - day_from + 1
    assert sb._from_item(CASE, _item(day_from=1, day_to=1))["nights"] == 1  # a same-day item is one night, not zero


def test_service_items_have_no_nights_and_unknown_start_has_no_date() -> None:
    out = sb._from_item({**CASE, "start_date": None, "pax": None},
                        _item(hotel_room_type_id=None, service_id="svc-1"))  # fmt: skip
    assert out["nights"] is None and out["service_date"] is None and out["pax"] is None


def test_mixed_cost_currencies_are_refused_and_no_costs_is_no_net() -> None:
    with pytest.raises(Conflict) as exc:
        sb._net(_item(net_currencies=["USD", "VND"]))
    assert exc.value.code == "mixed_cost_currencies"
    assert sb._net(_item(net_amounts=None, net_currencies=None)) == (None, None)


def test_b2b_links_only_on_request_lines() -> None:
    b2b = {**CASE, "channel": "b2b"}
    linked = sb._from_item(b2b, _item(b2b_hold_id="h-1", b2b_line_ref="room", b2b_held_as="on_request"))
    assert (linked["b2b_hold_id"], linked["b2b_line_ref"]) == ("h-1", "room")
    auto = sb._from_item(b2b, _item(b2b_hold_id="h-1", b2b_line_ref="room", b2b_held_as="available"))
    assert auto["b2b_hold_id"] is None
    assert sb._from_item(CASE, _item(b2b_hold_id="h-1", b2b_line_ref="room", b2b_held_as="on_request"))[
        "b2b_hold_id"
    ] is None  # fmt: skip


@pytest.mark.parametrize(
    ("case", "item", "reason"),
    [
        (CASE, _item(), None),
        (CASE, _item(live_booking="SB-000001"), "booked"),
        (CASE, _item(supplier_id=None), "no_supplier"),
        ({**CASE, "channel": "b2b"}, _item(b2b_line_status="declined"), "b2b_line_closed"),
        ({**CASE, "channel": "b2b"}, _item(b2b_line_status="cancelled"), "b2b_line_closed"),
        ({**CASE, "channel": "b2b"}, _item(b2b_declined=True), "b2b_line_closed"),
        ({**CASE, "channel": "b2b"}, _item(b2b_line_status="confirmed"), None),
        # A B2C item whose earlier booking was declined is replaced (critic B3).
        (CASE, _item(b2b_declined=False), None),
    ],
)
def test_skip_reasons(case: dict, item: dict, reason: str | None) -> None:
    assert sb._item_skip_reason(case, item) == reason


def test_closed_cases_take_no_booking() -> None:
    for status in ("new_inquiry", "proposal_sent", "cancelled", "completed"):
        with pytest.raises(Conflict, match="confirmed trip only"):
            sb._ensure_open({**CASE, "status": status})
    sb._ensure_open({**CASE, "status": "supplier_confirming"})


# ── Staff status moves (critic B1) ───────────────────────────────────────────


def test_post_sale_statuses_are_system_managed_except_cancel() -> None:
    assert trip_cases.SYSTEM_STATUSES == {
        "supplier_confirming",
        "invoice_sent",
        "payment_pending",
        "paid",
        "pre_departure",
        "on_trip",
        "completed",
    }
    assert "cancelled" not in trip_cases.SYSTEM_STATUSES
    assert set(trip_cases.STATUSES) - trip_cases.SYSTEM_STATUSES - trip_cases.APPROVAL_ONLY_STATUSES == {
        "new_inquiry",
        "discovery",
        "building",
        "proposal_sent",
        "cancelled",
    }


# ── Documents ────────────────────────────────────────────────────────────────


def test_sniff_reads_the_bytes() -> None:
    assert ops_files.sniff(PDF) == "application/pdf"
    assert ops_files.sniff(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (10).to_bytes(4, "big") * 2 + b"\x00" * 8
    ) == ("image/png")
    with pytest.raises(Invalid) as exc:
        ops_files.sniff(b"MZ\x90\x00 an executable")
    assert exc.value.code == "unsupported_file"


def test_decode_limits() -> None:
    assert ops_files.decode(base64.b64encode(PDF).decode()) == PDF
    for bad, code in (("***", "invalid_base64"), ("", "empty_file"), ("A" * (ops_files.MAX_BASE64_CHARS + 4),
                                                                         "file_too_large")):  # fmt: skip
        with pytest.raises(Invalid) as exc:
            ops_files.decode(bad)
        assert exc.value.code == code


def test_object_key_matches_the_bk026_check() -> None:
    assert ops_files.object_key("supplier_voucher", BOOKING, "application/pdf") == (
        f"ops-docs/supplier_voucher/{BOOKING}.pdf"
    )
    assert ops_files.object_key("supplier_confirmation", BOOKING, "image/webp").endswith(".webp")


def test_download_is_an_attachment_with_a_safe_name() -> None:
    assert ops_files._disposition('voucher "Mr X" ü.pdf') == "attachment; filename=\"voucher 'Mr X' _.pdf\""
    assert ops_files.clean_name("  a/b\\c  d.pdf ") == "a b c d.pdf"
    with pytest.raises(Invalid):
        ops_files.clean_name("   ")


def test_ops_docs_bucket_setting() -> None:
    assert Settings(database_url=None, ops_docs_bucket="aa-booking-dev-ops-docs-005097885195").ops_docs_bucket
    with pytest.raises(SettingsError, match="OPS_DOCS_BUCKET"):
        Settings(database_url=None, ops_docs_bucket="aa-booking-dev-supply-media-005097885195")
    with pytest.raises(SettingsError, match="OPS_DOCS_REGION"):
        Settings(database_url=None, ops_docs_region="nowhere")
    assert ops_files.store_from_settings(Settings(database_url=None)) is None


# ── Permissions and read-only sessions ───────────────────────────────────────


def test_permission_matrix() -> None:
    assert PERMISSIONS["ops.read"] == PERMISSIONS["ops.write"] == {"admin", "sales", "contracting"}
    assert "ops.read" in PREVIEW_PERMISSIONS and "ops.write" not in PREVIEW_PERMISSIONS


WRITES = [
    ("POST", "/booking/trip-cases/TC-000001/supplier-bookings/generate", None),
    ("POST", "/booking/trip-cases/TC-000001/supplier-bookings", {"supplier_id": BOOKING}),
    ("PATCH", f"/booking/supplier-bookings/{BOOKING}", {"notes": "x"}),
    ("POST", f"/booking/supplier-bookings/{BOOKING}/request", None),
    ("POST", f"/booking/supplier-bookings/{BOOKING}/confirm", {"confirmation_ref": "R"}),
    ("POST", f"/booking/supplier-bookings/{BOOKING}/cancel", {"reason": "x"}),
    ("POST", f"/booking/ops-files/{BOOKING}/remove", {"reason": "x"}),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_preview_and_e2e_sessions_never_write(method: str, path: str, body: Any) -> None:
    cis = LocalCis()
    preview = cis.workforce.token(["booking-admin"], client_id=BOOKING_STAFF_PREVIEW_CLIENT)
    for token, detail in ((preview, "preview_read_only"), (e2e_token(cis), "e2e_read_only")):
        conn = ScriptedConn()
        with client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(token))
        assert r.status_code == 403 and r.json()["detail"] == detail
        assert conn.calls == []


def test_settings_replace_keeps_bucket_rule() -> None:
    base = Settings(database_url=None)
    with pytest.raises(SettingsError):
        replace(base, ops_docs_bucket="public")
