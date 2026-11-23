"""AA-1157 (root ADR 0019 d5 as amended): the owner's read-only "view as a lower role", without a database.

- Inside a view every non-GET route that depends on ``current_staff`` (walked from ``app.routes``, so routes added later
  are covered) answers 403 ``view_as_read_only`` before any database access: the pool raises if touched.
- ``/booking/staff/me`` in a view: role = target, permissions = target ∩ viewer, a ``view_as`` block, read-only; the
  role editor says ``can_edit`` false (the owner markers read False during a view).
- The start: an owner's writes one ``staff.view_as_started`` row with the target role's FULL capability list; a
  non-owner (403 ``owner_only``), a wider target (403 ``view_as_not_downward``), an unknown or archived role (404) are
  refused before any row.
- An expired session answers 409 with exactly one ``expired`` end row; another viewer's session 403; the end is
  idempotent; ``GET /booking/view-as`` is the view-as route, not ``/booking/staff/{cis_user_id}``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.auth import custom_roles, view_as
from app.auth.staff import current_staff, role_permissions
from tests.staff_tokens import LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import COG_KEY, IDENTITY_SELECT, auth, client, cognito_row

SESSION = "b1157000-0000-4000-8000-000000000001"
GRANT = "FROM shared.v_staff_booking_grant"
ROLE = "FROM booking.staff_role r LEFT JOIN"
START = "ORDER BY created_at LIMIT 1"
ENDED = "'staff.view_as_ended' LIMIT 1"
OPEN = "s.actor_ref = $1"
AUDIT = "INSERT INTO booking.audit_logs"
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    custom_roles.forget()
    view_as._read_cache.clear()
    yield
    custom_roles.forget()


def grant(owner: bool = True, business: bool = False) -> dict[str, Any]:
    return {"booking_role_code": None, "is_owner": owner, "is_business_owner": business}


def start_row(*, actor: str = COG_KEY, target: str = "sales", expires: datetime | None = None) -> dict[str, Any]:
    after = {
        "session_id": SESSION,
        "target_role": target,
        "expires_at": (expires or (datetime.now(UTC) + timedelta(minutes=20))).isoformat(),
    }
    return {"actor_ref": actor, "after": json.dumps(after)}


def open_row(session_id: str, *, minutes: int = 10, after: Any = "default") -> dict[str, Any]:
    """A row of OPEN_SQL: a session without an end row, expiring ``minutes`` from now (negative: already past)."""
    if after == "default":
        after = json.dumps({"expires_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()})
    return {"session_id": session_id, "after": after}


def viewer_conn(role: str = "admin", *, owner: bool = True, row: Any = None, ended: bool = False) -> ScriptedConn:
    return (
        ScriptedConn()
        .on(IDENTITY_SELECT, cognito_row(role))
        .on(GRANT, grant(owner))
        .on(START, start_row() if row is None else row)
        .on(ENDED, 1 if ended else None)
        .on("SELECT now()", datetime.now(UTC))
    )


def admin_token(cis: LocalCis) -> str:
    return cis.workforce.token(["booking-admin", "cis-admin"], amr=["otp"])


def view_headers(cis: LocalCis) -> dict[str, str]:
    return {**auth(admin_token(cis)), view_as.HEADER: SESSION}


# ── reads in a view ──────────────────────────────────────────────────────────
def test_me_in_a_view_is_the_target_role_narrowed_and_read_only() -> None:
    cis = LocalCis()
    with client(cis, viewer_conn()) as c:
        r = c.get("/booking/staff/me", headers=view_headers(cis))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "sales" and body["read_only"] is True
    assert set(body["permissions"]) == role_permissions("sales") & role_permissions("admin")
    assert body["view_as"]["viewer_role"] == "admin" and body["view_as"]["target_label"] == "Sales"
    assert "session_id" not in body["view_as"] and SESSION not in r.text   # only the BFF holds the session id
    assert "staff.admin" not in body["permissions"]


def test_me_outside_a_view_is_unchanged() -> None:
    cis = LocalCis()
    with client(cis, viewer_conn()) as c:
        body = c.get("/booking/staff/me", headers=auth(admin_token(cis))).json()
    assert body["role"] == "admin" and body["view_as"] is None and body["read_only"] is False


def test_the_view_route_is_not_the_staff_detail_route() -> None:
    cis = LocalCis()
    with client(cis, viewer_conn()) as c:
        inside = c.get("/booking/view-as", headers=view_headers(cis))
        outside = c.get("/booking/view-as", headers=auth(admin_token(cis)))
    assert inside.json()["active"] is True and inside.json()["target_role"] == "sales"
    assert "session_id" not in inside.json()
    assert outside.json() == {"active": False}


def test_the_role_editor_is_not_an_owner_during_a_view() -> None:
    cis = LocalCis()
    conn = viewer_conn().on("FROM booking.staff_role r LEFT JOIN booking.staff_role_capability c", [])
    with client(cis, conn) as c:
        inside = c.get("/booking/staff-roles", headers=view_headers(cis))
        outside = c.get("/booking/staff-roles", headers=auth(admin_token(cis)))
    # in the view the role is sales (no staff.admin, not an owner): the editor is refused outright
    assert inside.status_code == 403
    assert outside.status_code == 200 and outside.json()["can_edit"] is True and outside.json()["can_view_as"] is True


@pytest.mark.asyncio
async def test_markers_read_false_while_viewing() -> None:
    conn = ScriptedConn().on(GRANT, grant(owner=True, business=True))
    assert await custom_roles.is_owner(conn, "sub") and await custom_roles.is_business_owner(conn, "sub")
    reset = custom_roles.VIEWING.set(True)
    try:
        assert not await custom_roles.is_owner(conn, "sub")
        assert not await custom_roles.is_business_owner(conn, "sub")
    finally:
        custom_roles.VIEWING.reset(reset)

    class Viewing:
        view_as = object()
        cognito_sub = "sub"

    assert await custom_roles.markers(conn, Viewing()) == (False, False)


# ── writes in a view: refused before any database access ─────────────────────
class Untouchable(ScriptedConn):
    def _answer(self, sql: str, args: tuple[Any, ...]) -> Any:
        raise AssertionError(f"database touched in a view-as write: {sql[:80]}")


def _depends_on_current_staff(dependant: Any) -> bool:
    return any(d.call is current_staff or _depends_on_current_staff(d) for d in dependant.dependencies)


def _staff_writes(app: Any) -> list[tuple[str, str]]:
    out = set()
    for route in view_as.api_routes(app.routes):
        if _depends_on_current_staff(route.dependant):
            out |= {(m, route.path) for m in route.methods - {"GET", "HEAD", "OPTIONS"}}
    return sorted(out)


def _concrete(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "b1157000-0000-4000-8000-0000000000ff", path)


def test_every_staff_write_is_refused_in_a_view_before_the_database() -> None:
    cis = LocalCis()
    with client(cis, Untouchable()) as c:
        writes = _staff_writes(c.app)
        assert len(writes) > 100 and ("POST", "/booking/view-as") in writes
        headers = view_headers(cis)
        for method, path in writes:
            r = c.request(method, _concrete(path), headers=headers, json={})
            assert (r.status_code, r.json().get("detail")) == (403, view_as.READ_ONLY), (method, path, r.text)


def test_the_header_on_a_bridge_token_is_refused() -> None:
    cis = LocalCis()
    with client(cis, viewer_conn()) as c:
        r = c.get("/booking/staff/me", headers={**auth(cis.token()), view_as.HEADER: SESSION})
    assert r.status_code in (401, 403)  # the bridge is retired (401); were it on, the header is 403


# ── session checks ───────────────────────────────────────────────────────────
def test_an_expired_session_ends_once_and_answers_409() -> None:
    cis = LocalCis()
    conn = viewer_conn(row=start_row(expires=datetime.now(UTC) - timedelta(seconds=1)))
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=view_headers(cis))
    assert (r.status_code, r.json()["detail"]) == (409, view_as.ENDED_DETAIL)
    ended = [a for a in conn.sql(AUDIT) if a[3] == view_as.ENDED]
    assert len(ended) == 1 and ended[0][0] == "system" and json.loads(ended[0][7])["end_reason"] == "expired"
    assert conn.sql("pg_advisory_xact_lock")


def test_an_ended_session_answers_409_without_a_new_row() -> None:
    cis = LocalCis()
    conn = viewer_conn(ended=True)
    with client(cis, conn) as c:
        assert c.get("/booking/staff/me", headers=view_headers(cis)).status_code == 409
    assert not [a for a in conn.sql(AUDIT) if a[3] == view_as.ENDED]


def test_another_viewers_session_is_refused() -> None:
    cis = LocalCis()
    conn = viewer_conn(row=start_row(actor="cog:someone-else"))
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=view_headers(cis))
    assert (r.status_code, r.json()["detail"]) == (403, view_as.NOT_OWN) and not conn.sql(AUDIT)


def test_an_erased_start_row_fails_closed() -> None:
    cis = LocalCis()
    conn = viewer_conn(row={"actor_ref": COG_KEY, "after": json.dumps({"erased": True})})
    with client(cis, conn) as c:
        assert c.get("/booking/staff/me", headers=view_headers(cis)).status_code == 409
    assert [json.loads(a[7])["end_reason"] for a in conn.sql(AUDIT) if a[3] == view_as.ENDED] == ["invalid"]


def test_a_lost_owner_ends_the_view() -> None:
    cis = LocalCis()
    conn = viewer_conn(owner=False)
    with client(cis, conn) as c:
        assert c.get("/booking/staff/me", headers=view_headers(cis)).status_code == 409
    assert [json.loads(a[7])["end_reason"] for a in conn.sql(AUDIT) if a[3] == view_as.ENDED] == ["owner_lost"]


# ── start / end ──────────────────────────────────────────────────────────────
def _start(conn: ScriptedConn, body: dict[str, Any], token: str | None = None, cis: LocalCis | None = None) -> Any:
    cis = cis or LocalCis()
    with client(cis, conn) as c:
        return c.post("/booking/view-as", headers=auth(token or admin_token(cis)), json=body)


def test_an_owner_start_logs_the_full_capability_list() -> None:
    conn = viewer_conn().on(OPEN, [open_row("b1157000-0000-4000-8000-0000000000aa")]).on(ENDED, None)
    r = _start(conn, {"role": "contracting", "minutes": 15, "reason": "Check the product nav"})
    assert r.status_code == 201, r.text
    assert r.json()["target_role"] == "contracting" and r.json()["read_only"] is True
    rows = conn.sql(AUDIT)
    started = [a for a in rows if a[3] == view_as.STARTED]
    assert len(started) == 1
    after = json.loads(started[0][7])
    assert after["capabilities"] == sorted(role_permissions("contracting"))
    assert after["viewer_role"] == "admin" and after["target_role"] == "contracting" and after["minutes"] == 15
    assert after["ended_sessions"] == ["b1157000-0000-4000-8000-0000000000aa"]
    superseded = [json.loads(a[7]) for a in rows if a[3] == view_as.ENDED]
    assert superseded == [{"session_id": "b1157000-0000-4000-8000-0000000000aa", "end_reason": "superseded"}]


def test_a_start_ends_an_expired_open_session_as_expired() -> None:
    old = "b1157000-0000-4000-8000-0000000000bb"
    conn = viewer_conn().on(OPEN, [open_row(old, minutes=-5)]).on(ENDED, None)
    assert _start(conn, {"role": "sales"}).status_code == 201
    ended = [json.loads(a[7]) for a in conn.sql(AUDIT) if a[3] == view_as.ENDED]
    assert ended == [{"session_id": old, "end_reason": "expired"}]


def test_the_owner_next_view_read_closes_an_expired_session_once() -> None:
    """A view left to time out (the cookie is gone): GET /booking/view-as outside a view (the banner's check at 0
    minutes, the Roles page) writes its end row as the system, reason expired; an open one is left alone."""
    cis = LocalCis()
    old, live = "b1157000-0000-4000-8000-0000000000cc", "b1157000-0000-4000-8000-0000000000dd"
    conn = viewer_conn().on(OPEN, [open_row(old, minutes=-1), open_row(live, minutes=10)]).on(ENDED, None)
    with client(cis, conn) as c:
        r = c.get("/booking/view-as", headers=auth(admin_token(cis)))
    assert r.json() == {"active": False}
    ended = [(a[0], json.loads(a[7])) for a in conn.sql(AUDIT) if a[3] == view_as.ENDED]
    assert ended == [("system", {"session_id": old, "end_reason": "expired"})]
    assert conn.sql(view_as.LOCK_SQL)


def test_the_view_read_with_nothing_open_writes_nothing() -> None:
    cis = LocalCis()
    conn = viewer_conn()
    with client(cis, conn) as c:
        assert c.get("/booking/view-as", headers=auth(admin_token(cis))).json() == {"active": False}
    assert not conn.sql(AUDIT) and not conn.sql(view_as.LOCK_SQL)


@pytest.mark.asyncio
async def test_sign_out_ends_open_views_and_an_expired_one_as_expired() -> None:
    old, live = "b1157000-0000-4000-8000-0000000000ee", "b1157000-0000-4000-8000-0000000000ff"
    conn = ScriptedConn().on(OPEN, [open_row(old, minutes=-1), open_row(live, minutes=10)]).on(ENDED, None)
    conn.on("SELECT now()", datetime.now(UTC))

    class Viewer:
        cis_user_id = COG_KEY

    assert await view_as.end_all(conn, Viewer()) == [old, live]
    reasons = [json.loads(a[7])["end_reason"] for a in conn.sql(AUDIT) if a[3] == view_as.ENDED]
    assert reasons == ["expired", "signed_out"]


def test_a_custom_role_start() -> None:
    caps = sorted(custom_roles.SEED_CAPABILITIES)
    role = {
        "code": custom_roles.SEED_CODE,
        "label": custom_roles.SEED_LABEL,
        "based_on": "contracting",
        "archived": False,
        "capabilities": caps,
    }
    conn = viewer_conn().on(ROLE, role)
    r = _start(conn, {"role": custom_roles.SEED_CODE})
    assert r.status_code == 201, r.text
    (started,) = [a for a in conn.sql(AUDIT) if a[3] == view_as.STARTED]
    after = json.loads(started[7])
    assert after["capabilities"] == caps and after["target_label"] == custom_roles.SEED_LABEL
    assert after["minutes"] == view_as.DEFAULT_MINUTES


def test_a_non_owner_cannot_start() -> None:
    conn = viewer_conn(owner=False)
    r = _start(conn, {"role": "sales"})
    assert (r.status_code, r.json()["detail"]) == (403, view_as.OWNER_ONLY) and not conn.sql(AUDIT)


def test_a_business_owner_alone_cannot_start() -> None:
    conn = viewer_conn().on(GRANT, grant(owner=False, business=True))
    assert _start(conn, {"role": "sales"}).status_code == 403 and not conn.sql(AUDIT)


def test_a_wider_target_is_refused() -> None:
    cis = LocalCis()
    conn = viewer_conn("sales")
    r = _start(conn, {"role": "admin"}, cis.workforce.token(["booking-sales"]), cis)
    assert (r.status_code, r.json()["detail"]) == (403, view_as.NOT_DOWNWARD) and not conn.sql(AUDIT)


@pytest.mark.parametrize(
    "role_row",
    [
        None,
        {"code": "custom_x", "label": "X", "based_on": "sales", "archived": True, "capabilities": ["trip_case.read"]},
    ],
)
def test_an_unknown_or_archived_role_is_refused(role_row: Any) -> None:
    conn = viewer_conn().on(ROLE, role_row)
    r = _start(conn, {"role": "custom_x"})
    assert (r.status_code, r.json()["detail"]) == (404, view_as.UNKNOWN_ROLE) and not conn.sql(AUDIT)


def test_minutes_are_bounded() -> None:
    assert _start(viewer_conn(), {"role": "sales", "minutes": view_as.MAX_MINUTES + 1}).status_code == 422
    assert _start(viewer_conn(), {"role": "sales", "minutes": view_as.MIN_MINUTES - 1}).status_code == 422


def test_end_is_idempotent_and_own_only() -> None:
    cis = LocalCis()
    conn = viewer_conn()
    with client(cis, conn) as c:
        first = c.post(f"/booking/view-as/{SESSION}/end", headers=auth(admin_token(cis)))
    assert first.status_code == 200 and first.json()["changed"] is True
    conn = viewer_conn(ended=True)
    with client(cis, conn) as c:
        again = c.post(f"/booking/view-as/{SESSION}/end", headers=auth(admin_token(cis)))
    assert again.json()["changed"] is False and not [a for a in conn.sql(AUDIT) if a[3] == view_as.ENDED]
    conn = viewer_conn(row=start_row(actor="cog:someone-else"))
    with client(cis, conn) as c:
        assert c.post(f"/booking/view-as/{SESSION}/end", headers=auth(admin_token(cis))).status_code == 404


def test_read_permissions_follow_nested_route_gates() -> None:
    from app.main import create_app  # noqa: PLC0415

    cis = LocalCis()
    with client(cis, viewer_conn()) as c:
        reads = view_as.read_permissions(c.app)
    assert {"staff.directory", "staff.admin", "audit.read", "pricing.read", "trip_case.read"} <= reads
    assert "trip_case.write" not in reads
    assert create_app  # imported: the app factory is the walked one
    ok, missing = view_as.downward(role_permissions("admin"), role_permissions("finance"), reads)
    assert ok and not missing
    assert not view_as.downward(role_permissions("sales"), role_permissions("finance"), reads)[0]
