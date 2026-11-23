"""AA-1158 part 2 (review S289) — the choices of the supplier profile are admin-edited data
(``booking.supplier_profile_option``, bk-067), without a database: the supplier checks read the active codes (a code
the supplier already holds stays valid), only ``staff.admin`` adds or edits a choice, a code never changes, every
write is audited. The DB rules are in tests/integration/test_bk067_schema.py, the HTTP flow in
tests/integration/test_aa1158_supplier_bank_api.py. All codes and labels are invented."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.suppliers import profile, profile_options
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("it-1158p2-admin", None, "Admin", "admin", True, None)
CONTRACTING = Staff("it-1158p2-maker", None, "Maker", "contracting", True, None)
ACTIVE = [
    {"kind": "contact_role", "code": "accounts"},
    {"kind": "contact_role", "code": "night_desk"},
    {"kind": "terms_basis", "code": "deposit_balance"},
    {"kind": "terms_basis", "code": "after_service"},
    {"kind": "terms_method", "code": "bank_transfer"},
]


def conn_with(active: list[dict[str, str]] = ACTIVE) -> ScriptedConn:
    return ScriptedConn().on("FROM booking.supplier_profile_option WHERE active", active)


def _values(**body: Any) -> dict[str, Any]:
    return profile.normalize(dict(body))


@pytest.mark.asyncio
async def test_active_codes_pass_and_a_new_choice_is_usable_at_once() -> None:
    conn = conn_with()
    await profile_options.check_profile(
        conn,
        _values(
            role_contacts=[{"role": "night_desk", "phone": "+84 000 111"}],
            payment_terms={"basis": "after_service", "method": "bank_transfer", "due_days": 10},
        ),
    )
    assert len(conn.sql("WHERE active")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"role_contacts": [{"role": "concierge", "name": "Lan"}]}, "invalid_role_contact"),
        ({"payment_terms": {"basis": "on_arrival"}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "after_service", "method": "cheque"}}, "invalid_payment_terms"),
    ],
)
async def test_a_retired_or_unknown_code_is_refused(body: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as err:
        await profile_options.check_profile(conn_with(), _values(**body))
    assert err.value.code == code and "Profile choices" in err.value.message


@pytest.mark.asyncio
async def test_a_code_the_supplier_holds_stays_valid_after_it_is_retired() -> None:
    current = {
        "role_contacts": [{"role": "concierge", "name": "Lan"}],
        "payment_terms": {"basis": "on_arrival", "method": "cheque"},
    }
    body = _values(
        role_contacts=[{"role": "concierge", "name": "Lan Vo"}, {"role": "accounts", "email": "desk@inn.test"}],
        payment_terms={"basis": "on_arrival", "method": "cheque", "note": "cash desk"},
    )
    await profile_options.check_profile(conn_with(), body, current)
    with pytest.raises(Invalid):  # a create holds nothing yet
        await profile_options.check_profile(conn_with(), body)


@pytest.mark.asyncio
async def test_nothing_to_check_reads_nothing() -> None:
    conn = conn_with()
    await profile_options.check_profile(conn, _values(legal_entity="Inn Co", role_contacts=[], payment_terms=None))
    assert conn.calls == []


def test_the_deposit_rule_stays_tied_to_its_code() -> None:
    assert (
        _values(payment_terms={"basis": "deposit_balance", "deposit_percent": 30})["payment_terms"]["deposit_percent"]
        == "30"
    )
    with pytest.raises(Invalid):
        _values(payment_terms={"basis": "after_service", "deposit_percent": 30})


@pytest.mark.asyncio
async def test_list_groups_by_kind_with_retired_rows() -> None:
    rows = [
        {"kind": "contact_role", "code": "accounts", "active": True},
        {"kind": "contact_role", "code": "concierge", "active": False},
        {"kind": "terms_method", "code": "cash", "active": True},
    ]
    out = await profile_options.list_all(ScriptedConn().on("ORDER BY kind, position, code", rows))
    assert [r["code"] for r in out["contact_role"]] == ["accounts", "concierge"]
    assert out["terms_basis"] == [] and [r["code"] for r in out["terms_method"]] == ["cash"]


def _option(**over: Any) -> dict[str, Any]:
    return {
        "id": "5c000000-0000-4000-8000-0000000011b2",
        "kind": "contact_role",
        "code": "night_desk",
        "label_en": "Night desk",
        "label_vi": "Trực đêm",
        "position": 70,
        "active": True,
        "updated_at": None,
        "updated_by": ADMIN.cis_user_id,
        **over,
    }


@pytest.mark.asyncio
async def test_only_an_administrator_edits_the_choices() -> None:
    conn = ScriptedConn()
    with pytest.raises(Forbidden):
        await profile_options.create(conn, CONTRACTING, {"kind": "contact_role", "code": "x1", "label_en": "X",
                                                         "label_vi": "X"})  # fmt: skip
    with pytest.raises(Forbidden):
        await profile_options.update(conn, CONTRACTING, "contact_role", "accounts", {"active": False})
    assert conn.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"kind": "vendor_tier", "code": "gold", "label_en": "Gold", "label_vi": "Vàng"}, "unknown_kind"),
        ({"kind": "contact_role", "code": "Night Desk", "label_en": "N", "label_vi": "N"}, "invalid_code"),
        ({"kind": "contact_role", "code": "night_desk", "label_en": " ", "label_vi": "N"}, "label_en_required"),
        ({"kind": "contact_role", "code": "night_desk", "label_en": "N", "label_vi": "x" * 81}, "label_vi_too_long"),
        ({"kind": "contact_role", "code": "night_desk", "label_en": "N", "label_vi": "N", "position": 0},
         "invalid_position"),
    ],
)  # fmt: skip
async def test_create_refuses_a_bad_choice(body: dict[str, Any], code: str) -> None:
    conn = ScriptedConn()
    with pytest.raises(Invalid) as err:
        await profile_options.create(conn, ADMIN, body)
    assert err.value.code == code and conn.sql("INSERT INTO") == []


@pytest.mark.asyncio
async def test_create_appends_and_audits() -> None:
    conn = ScriptedConn().on("coalesce(max(position), 0) + 10", 70).on("INSERT INTO booking.supplier_profile_option",
                                                                       _option())  # fmt: skip
    out = await profile_options.create(
        conn, ADMIN, {"kind": "contact_role", "code": "night_desk", "label_en": " Night  desk ", "label_vi": "Trực đêm"}
    )
    assert out["code"] == "night_desk"
    args = conn.sql("INSERT INTO booking.supplier_profile_option")[0]
    assert args == ("contact_role", "night_desk", "Night desk", "Trực đêm", 70, ADMIN.cis_user_id)
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "supplier_profile_option.created" and audit[4] == "supplier_profile_option"
    assert json.loads(audit[7])["code"] == "night_desk"


@pytest.mark.asyncio
async def test_create_of_an_existing_code_is_a_conflict() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.supplier_profile_option", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict) as err:
        await profile_options.create(conn, ADMIN, {"kind": "contact_role", "code": "accounts", "label_en": "A",
                                                   "label_vi": "A", "position": 5})  # fmt: skip
    assert err.value.code == "option_exists"


@pytest.mark.asyncio
async def test_update_retires_a_choice_and_audits_the_change_only() -> None:
    conn = (
        ScriptedConn()
        .on("WHERE kind = $1 AND code = $2 FOR UPDATE", _option())
        .on("UPDATE booking.supplier_profile_option", _option(active=False))
    )
    out = await profile_options.update(conn, ADMIN, "contact_role", "night_desk", {"active": False, "position": 70})
    assert out["active"] is False
    update = conn.sql("UPDATE booking.supplier_profile_option")[0]
    assert update == ("contact_role", "night_desk", False, ADMIN.cis_user_id)
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert json.loads(audit[6]) == {"active": True} and json.loads(audit[7]) == {"active": False}


@pytest.mark.asyncio
async def test_update_without_a_change_writes_nothing_and_unknown_is_404() -> None:
    conn = ScriptedConn().on("FOR UPDATE", _option())
    assert (await profile_options.update(conn, ADMIN, "contact_role", "night_desk", {"label_en": "Night desk"}))[
        "label_en"
    ] == "Night desk"
    assert conn.sql("UPDATE booking") == [] and conn.sql("audit_logs") == []
    with pytest.raises(NotFound):
        await profile_options.update(ScriptedConn(), ADMIN, "contact_role", "nope", {"active": True})
    with pytest.raises(Invalid):
        await profile_options.update(conn, ADMIN, "contact_role", "night_desk", {"active": "no"})
