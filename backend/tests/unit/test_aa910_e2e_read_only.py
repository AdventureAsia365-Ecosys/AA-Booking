"""AA-910 (root ADR 0006a A10a): the temporary WORKFORCE E2E user holds booking-admin so the signed-in smoke can read
every staff page, but its session never writes, on any client. Writes are 403 ``e2e_read_only`` before any DB access;
reads keep the admin permissions. Local RSA keys + fake JWKS, no network."""

from __future__ import annotations

from typing import Any

import pytest

from app.auth.identity_verifier import Principal
from app.auth.staff import E2E_TEST_STAFF_EMAIL, is_e2e_test_principal
from tests.staff_tokens import BOOKING_STAFF_CLIENT, BOOKING_STAFF_PREVIEW_CLIENT, LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import COG_KEY, IDENTITY_SELECT, auth, client, cognito_row

E2E = "e2e-booking@e2e-mail.lumiguides.it.com"


def e2e_token(cis: LocalCis, client_id: str = BOOKING_STAFF_CLIENT, email: str = E2E) -> str:
    return cis.workforce.token(["booking-admin"], email=email, client_id=client_id)


def e2e_row() -> dict[str, Any]:
    return {**cognito_row("admin"), "email": E2E, "name": "e2e-booking"}


def test_constant_matches_the_trigger_allow_map_address() -> None:
    assert E2E_TEST_STAFF_EMAIL == E2E


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/booking/staff/me/preferences", {"notify": {}}),
        ("PATCH", f"/booking/staff/{COG_KEY}", {"active": False}),
        ("POST", "/booking/trip-cases", {"title": "x"}),
        ("POST", "/booking/customers", {"name": "x"}),
        ("POST", "/booking/notifications/read", {"all": True}),
        ("POST", "/booking/pricing/rules", {}),
        # AA-996: the builder's live quote is a POST too: the Figma audit's 403 on it was this rule.
        ("POST", "/booking/pricing/quote-preview", {"channel": "b2c_tailor_made", "items": []}),
        ("POST", "/booking/retention/runs", {}),
        ("DELETE", "/booking/customers/c-1", None),
        ("DELETE", "/booking/rates/00000000-0000-4000-8000-000000000001", None),
    ],
)
@pytest.mark.parametrize("email", [E2E, "E2E-Booking@E2E-Mail.Lumiguides.IT.com", f" {E2E} "])
def test_e2e_writes_are_403_before_any_db_access(method: str, path: str, body: Any, email: str) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(e2e_token(cis, email=email)))
    assert r.status_code == 403
    assert r.json()["detail"] == "e2e_read_only"
    assert conn.calls == []


def test_e2e_on_the_preview_client_is_still_refused() -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with client(cis, conn) as c:
        r = c.post(
            "/booking/trip-cases",
            json={"title": "x"},
            headers=auth(e2e_token(cis, client_id=BOOKING_STAFF_PREVIEW_CLIENT)),
        )
    assert r.status_code == 403
    assert r.json()["detail"] in {"preview_read_only", "e2e_read_only"}
    assert conn.calls == []


def test_e2e_reads_keep_the_admin_permissions() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, e2e_row())
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(e2e_token(cis)))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert {"staff.admin", "audit.read"} <= set(body["permissions"])


def test_other_admin_writes_are_not_refused_by_the_e2e_rule() -> None:
    """Control: a real admin on booking-staff reaches the route (it then fails for its own reasons, not this 403)."""
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row("admin"))
    with client(cis, conn) as c:
        token = cis.workforce.token(["booking-admin"], client_id=BOOKING_STAFF_CLIENT)
        r = c.put("/booking/staff/me/preferences", json={"notify": {}}, headers=auth(token))
    assert r.status_code != 403 or r.json().get("detail") != "e2e_read_only"


def _principal(email: str | None) -> Principal:
    return Principal(
        iss="i", sub="s", client_id="c", groups=("booking-admin",), email=email, email_verified=None, username="s"
    )


@pytest.mark.parametrize("email", [E2E, E2E.upper(), f"  {E2E}"])
def test_is_e2e_test_principal_matches_the_address(email: str) -> None:
    assert is_e2e_test_principal(_principal(email))


@pytest.mark.parametrize(
    "email",
    [
        None,
        "",
        "e2e@e2e-mail.lumiguides.it.com",  # the CIS E2E user (no Booking group anyway)
        "e2e-booking2@e2e-mail.lumiguides.it.com",
        "e2e-booking@lumiguides.it.com",
        "thu@adventure.asia",
    ],
)
def test_is_e2e_test_principal_ignores_other_addresses(email: str | None) -> None:
    assert not is_e2e_test_principal(_principal(email))
