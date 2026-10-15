"""AA-811: the ``bk_notify_email`` handler and the SES sender, with a stubbed SES client (no AWS call)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Any

import asyncpg
import pytest
from botocore.exceptions import ClientError

from app import mailer
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.notifications import staff_email
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

NOTE_ID = "22222222-2222-2222-2222-222222222222"
TOUR_A, TOUR_B = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"

SETTINGS = Settings(
    database_url=None,
    environment="dev",
    staff_notify_email="staff@adventure.test",
    ses_from_address="no-reply@lumiguides.it.com",
    ses_configuration_set="aa-email-dev-transactional",
    staff_admin_url="https://admin.example.test",
)


class StubSes:
    def __init__(self, error: BaseException | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return {"MessageId": f"ses-{len(self.sent)}"}


def ses_error(code: str) -> ClientError:
    """What boto3 raises for an SES refusal (real botocore class: the duck-typed code read must match it)."""
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "SendEmail")


def case_row(**changes: Any) -> dict[str, Any]:
    row = {
        "notification_id": NOTE_ID,
        "type": "trip_case.new_inquiry",
        "code": "TC-000042",
        "start_date": None,
        "end_date": None,
        "pax": None,
        "pax_adults": None,
        "pax_children": None,
        "inquiry_snapshot": json.dumps(
            {"days": [{"day": 1, "name": "Luang Prabang walk", "source_tour_id": TOUR_B}], "custom_days": []}
        ),
        "customer_name": "Minh Nguyen",
        "customer_email": "minh@example.test",
        "customer_phone": "+84900000001",
        "needs_merge_review": False,
        "legs": json.dumps(
            [
                {"position": 2, "source_tour_id": TOUR_B, "day_from": 5, "day_to": 6},
                {"position": 1, "source_tour_id": TOUR_A, "day_from": 1, "day_to": 4},
            ]
        ),
        "countries": [],
    }
    row.update(changes)
    return row


def conn_for(row: dict[str, Any] | None = None, catalog: Any = None) -> ScriptedConn:
    tours = [{"tour_id": TOUR_A, "title": "Hanoi and Halong Bay", "country": "Vietnam"}] if catalog is None else catalog
    return ScriptedConn(
        [("FROM booking.notifications n", case_row() if row is None else row), ("catalog.v_tour", tours)]
    )


def job(payload: dict[str, Any] | None = None) -> Job:
    return Job("j-1", staff_email.KIND, {"notification_id": NOTE_ID} if payload is None else payload, 1, 5)


async def run(
    conn: ScriptedConn, ses: StubSes, settings: Settings = SETTINGS, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    handler = staff_email.build_handler(settings, lambda _region: ses)
    return await handler(conn, job(payload))


# ── Handler ──────────────────────────────────────────────────────────────────


async def test_sends_one_email_with_the_inquiry_details() -> None:
    ses = StubSes()
    result = await run(conn_for(), ses)

    assert result == {
        "outcome": "sent",
        "message_id": "ses-1",
        "to": "staff@adventure.test",
        "trip_case": "TC-000042",
        "notification_id": NOTE_ID,
    }
    (sent,) = ses.sent
    assert sent["FromEmailAddress"] == "no-reply@lumiguides.it.com"
    assert sent["Destination"] == {"ToAddresses": ["staff@adventure.test"]}
    assert sent["ConfigurationSetName"] == "aa-email-dev-transactional"
    simple = sent["Content"]["Simple"]
    assert simple["Subject"]["Data"] == "[Dev] New inquiry TC-000042: Minh Nguyen"
    text = simple["Body"]["Text"]["Data"]
    for expected in (
        "Customer: Minh Nguyen",
        "Email: minh@example.test",
        "Phone: +84900000001",
        "Countries: Vietnam",
        "Trip length: 6 days",
        "Travel dates: not given yet",
        "Travellers: not given yet",
        "1. Hanoi and Halong Bay (Vietnam), days 1-4",  # catalog title + country
        "2. Luang Prabang walk, days 5-6",  # not in the catalog: snapshot day name
        "Open the trip case: https://admin.example.test/cases/TC-000042",
    ):
        assert expected in text
    html = simple["Body"]["Html"]["Data"]
    assert 'href="https://admin.example.test/cases/TC-000042"' in html
    assert "review before merging" not in text


async def test_dates_pax_review_and_html_escaping() -> None:
    row = case_row(
        customer_name="<b>Eve</b>",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 6),
        pax=3,
        pax_adults=2,
        pax_children=1,
        needs_merge_review=True,
        countries=["VN", "LA"],
    )
    ses = StubSes()
    await run(conn_for(row), ses, settings=replace(SETTINGS, environment="prod"))
    simple = ses.sent[0]["Content"]["Simple"]
    assert simple["Subject"]["Data"] == "New inquiry TC-000042: <b>Eve</b>"  # no env prefix on prod
    text = simple["Body"]["Text"]["Data"]
    assert "Travel dates: 2026-11-01 to 2026-11-06" in text
    assert "Travellers: 2 adults, 1 child" in text
    assert "Countries: Vietnam, VN, LA" in text
    assert "review before merging" in text
    html = simple["Body"]["Html"]["Data"]
    assert "<b>Eve</b>" not in html and "&lt;b&gt;Eve&lt;/b&gt;" in html


async def test_no_configuration_set_is_omitted() -> None:
    ses = StubSes()
    await run(conn_for(), ses, settings=replace(SETTINGS, ses_configuration_set=""))
    assert "ConfigurationSetName" not in ses.sent[0]


@pytest.mark.parametrize(
    ("field", "reason"),
    [("staff_notify_email", "STAFF_NOTIFY_EMAIL not set"), ("ses_from_address", "SES_FROM_ADDRESS not set")],
)
async def test_noop_when_not_configured(field: str, reason: str) -> None:
    ses = StubSes()
    conn = conn_for()
    result = await run(conn, ses, settings=replace(SETTINGS, **{field: ""}))
    assert result == {"outcome": "skipped", "reason": reason, "notification_id": NOTE_ID}
    assert ses.sent == [] and conn.calls == []


@pytest.mark.parametrize("payload", [{}, {"notification_id": 7}, {"notification_id": ""}])
async def test_bad_payload_is_permanent(payload: dict) -> None:
    with pytest.raises(PermanentError):
        await run(conn_for(), StubSes(), payload=payload)


async def test_missing_or_other_notification_is_permanent() -> None:
    with pytest.raises(PermanentError, match="not a trip_case.new_inquiry"):
        await run(ScriptedConn(), StubSes())
    with pytest.raises(PermanentError):
        await run(conn_for(case_row(type="trip_case.assigned")), StubSes())


async def test_unreadable_catalog_falls_back_to_snapshot_titles() -> None:
    ses = StubSes()
    await run(conn_for(catalog=asyncpg.InsufficientPrivilegeError("no grant")), ses)
    text = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert f"1. AA tour {TOUR_A}, days 1-4" in text
    assert "Countries: -" in text


async def test_keyed_leg_finds_its_tour_by_catalog_source_id() -> None:
    """AA-830: a leg with a catalog key is looked up by source_id even when its source_tour_id is a stale tour_id."""
    legs = [{"position": 1, "source_tour_id": "stale-tour-id", "source_system": "cis", "source_id": "src-a",
             "day_from": 1, "day_to": 2}]  # fmt: skip
    catalog = [{"source_id": "src-a", "tour_id": "t-new", "title": "Mekong Valley", "country": "Laos"}]
    ses = StubSes()
    conn = conn_for(case_row(legs=json.dumps(legs)), catalog=catalog)
    await run(conn, ses)
    assert "1. Mekong Valley (Laos), days 1-2" in ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    (asked,) = conn.sql("catalog.v_tour")
    assert asked == (["src-a"],)


async def test_permanent_ses_refusal_fails_the_job_at_once() -> None:
    with pytest.raises(PermanentError, match="MessageRejected"):
        await run(conn_for(), StubSes(ses_error("MessageRejected")))


async def test_transient_ses_error_is_retried() -> None:
    with pytest.raises(mailer.MailError) as info:
        await run(conn_for(), StubSes(ses_error("TooManyRequestsException")))
    assert info.value.permanent is False
    assert not isinstance(info.value, PermanentError)


async def test_network_error_is_retried_as_is() -> None:
    with pytest.raises(ConnectionError):
        await run(conn_for(), StubSes(ConnectionError("reset")))


async def test_the_ses_client_is_created_once_per_handler() -> None:
    made: list[str] = []
    ses = StubSes()

    def factory(region: str) -> StubSes:
        made.append(region)
        return ses

    handler = staff_email.build_handler(SETTINGS, factory)
    await handler(conn_for(), job())
    await handler(conn_for(), job())
    assert made == ["us-west-1"] and len(ses.sent) == 2


# ── Dedupe ───────────────────────────────────────────────────────────────────


async def test_one_job_per_notification_key() -> None:
    assert staff_email.idempotency_key(NOTE_ID) == f"bk_notify_email:{NOTE_ID}"
    assert staff_email.payload(NOTE_ID) == {"notification_id": NOTE_ID}


def test_access_denied_is_retried_not_permanent():
    # S238 review: IAM propagation right after the Infra apply can deny the first sends
    from app import mailer

    assert "AccessDeniedException" not in mailer.PERMANENT_ERROR_CODES
    assert "MessageRejected" in mailer.PERMANENT_ERROR_CODES
