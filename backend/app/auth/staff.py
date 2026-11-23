"""Booking staff: the thin staff profile, sign-in rules, permissions and FastAPI dependencies.

A request is staff-authenticated when its bearer token passes the multi-issuer verifier (``app.auth.identity_config``,
a byte copy of the CIS verifier) **and** the matching active ``booking.staff_users`` row exists. Two issuers:

- **WORKFORCE** (Cognito staff pool, clients booking-staff / breakglass; AA-680 Phase 2, ADR 0006a). The row is found
  by its Cognito identity ``(cognito_iss, cognito_sub)`` (bk-016). The Booking role comes from the ``booking-*`` group
  on **every** request (the first in :data:`BOOKING_GROUP_ROLES` order wins); no ``booking-*`` group is 403
  ``no_booking_role`` and nothing is written. An identity with no row gets one, keyed ``'cog:' || sub`` and active,
  from the token claims; bridge-era rows were linked to their identity once by the AA-CIS-Infra backfill script. A
  linked row whose ``active`` is false is refused (403 ``staff_inactive``): ``active`` stays a Booking-side switch.
- **BRIDGE** (the CIS-minted Booking token, during the transition only, ADR 0006a A6): the row is keyed by the CIS
  ``admin_users.id`` (``sub``) and provisioned on the first token: CIS ``admin`` → Booking ``admin`` (active);
  ``reviewer`` → ``sales`` and ``content`` → ``content_creator``, both inactive until a Booking admin activates them.
  Later tokens refresh the profile fields (email, name, cis_role, last_seen_at) but never the role or the active flag.

**Preview cap (AA-911 decision D-1, root ADR 0006a A11).** A WORKFORCE token from the Preview-only
``booking-staff-preview`` client (the fixed Preview host, which ran unmerged PR code; retired with Vercel in S272, so
only a client set through ``COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID`` is affected) is a read-only session whatever
``booking-*`` group the user holds: any request other than GET / HEAD / OPTIONS is 403 ``preview_read_only`` before
anything is read or written, and :meth:`Staff.can` grants only :data:`PREVIEW_PERMISSIONS` (reads; never
``staff.admin``, ``audit.read`` or any write). ``/booking/staff/me`` reports ``preview_viewer: true`` and the capped
permission list, so the UI hides write actions.

**E2E test session cap (AA-910, root ADR 0006a A10a).** The temporary WORKFORCE E2E user
:data:`E2E_TEST_STAFF_EMAIL` holds ``booking-admin`` so a signed-in smoke can read every staff page (``/staff`` and
``/audit-log`` included), but its sign-in codes sit in a shared test mailbox. So its session is read-only on every
client: any request other than GET / HEAD / OPTIONS is 403 ``e2e_read_only`` before anything is read or written.
Reads keep the admin permissions. The match is on the access token's ``email`` claim, which the WORKFORCE pre token
trigger sets to the normalised address; lower-casing here can only cap more sessions, never fewer.

**Finance (AA-1035 R2, root ADR 0019 d3 / d9).** ``finance`` issues invoices and records payments (``billing.read``,
``billing.write``) and runs agency billing (``agency_billing.*``, ``agency_payment.approve``, ``billing_config.write``,
AA-1037). It comes ONLY from the WORKFORCE group ``booking-finance`` (granted on the CIS Staff page through
``shared.staff_app_grant`` role ``finance``); the CIS -> Booking bridge never yields it (:data:`ROLE_FROM_CIS` has no
finance entry and CIS mints no bridge token for its new roles). AA-1150 (S285 L7a): finance also reads trip cases,
prices, supply, operations, supplier bills and the catalog (``trip_case.read``, ``pricing.read``, ``supply.read``,
``ops.read``, ``bill.read``, ``catalog.read``); it writes none of them, and ``payout.write`` stays admin only.

**Make / approve split (AA-1150).** Three approval capabilities are separate from the write rights that make the work:
``supplier.activate`` (activate a supplier), ``contract.publish`` (publish a supplier contract) and
``stop_sale.approve`` (review a pending stop-sale, approve or reject the end of one). A role with ``supply.write`` /
``stop_sale.write`` alone makes the work and a second person approves it. The built-in ``admin`` and ``contracting``
roles hold all three. AA-1155 T4.1 (root spec R4.3) splits the rest: ``pricing.approve`` (approve / reject a pricing
rule, FX rate or cancellation policy, approve an agency tier discount or rate card, approve a departure price
adjustment, reject another person's pending one or withdraw one in force), ``b2b_desk.approve`` the same for an agency
allotment, and ``supplier.activate`` also for activating a hotel or a service (one a maker without it creates starts
``inactive``). Each is checked in the service (403 ``forbidden``); the maker still withdraws its own PENDING price
adjustment or allotment with the write right.

**Dev-only E2E write identities (AA-1087, root ADR 0006a A10e).** Three more WORKFORCE E2E users may write, each only
its own allow-listed routes, only on Dev and only on a synthetic agency's record (:mod:`app.auth.e2e_staff`): a staff
user (``booking-sales``: send and confirm a supplier booking) and a finance maker / checker pair (``booking-finance``:
record an agency payment; approve or reject it). Checked here, before the route runs. A10e-1 (S278 l18): the staff
user may also cancel a synthetic departure (body exactly ``{"status": "cancelled"}``); that one request then holds
``trip_plan.write`` (``Staff.e2e_grants``), which its ``sales`` role lacks. A10e-2 (AA-1091 S279): a pricing maker
(the CIS A10d address with the "Pricing team" preset, ``cis-customer_success`` + ``booking-contracting``) and a pricing
checker (``booking-contracting``) run the price steps on synthetic trip plans only; the workbook import checks its
tours inside the route (``Staff.e2e_writer``).

**Administrator MFA (AA-1111 S2, root ADR 0006c, bk-061; :mod:`app.auth.admin_mfa`).** A WORKFORCE ``booking-admin``
session (not Preview, not a Dev-only E2E identity) whose access token's ``amr`` lacks ``otp`` (no authenticator code)
is refused 403 ``mfa_required`` while the pricing policy parameter ``staff_admin_mfa_required`` is on; off, it is only
logged. Checked before the staff row sync.

**Custom roles (AA-1155 T5, root spec R4.1-R4.3, root ADR 0006a A14; :mod:`app.auth.custom_roles`).** A WORKFORCE
token with the group ``booking-custom`` and no built-in ``booking-*`` group holds the custom role the CIS Staff page
granted, read through the CIS view ``shared.v_staff_booking_grant`` and ``booking.staff_role`` (bk-062), cached at most
60 s. The staff row's ``role`` is synced to the custom code (audited like any role change) and :meth:`Staff.can` answers
from that role's capabilities (:attr:`Staff.custom_permissions`), never the approve class. A missing grant, an unknown
or archived role is 403 ``no_booking_role``; an unreadable view or table 403 ``custom_role_unavailable`` (fail closed
for custom holders only: built-in holders never read either).

**View as a lower role (AA-1157, root ADR 0019 d5 as amended; :mod:`app.auth.view_as`).** A WORKFORCE request carrying
the header ``x-booking-view-as`` (the owner's opaque view-as session id, forwarded by the staff BFF) is refused 403
``view_as_read_only`` for every method other than GET / HEAD / OPTIONS, before anything is read or written. A read
runs as the target role once the session checks out (permissions = target ∩ viewer, ``view_as`` set; the owner markers
read False); a dead session answers 409 ``view_as_ended``. The header on a BRIDGE token is 403.

Permissions are a role matrix in code (same pattern as CIS ``frontend/lib/rbac.ts``, ERD v2 README
question 5). The matrix is provisional until the PRD role matrix is confirmed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import audit
from app.auth import admin_mfa, custom_roles, e2e_staff, identity_config, view_as
from app.auth.identity_verifier import AuthUnavailable, InvalidToken, Principal

log = logging.getLogger(__name__)

STAFF_ROLES = ("admin", "sales", "contracting", "content_creator", "finance")  # finance: AA-1035, bk-038
# BRIDGE: CIS role (bridge token cis_role) → Booking role on first provisioning (ERD v2 identity note).
ROLE_FROM_CIS = {"admin": "admin", "reviewer": "sales", "content": "content_creator"}
AUTO_ACTIVE_CIS_ROLES = frozenset({"admin"})

# WORKFORCE: Cognito group → Booking role, in precedence order (AA-CIS-Infra identity.tf local.id_staff_groups, ADR
# 0006 decision 3). A user in several booking-* groups gets the first one listed here.
BOOKING_GROUP_ROLES: tuple[tuple[str, str], ...] = (
    ("booking-admin", "admin"),
    ("booking-sales", "sales"),
    ("booking-contracting", "contracting"),
    ("booking-content_creator", "content_creator"),
    ("booking-finance", "finance"),  # AA-1035 R2 (identity.tf precedence 75)
)
# WORKFORCE: the informational cis_role column = the highest cis-* group, if any (the CIS API's GROUP_ROLES order;
# AA-1035: the six R2 groups, and cis-reviewer is an alias of content for one release).
CIS_GROUP_ROLES: tuple[tuple[str, str], ...] = (
    ("cis-admin", "admin"),
    ("cis-content_lead", "content_lead"),
    ("cis-ai_ops", "ai_ops"),
    ("cis-customer_success", "customer_success"),
    ("cis-finance", "finance"),
    ("cis-reviewer", "content"),
    ("cis-tenant_qa", "tenant_qa"),
    ("cis-support", "support"),
    ("cis-content", "content"),
)
# Prefix of the staff key of rows created on a WORKFORCE sign-in (bk-016 staff_users_cognito_key_chk).
COGNITO_KEY_PREFIX = "cog:"

PERMISSIONS: dict[str, frozenset[str]] = {
    # AA-1150 (S285 L7a): finance reads trip cases, prices, supply, operations, supplier bills and the catalog (it
    # writes none of them; payouts stay admin only, tech lead decision Q4).
    "trip_case.read": frozenset({"admin", "sales", "contracting", "finance"}),
    "trip_case.write": frozenset({"admin", "sales"}),
    "trip_case.assign": frozenset({"admin", "sales"}),
    "version.write": frozenset({"admin", "sales"}),
    "customer.read": frozenset({"admin", "sales", "contracting"}),
    "customer.write": frozenset({"admin", "sales"}),
    "customer.admin": frozenset({"admin"}),  # block / unblock, archive
    "staff.directory": frozenset({"admin", "sales", "contracting"}),  # StaffPicker (D4)
    # Margins, net costs and FX are commercial data: sales quote, contracting owns rates (ADR 0005 d8).
    "pricing.read": frozenset({"admin", "sales", "contracting", "finance"}),
    # New pricing rule / FX rows and their approval (maker-checker, bk-007): contracting owns rates, admin backs up.
    "pricing.write": frozenset({"admin", "contracting"}),
    # AA-1155 T4.1 (make / approve split, root spec R4.3): the checker side of pricing.write is its own capability.
    # Approving or rejecting a pricing rule, FX rate or cancellation policy, approving an agency tier discount or rate
    # card, approving a departure price adjustment and ending another person's or one in force need pricing.approve;
    # making a row and withdrawing one's own pending row stay pricing.write. Admin and the built-in contracting hold
    # both (a custom role never holds an approval).
    "pricing.approve": frozenset({"admin", "contracting"}),
    # Supply (bk-008, ADR 0005 d8): contracting owns suppliers and contracts; sales reads them to quote.
    "supply.read": frozenset({"admin", "sales", "contracting", "finance"}),
    "supply.write": frozenset({"admin", "contracting"}),
    # AA-1150 (make / approve split): approving supply is its own capability, never implied by supply.write. Activating
    # a supplier (POST /suppliers/{ref}/activate) and publishing a contract (status published; still never by the
    # draft's maker, 403 maker_checker) = admin and the built-in contracting ("Product & operations"), which keep both
    # so the A10e-2 pricing checker and the CIS "Pricing team" preset work as before. A later custom maker role holds
    # supply.write without them. AA-1155 T4.1: activating a hotel or a service needs supplier.activate as well.
    "supplier.activate": frozenset({"admin", "contracting"}),
    "contract.publish": frozenset({"admin", "contracting"}),
    # AA-1145 (bk-060; T13 review 2.4): set, review and end a stop-sale on a supplier contract. Contracting (shown as
    # "Product & operations") and admin; sales never (403). A new stop-sale is in effect at once; its review and the
    # end of an approved one take a second person (403 maker_checker + CHECKs).
    "stop_sale.write": frozenset({"admin", "contracting"}),
    # AA-1150: setting a stop-sale, withdrawing one's own and asking to end one stay stop_sale.write; reviewing a
    # pending stop-sale (approve / reject) and approving or rejecting its end need stop_sale.approve (also on the
    # stop-sale ranges of season calendars). Admin and contracting hold both.
    "stop_sale.approve": frozenset({"admin", "contracting"}),
    # CIS catalog reads (content is read-only in Booking, O7): every staff role, content creators included.
    "catalog.read": frozenset({"admin", "sales", "contracting", "content_creator", "finance"}),
    # Commercial row of a catalog tour (sellable, trip style, price hint, notes; bk-009): contracting + admin.
    "trip_plan.write": frozenset({"admin", "contracting"}),
    # Booking state of CIS countries and destinations (bk-029, AA-996): status, area, notes. Admin only (UI spec §3).
    "catalog.admin": frozenset({"admin"}),
    "staff.admin": frozenset({"admin"}),
    "audit.read": frozenset({"admin"}),
    # Retention runs and their status (AA-825): DRY RUN / live delete and redaction of Booking rows.
    "retention.admin": frozenset({"admin"}),
    # B2B partners (bk-019, AA-972): which agencies may call booking.b2b.v1 and through which M2M client.
    "b2b.admin": frozenset({"admin"}),
    # B2B desk (bk-022, AA-975): on-request lines to accept / decline and incidents on departures. Every operational
    # role reads and answers it (no separate ops role exists); content creators do not.
    "b2b_desk.read": frozenset({"admin", "sales", "contracting"}),
    "b2b_desk.write": frozenset({"admin", "sales", "contracting"}),
    # AA-1075 (bk-047, root S275 study bk-11 / bk-14): enter a booking for an agency (e-mail / chat), cancel or reduce
    # one for it and ask for a lower cancellation fee = admin, sales (who talk to agencies). A price below the floor
    # margin and a lower cancellation fee need a second person with b2b_desk.approve = admin, contracting (who own
    # the pricing), never the one who asked (maker-checker, 403 maker_checker + CHECKs). Not in preview sessions.
    # AA-1155 T4.1: approving an agency allotment and ending another person's or one in force also need
    # b2b_desk.approve (proposing one and withdrawing one's own pending one stay b2b_desk.write).
    "b2b_desk.book": frozenset({"admin", "sales"}),
    "b2b_desk.approve": frozenset({"admin", "contracting"}),
    # Operations (bk-026, AA-682 S1): supplier bookings of a sold trip and their documents. Every operational role
    # books and chases suppliers (no separate ops role yet, design D16); content creators do not.
    "ops.read": frozenset({"admin", "sales", "contracting", "finance"}),
    "ops.write": frozenset({"admin", "sales", "contracting"}),
    # Billing (bk-028, AA-682 S3): customer invoices and manual payments. Sales draft, send and record; only an admin
    # verifies a payment, and never one they recorded (maker-checker, design D6). Contracting does not see customer
    # money (design §5). Not in preview sessions (critic note 8: bill-to snapshots and payment references).
    # AA-1035 R2 (root ADR 0019 d9): finance issues invoices and records payments; verifying stays admin (design D6).
    "billing.read": frozenset({"admin", "sales", "finance"}),
    "billing.write": frozenset({"admin", "sales", "finance"}),
    "payment.verify": frozenset({"admin"}),
    # Supplier bills and payouts (bk-030, AA-682 S4, design §5): every operational role reads them; contracting (who
    # books suppliers) and admin enter and approve bills, never their own (maker-checker); only an admin records a
    # payout, never on a bill they entered. Not in preview sessions (payout references are bank references).
    "bill.read": frozenset({"admin", "sales", "contracting", "finance"}),
    "bill.write": frozenset({"admin", "contracting"}),
    "bill.approve": frozenset({"admin", "contracting"}),
    "payout.write": frozenset({"admin"}),
    # Agency billing (bk-041 / bk-042, AA-1037 R4, root ADR 0019 d3 / d9): every invoice from AA to an agency (software
    # fee + tour settlement). Finance runs it: drafts, issues, records payments; a second finance user (or an admin)
    # approves or rejects a payment, never its recorder (maker-checker, 403 maker_checker + a CHECK). The billing
    # entity, tax rules and payment terms are data rows finance edits (b16 / b17). Not in preview sessions.
    "agency_billing.read": frozenset({"admin", "finance"}),
    "agency_billing.write": frozenset({"admin", "finance"}),
    "agency_payment.approve": frozenset({"admin", "finance"}),
    "billing_config.write": frozenset({"admin", "finance"}),
    # AA-1037 extras G6 (bk-044): a partner terms clause (e.g. price errors) reaches agencies only once approved; the
    # business owner approves (admin), never the clause's last editor (403 maker_checker + a CHECK).
    "terms_clause.approve": frozenset({"admin"}),
    # AA-1074 (bk-045, root S275 study bk-08): profit per trip case, departure and agency (revenue, net costs, margin).
    # Finance reads it, admin backs up; sales and contracting see margins at price time (pricing.read) but not the
    # realised profit (proposed default, to confirm with the business owner). Not in preview sessions.
    "profit.read": frozenset({"admin", "finance"}),
    # AA-1091 (bk-050, root ADR 0005b d6 / d15, Q5): the pricing policy parameters (quote validity, surcharge rules,
    # auto-publish threshold, deposits, ...). Everyone who prices or bills reads them; contracting and finance propose
    # a change; the product / pricing lead (contracting, admin) approves the price parameters and finance (admin
    # backs up) the FX, tax, surcharge and payment ones, never the change's maker (403 own_row + a table CHECK).
    "pricing_policy.read": frozenset({"admin", "sales", "contracting", "finance"}),
    "pricing_policy.write": frozenset({"admin", "contracting", "finance"}),
    "pricing_policy.approve": frozenset({"admin", "contracting"}),
    "pricing_policy.approve_finance": frozenset({"admin", "finance"}),
    # AA-1096 (bk-056, S279 thresholds program): the data retention windows on Pricing > Policy are a DM-7.5 decision;
    # only the administrator approves them (a finance or contracting maker proposes; never the maker, as above).
    "pricing_policy.approve_admin": frozenset({"admin"}),
    # AA-1091 (bk-054 / bk-055, root ADR 0005b d6): hand prices are price list versions and seasons are calendars. The
    # customer-service maker (contracting) drafts and submits; the product / pricing lead (contracting, admin) approves,
    # rejects or withdraws a published one, never their own version (403 maker_checker + table CHECKs).
    "price_version.write": frozenset({"admin", "contracting"}),
    "price_version.approve": frozenset({"admin", "contracting"}),
    "season.write": frozenset({"admin", "contracting"}),
    "season.approve": frozenset({"admin", "contracting"}),
}

# AA-911 D-1: the most a Preview-client session may do, intersected with its role's permissions. Reads only; the admin
# reads (staff.admin, audit.read, retention.admin) and every write stay out.
PREVIEW_PERMISSIONS: frozenset[str] = frozenset(
    {
        "trip_case.read",
        "customer.read",
        "staff.directory",
        "pricing.read",
        "pricing_policy.read",
        "supply.read",
        "catalog.read",
        "b2b_desk.read",
        "ops.read",
    }
)
# The only methods a Preview-client session may call (RFC 9110 safe methods that routes here serve).
PREVIEW_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# AA-910 (root ADR 0006a A10a): the Booking E2E test user. Its session never writes (see the module docstring). A code
# constant that mirrors the WORKFORCE trigger allow-map (AA-CIS-Infra workforce.E2E_BOOKING_TEST_STAFF_EMAIL); removed
# with that exception.
E2E_TEST_STAFF_EMAIL = "e2e-booking@e2e-mail.lumiguides.it.com"

# last_seen_at is refreshed at most this often (one write per staff user per 5 minutes, not per request).
LAST_SEEN_EVERY = "5 minutes"
LAST_SEEN_EVERY_TD = timedelta(minutes=5)

STAFF_COLUMNS = "cis_user_id, email, name, role, active, cis_role, created_at, updated_at, last_seen_at, cognito_sub"
# Columns every staff row dict carries; cognito_sub (bk-016) is read with .get so older fakes stay valid.
_BASE_COLUMNS = (
    "cis_user_id",
    "email",
    "name",
    "role",
    "active",
    "cis_role",
    "created_at",
    "updated_at",
    "last_seen_at",
)

bearer = HTTPBearer(
    auto_error=False, description="Cognito WORKFORCE access token (booking-staff client) or the CIS bridge token"
)


@dataclass(frozen=True)
class Staff:
    cis_user_id: str
    email: str | None
    name: str | None
    role: str
    active: bool
    cis_role: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None
    cognito_sub: str | None = None
    # Session flag, not a column: True when this request's token came from the Preview-only client (AA-911 D-1).
    preview: bool = False
    # Session flag, not a column (AA-1087 A10e-1): permissions one Dev-only E2E write request holds on top of its role,
    # set by current_staff only after every e2e_staff check passed (app.auth.e2e_staff.E2EWriter.grants).
    e2e_grants: frozenset[str] = frozenset()
    # Session flag, not a column (AA-1091 A10e-2): the Dev-only E2E writer's name (app.auth.e2e_staff.E2EWriter.name)
    # when this request comes from one, so a route that checks its own records (the workbook import) knows it.
    e2e_writer: str | None = None
    # Session fields, not columns (AA-1155 T5): a booking-custom holder's capabilities (already narrowed to known,
    # non-approve capabilities) and its role's label. None for a built-in role: the matrix answers.
    custom_permissions: frozenset[str] | None = None
    role_label: str | None = None
    # Session field, not a column (AA-1157): the owner's open read-only "view as a lower role" session
    # (app.auth.view_as.ViewAs). role / custom_permissions are then the target's (target ∩ viewer); every write is
    # refused before the route.
    view_as: Any = None

    @classmethod
    def from_row(cls, row: Any) -> Staff:
        return cls(**{k: row[k] for k in _BASE_COLUMNS}, cognito_sub=row.get("cognito_sub"))

    @property
    def managed_in_directory(self) -> bool:
        """Linked to a Cognito identity: the role follows the booking-* group (CIS → Platform → Staff)."""
        return self.cognito_sub is not None

    def can(self, permission: str) -> bool:
        if self.preview and permission not in PREVIEW_PERMISSIONS:
            return False
        if self.active and permission in self.e2e_grants:
            return True
        if self.custom_permissions is not None:
            return self.active and permission in self.custom_permissions
        return self.active and self.role in PERMISSIONS.get(permission, frozenset())

    @property
    def permissions(self) -> list[str]:
        return sorted(p for p in PERMISSIONS if self.can(p))

    def public(self) -> dict[str, Any]:
        return {
            "cis_user_id": self.cis_user_id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "active": self.active,
            "cis_role": self.cis_role,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "managed_in_directory": self.managed_in_directory,
        }


@dataclass(frozen=True)
class BridgeClaims:
    """What the CIS bridge token says about the caller (``sub`` = CIS ``admin_users.id``)."""

    sub: str
    email: str | None
    name: str | None
    cis_role: str


async def provision(conn: asyncpg.Connection, claims: BridgeClaims, role: str) -> Staff:
    """BRIDGE: create the profile on the first token, refresh profile fields afterwards. Returns the row."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO booking.staff_users AS s (cis_user_id, email, name, role, active, cis_role, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (cis_user_id) DO UPDATE
            SET email = coalesce(EXCLUDED.email, s.email),
                name = coalesce(EXCLUDED.name, s.name),
                cis_role = EXCLUDED.cis_role,
                last_seen_at = now()
            WHERE s.last_seen_at IS NULL
               OR s.last_seen_at < now() - interval '{LAST_SEEN_EVERY}'
               OR s.cis_role IS DISTINCT FROM EXCLUDED.cis_role
               OR (EXCLUDED.email IS NOT NULL AND s.email IS DISTINCT FROM EXCLUDED.email)
               OR (EXCLUDED.name IS NOT NULL AND s.name IS DISTINCT FROM EXCLUDED.name)
        RETURNING {STAFF_COLUMNS}, (xmax = 0) AS inserted
        """,  # noqa: S608 — constants only
        claims.sub,
        claims.email,
        claims.name,
        role,
        claims.cis_role in AUTO_ACTIVE_CIS_ROLES,
        claims.cis_role,
    )
    inserted = False
    if row is None:  # unchanged within LAST_SEEN_EVERY: nothing written
        row = await conn.fetchrow(
            f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1",  # noqa: S608
            claims.sub,
        )
    else:
        inserted = bool(row["inserted"])
    staff = Staff.from_row(row)
    if inserted:
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=staff.cis_user_id,
            action="staff.provisioned",
            entity_type="staff_user",
            entity_id=staff.cis_user_id,
            after={**staff.public(), "from_cis_role": claims.cis_role},
        )
    return staff


def role_permissions(role: str) -> frozenset[str]:
    """The built-in matrix row of ``role`` (empty for a custom code: its capabilities come from the custom role)."""
    return frozenset(p for p, roles in PERMISSIONS.items() if role in roles)


def booking_role(groups: tuple[str, ...]) -> str | None:
    """The Booking role of a WORKFORCE principal: the first booking-* group in precedence order, or None."""
    matched = [role for group, role in BOOKING_GROUP_ROLES if group in groups]
    if len(matched) > 1:
        log.info("booking_role_multiple roles=%s chosen=%s", ",".join(matched), matched[0])
    return matched[0] if matched else None


def cis_role_from_groups(groups: tuple[str, ...]) -> str | None:
    return next((role for group, role in CIS_GROUP_ROLES if group in groups), None)


def _display_name(email: str | None) -> str | None:
    """Initial name of a row created on a WORKFORCE sign-in: Cognito access tokens carry no name, and Booking does not
    read the CIS directory, so the e-mail local part stands in (``thu@adventure.asia`` → ``thu``)."""
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[0][:120] or None


_IDENTITY_WHERE = "cognito_iss = $1 AND cognito_sub = $2"


async def sync_cognito_staff(conn: asyncpg.Connection, principal: Principal, role: str) -> Staff:
    """WORKFORCE: the row of this Cognito identity, created on the first sign-in, its role synced from the groups.

    Never rebinds an identity: a row is found by ``(cognito_iss, cognito_sub)`` only. The caller has already refused a
    principal without a booking-* group. Runs inside the caller's transaction.
    """
    cis_role = cis_role_from_groups(principal.groups)
    row = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE {_IDENTITY_WHERE}",  # noqa: S608 — constants only
        principal.iss,
        principal.sub,
    )
    if row is None:
        key = COGNITO_KEY_PREFIX + principal.sub
        row = await conn.fetchrow(
            f"""
            INSERT INTO booking.staff_users
                (cis_user_id, email, name, role, active, cis_role, cognito_iss, cognito_sub, last_seen_at)
            VALUES ($1, $2, $3, $4, true, $5, $6, $7, now())
            ON CONFLICT DO NOTHING
            RETURNING {STAFF_COLUMNS}
            """,  # noqa: S608 — constants only
            key,
            principal.email,
            _display_name(principal.email),
            role,
            cis_role,
            principal.iss,
            principal.sub,
        )
        if row is not None:
            staff = Staff.from_row(row)
            log.info("booking_staff_provisioned source=cognito cis_user_id=%s role=%s", key, role)
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=staff.cis_user_id,
                action="staff.provisioned",
                entity_type="staff_user",
                entity_id=staff.cis_user_id,
                after={**staff.public(), "source": "cognito", "client_id": principal.client_id},
                entity_label=staff.name or staff.cis_user_id,
                summary=f"Created on the first company e-mail sign-in, role {role} (Cognito group)",
            )
            return staff
        # A concurrent first sign-in created it (ON CONFLICT waited for that commit), or the key is taken.
        row = await conn.fetchrow(
            f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE {_IDENTITY_WHERE}",  # noqa: S608
            principal.iss,
            principal.sub,
        )
        if row is None:
            log.warning("booking_staff_identity_conflict cis_user_id=%s", key)
            raise HTTPException(status_code=403, detail="identity_conflict")
    return await _sync_row(conn, Staff.from_row(row), principal, role, cis_role)


async def _sync_row(
    conn: asyncpg.Connection, staff: Staff, principal: Principal, role: str, cis_role: str | None
) -> Staff:
    email = principal.email or staff.email
    stale = staff.last_seen_at is None or datetime.now(UTC) - staff.last_seen_at >= LAST_SEEN_EVERY_TD
    if (staff.role, staff.email, staff.cis_role) == (role, email, cis_role) and not stale:
        return staff
    locked = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1 FOR UPDATE",  # noqa: S608
        staff.cis_user_id,
    )
    if locked is None:  # deleted since the first read: nothing to sync (the next request provisions again)
        return staff
    before = Staff.from_row(locked)
    updated = Staff.from_row(
        await conn.fetchrow(
            f"""
            UPDATE booking.staff_users
               SET role = $2, email = $3, cis_role = $4, last_seen_at = now()
             WHERE cis_user_id = $1
            RETURNING {STAFF_COLUMNS}
            """,  # noqa: S608 — constants only
            staff.cis_user_id,
            role,
            email,
            cis_role,
        )
    )
    if before.role != updated.role:
        log.info(
            "booking_staff_role_synced cis_user_id=%s from=%s to=%s", updated.cis_user_id, before.role, updated.role
        )
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=updated.cis_user_id,
            action="staff.role_synced",
            entity_type="staff_user",
            entity_id=updated.cis_user_id,
            before={"cis_user_id": updated.cis_user_id, "role": before.role},
            after={"cis_user_id": updated.cis_user_id, "role": updated.role},
            entity_label=updated.name or updated.cis_user_id,
            summary=f"Role {before.role} -> {updated.role} from "
            + (
                "the CIS Staff page custom role grant (booking-custom)"
                if custom_roles.CUSTOM_CODE.fullmatch(updated.role)
                else "the booking-* Cognito group"
            ),
        )
    return updated


def _bridge_claims(principal: Principal, token: str) -> BridgeClaims:
    """BRIDGE: the verified principal plus the ``name`` claim, which the vendored Principal does not carry. The token
    was verified by the caller, so reading one more claim without the signature check is safe here."""
    if principal.sub.startswith(COGNITO_KEY_PREFIX):  # never a CIS admin_users.id; such a key belongs to a Cognito row
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"})
    raw = jwt.decode(token, options={"verify_signature": False}).get("name")
    name = (raw.strip()[:320] or None) if isinstance(raw, str) else None
    cis_role = principal.groups[0] if len(principal.groups) == 1 else ""
    return BridgeClaims(sub=principal.sub, email=principal.email, name=name, cis_role=cis_role)


def is_e2e_test_principal(principal: Principal) -> bool:
    """True for the AA-910 E2E test user (exact address after trim + lower-case; see :data:`E2E_TEST_STAFF_EMAIL`)."""
    return isinstance(principal.email, str) and principal.email.strip().lower() == E2E_TEST_STAFF_EMAIL


async def _json_body(request: Request) -> Any:
    """The request's JSON body, or None when it is empty or not JSON (Starlette caches the bytes for the route)."""
    try:
        raw = await request.body()
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


async def _custom_role(
    pool: asyncpg.Pool,
    principal: Principal,
    role: str | None,
    writer: e2e_staff.E2EWriter | None,
    method: str,
    route: Any,
    body: Any,
) -> tuple[str, custom_roles.CustomRole | None]:
    """The session's Booking role: the built-in ``role`` from the groups, or (AA-1155 T5) for ``booking-custom`` with no
    built-in group the custom role the CIS grant names (fail closed, 403). Then the A10g approve probe check (root ADR
    0006a): a probe of the custom-role maker is answered from its capabilities before any record is read; a probe the
    role could pass is never run unless allow-listed (the synthetic rule follows)."""
    custom = None
    if role is None:
        custom = await custom_roles.resolve(pool, principal.sub)
        role = custom.code
    if writer is not None:
        held = custom.capabilities if custom is not None else role_permissions(role)
        e2e_staff.check_probe(writer, method, route, body, held)
    return role, custom


def _session(
    staff: Staff,
    *,
    custom: custom_roles.CustomRole | None,
    preview: bool,
    writer: e2e_staff.E2EWriter | None,
    method: str,
    route: str | None,
) -> Staff:
    """The request's session fields on the synced row (never stored): a custom role's capabilities and label, the
    Preview cap, an A10e-1 route-scoped grant (every check passed) and the E2E writer's name."""
    if custom is not None:
        staff = replace(staff, custom_permissions=custom.capabilities, role_label=custom.label)
    if preview:
        staff = replace(staff, preview=True)
    if writer is not None:
        staff = replace(staff, e2e_grants=writer.grants_for(method, route), e2e_writer=writer.name)
    return staff


async def get_pool(request: Request) -> asyncpg.Pool:
    db = request.app.state.db
    if not db.configured:
        raise HTTPException(status_code=503, detail="database_not_configured")
    return await db.pool()


async def current_staff(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> Staff:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing_bearer_token", headers={"WWW-Authenticate": "Bearer"})
    verifier = request.app.state.staff_verifier
    token = credentials.credentials
    try:
        principal = await verifier.verify(token)
    except InvalidToken as exc:
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"}) from exc
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_unavailable") from exc
    kind = identity_config.issuer_kind(verifier, principal.iss)
    if kind == identity_config.WORKFORCE:
        preview = identity_config.is_preview_client(request.app.state.settings, principal.client_id)
        if preview and request.method.upper() not in PREVIEW_METHODS:
            # AA-911 D-1: a Preview-host session never writes, whatever its groups. Refused before any DB access.
            log.info("booking_preview_write_refused method=%s path=%s", request.method, request.url.path)
            raise HTTPException(status_code=403, detail="preview_read_only")
        if is_e2e_test_principal(principal) and request.method.upper() not in PREVIEW_METHODS:
            # AA-910: the E2E admin's codes are readable by CI roles; it may read every page but never write.
            log.info("booking_e2e_write_refused method=%s path=%s", request.method, request.url.path)
            raise HTTPException(status_code=403, detail="e2e_read_only")
        view_ref = request.headers.get(view_as.HEADER)
        if view_ref is not None and request.method.upper() not in PREVIEW_METHODS:
            # AA-1157: a view-as session never writes, whatever the route. Refused before any DB access.
            log.info("booking_view_as_write_refused method=%s path=%s", request.method, request.url.path)
            raise HTTPException(status_code=403, detail=view_as.READ_ONLY)
        writer = e2e_staff.writer_for(principal.email)
        route_obj = request.scope.get("route")
        route = getattr(route_obj, "path", None)
        if writer is not None:
            # AA-1087 (ADR 0006a A10e): Dev only, exact groups, its own allow-listed writes; before any DB access.
            # A10g: an approve probe passes this step and is answered by check_probe below.
            probe = e2e_staff.is_probe(writer, request.method, route_obj)
            e2e_staff.check_session(writer, principal.groups, request.method, route, request.url.path, probe=probe)
        role = booking_role(principal.groups)
        if role is None and custom_roles.CUSTOM_GROUP not in principal.groups:
            raise HTTPException(status_code=403, detail="no_booking_role")
        body: Any = None
        if writer is not None and e2e_staff.reads_body(request.method, route):
            # AA-1087 A10e-1 / AA-1091 A10e-2: a write with a body rule (departure cancel: exactly it) or a body check
            # (workbench action: make_sellable / make_not_sellable on tour keys), before any DB access.
            body = await _json_body(request)
            e2e_staff.check_body(writer, request.method, route, body)
        pool = await get_pool(request)
        role, custom = await _custom_role(pool, principal, role, writer, request.method, route_obj, body)
        # AA-1111 (root ADR 0006c): an administrator without an authenticator code, 403 mfa_required while the
        # pricing policy parameter staff_admin_mfa_required is on (logged only while it is off).
        await admin_mfa.check_session(
            pool, principal, token, role=role, exempt=preview or writer is not None or is_e2e_test_principal(principal)
        )
        if writer is not None:
            # AA-1087: an allowed write only on a synthetic agency's record (A10e-2: a synthetic plan, an E2E maker's
            # version of one), before the staff row sync or the route.
            await e2e_staff.check_synthetic(pool, writer, request.method, route, request.path_params, body)
        async with pool.acquire() as conn, conn.transaction():
            staff = await sync_cognito_staff(conn, principal, role)
        staff = _session(staff, custom=custom, preview=preview, writer=writer, method=request.method, route=route)
        if view_ref is not None and staff.active:
            staff = await view_as.apply(pool, request.app, staff, principal.sub, view_ref)
        custom_roles.VIEWING.set(staff.view_as is not None)
    elif kind == identity_config.BRIDGE:
        if request.headers.get(view_as.HEADER) is not None:
            raise HTTPException(status_code=403, detail=view_as.READ_ONLY)
        claims = _bridge_claims(principal, token)
        role = ROLE_FROM_CIS.get(claims.cis_role)
        if role is None:
            raise HTTPException(status_code=403, detail="unknown_cis_role")
        pool = await get_pool(request)
        async with pool.acquire() as conn, conn.transaction():
            staff = await provision(conn, claims, role)
    else:  # an issuer the verifier holds but this module has no rules for: refuse
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"})
    if not staff.active:
        raise HTTPException(status_code=403, detail="staff_inactive")
    return staff


def require(permission: str) -> Callable[..., Awaitable[Staff]]:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")

    async def dependency(staff: Annotated[Staff, Depends(current_staff)]) -> Staff:
        if not staff.can(permission):
            raise HTTPException(status_code=403, detail="forbidden")
        return staff

    # AA-1155 T5: the permission a route gate checks, readable from the route (the A10g probe list is tested against
    # it: tests/unit/test_aa1155_custom_roles.py).
    dependency.permission = permission  # type: ignore[attr-defined]
    return dependency
