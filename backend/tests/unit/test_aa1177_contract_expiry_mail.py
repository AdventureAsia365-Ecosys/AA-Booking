"""AA-1177 (Jira PR-31): the supplier contract expiry e-mail (app.supply.contract_expiry_mail) and the job that sends
it after the in-app announcement. Invented suppliers, contracts and addresses only."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pytest

from app import mailer
from app.auth.staff import PERMISSIONS, STAFF_ROLES
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.pricing import policy_params, policy_params_ops
from app.settings import Settings
from app.supply import contract_expiry, contract_expiry_mail
from app.supply.contract_expiry_mail import Mailer, Record
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows

TODAY = date(2031, 3, 3)
NOTE_ID = "11770000-0000-4000-8000-000000000001"


def _row(mail: Any = None, **over: Any) -> dict[str, Any]:
    return {
        "notification_id": NOTE_ID,
        "mail": mail,
        "contract_id": "c1177000-0000-4000-8000-000000000001",
        "title": "Riverside Garden rooms 2031",
        "reference": "RG-31",
        "valid_to": date(2031, 4, 2),
        "days_left": 30,
        "supplier_code": "SUP-000777",
        "supplier_name": "Lotus Bay Travel",
        "supplier_categories": ["hotel", "dmc"],
        **over,
    }


STAFF = [
    {"cis_user_id": "cog:a1", "email": "lead@example.test"},
    {"cis_user_id": "cog:a2", "email": "Lead@Example.test"},  # the same inbox twice: mailed once
    {"cis_user_id": "cog:c1", "email": "rates@example.test"},
]


class FakeSes:
    def __init__(self, errors: dict[str, mailer.MailError] | None = None) -> None:
        self.sent: list[mailer.Message] = []
        self.errors = errors or {}

    async def __call__(self, message: mailer.Message) -> str:
        if message.to in self.errors:
            raise self.errors[message.to]
        self.sent.append(message)
        return f"msg-{len(self.sent)}"


def _mail(ses: FakeSes | None = None) -> Mailer:
    return Mailer(ses or FakeSes(), "dev", "https://booking.example.test")


def _conn(pending: list[dict[str, Any]], *, staff: list[dict[str, Any]] | None = None, **policy: str) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.notifications n JOIN booking.supplier_contracts k", lambda *_: pending)
    conn.on("FROM booking.staff_users", lambda *_: STAFF if staff is None else staff)
    conn.on(POLICY_FRAGMENT, lambda *_: policy_rows(**policy))
    return conn


def _records(conn: ScriptedConn) -> list[dict[str, Any]]:
    return [json.loads(args[1]) for args in conn.sql("UPDATE booking.notifications SET data = jsonb_set")]


async def _run(conn: ScriptedConn, mail: Mailer, **kw: Any) -> dict[str, Any]:
    return await contract_expiry_mail.run(conn, mail, today=TODAY, window_days=60, **kw)  # type: ignore[arg-type]


# ── The parameter ────────────────────────────────────────────────────────────


def test_the_roles_parameter_offers_every_built_in_role_and_is_read_by_the_mail_module() -> None:
    param = policy_params.PARAMS["contract_expiry_mail_roles"]
    assert param.type == policy_params.CODES and param.group == policy_params.PRICING
    assert set(param.choices) == set(STAFF_ROLES) == set(policy_params_ops.STAFF_ROLE_CHOICES)
    assert param.used_by == "app.supply.contract_expiry_mail" and param.section == "operations"
    assert policy_params.normalise("contract_expiry_mail_roles", ["contracting", "admin"]) == ["admin", "contracting"]
    assert policy_params.normalise("contract_expiry_mail_roles", []) == []
    with pytest.raises(policy_params.PolicyValueError):
        policy_params.normalise("contract_expiry_mail_roles", ["owner"])


# ── The message ──────────────────────────────────────────────────────────────


def test_the_message_is_english_then_vietnamese_with_business_facts_only() -> None:
    m = contract_expiry_mail.render(
        _row(), to="rates@example.test", environment="dev", admin_url="https://booking.example.test/"
    )
    assert m.to == "rates@example.test"
    assert m.subject.startswith("[Dev] Supplier contract ends 2031-04-02 / Hợp đồng nhà cung cấp hết hạn 2031-04-02")
    assert "Lotus Bay Travel" in m.subject
    link = "https://booking.example.test/suppliers/SUP-000777?tab=contracts"
    for body in (m.text, m.html):
        assert link in body
        assert "ends in 30 days" in body and "hết hạn sau 30 ngày" in body
        assert body.index("ends in 30 days") < body.index("hết hạn sau 30 ngày")
        assert "Hotel, DMC" in body and "Khách sạn, DMC" in body
        assert "RG-31" in body
        assert "@" not in body.replace("rates@example.test", "")  # no other address in the body


def test_prod_has_no_prefix_one_day_is_singular_and_html_is_escaped() -> None:
    row = _row(days_left=1, title="<b>Rooms</b> & more", reference=None)
    m = contract_expiry_mail.render(row, to="x@example.test", environment="prod", admin_url="https://b.example.test")
    assert m.subject.startswith("Supplier contract ends")
    assert "ends in 1 day." in m.text and "Reference: -" in m.text
    assert "<b>Rooms</b>" not in m.html and "&lt;b&gt;Rooms&lt;/b&gt; &amp; more" in m.html


def test_the_record_keeps_staff_keys_never_addresses() -> None:
    record = Record.load({"sent": ["cog:a1"], "refused": ["cog:x"], "done": False})
    assert record.handled() == {"cog:a1", "cog:x"} and not record.done
    assert Record.load(None) == Record() and Record.load(json.dumps({"done": True})).done
    assert "@" not in record.dump(datetime(2031, 3, 3))


# ── The run ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_mail_per_person_and_contract_then_nothing_on_the_next_run() -> None:
    ses = FakeSes()
    conn = _conn([_row()])
    result = await _run(conn, _mail(ses))
    assert result | {"roles": None} == {
        "outcome": "done",
        "roles": None,
        "contracts": 1,
        "recipients": 2,
        "contracts_done": 1,
        "sent": 2,
        "refused": 0,
        "failed": 0,
    }
    assert sorted(m.to for m in ses.sent) == ["lead@example.test", "rates@example.test"]
    assert conn.sql("FROM booking.staff_users")[0] == (["admin", "contracting"], "supply.read")
    # S291: one write after each message, then the closing write that marks the record done.
    records = _records(conn)
    assert [(r["sent"], r["done"]) for r in records] == [
        (["cog:a1"], False),
        (["cog:a1", "cog:c1"], False),
        (["cog:a1", "cog:c1"], True),
    ]
    record = records[-1]
    assert record["sent"] == ["cog:a1", "cog:c1"] and record["done"] is True and record["refused"] == []
    # The SQL leaves out a done record; a record that is done is never mailed again even if it were read.
    again = await _run(_conn([]), _mail(ses))
    assert again["sent"] == 0 and len(ses.sent) == 2


@pytest.mark.asyncio
async def test_a_partial_record_sends_only_to_the_people_not_handled_yet() -> None:
    ses = FakeSes()
    conn = _conn([_row(mail={"sent": ["cog:a1"], "refused": [], "done": False})])
    result = await _run(conn, _mail(ses))
    assert [m.to for m in ses.sent] == ["rates@example.test"] and result["sent"] == 1
    assert _records(conn)[0]["sent"] == ["cog:a1", "cog:c1"]


@pytest.mark.asyncio
async def test_a_refused_address_is_final_a_throttled_one_is_retried() -> None:
    ses = FakeSes(
        {
            "lead@example.test": mailer.MailError("MessageRejected", "no", permanent=True),
            "rates@example.test": mailer.MailError("TooManyRequestsException", "slow", permanent=False),
        }
    )
    conn = _conn([_row()])
    result = await _run(conn, _mail(ses))
    assert result["refused"] == 1 and result["failed"] == 1 and result["contracts_done"] == 0
    record = _records(conn)[-1]
    assert record["refused"] == ["cog:a1"] and record["sent"] == [] and record["done"] is False
    with pytest.raises(contract_expiry_mail.MailFailed):
        contract_expiry_mail.raise_if_failed(result)


@pytest.mark.asyncio
async def test_a_run_that_stops_part_way_resends_only_the_people_not_mailed() -> None:
    """S291: the record is written after every send, so a run that dies after the first message (a crash, a deploy,
    a sending pause) leaves that person recorded; the next run mails only the others, and a run after that none."""

    class Crash(RuntimeError):
        pass

    ses = FakeSes()

    async def dies_on_the_second(message: mailer.Message) -> str:
        if ses.sent:
            raise Crash("the task stopped")
        return await ses(message)

    conn = _conn([_row()])
    with pytest.raises(Crash):
        await _run(conn, Mailer(dies_on_the_second, "dev", "https://booking.example.test"))
    assert [m.to for m in ses.sent] == ["lead@example.test"]
    left = _records(conn)[-1]
    assert left["sent"] == ["cog:a1"] and left["done"] is False

    # The next run reads that record (the row is still pending): only the person not mailed yet gets one.
    retry = _conn([_row(mail=left)])
    out = await _run(retry, _mail(ses))
    assert out["sent"] == 1 and [m.to for m in ses.sent] == ["lead@example.test", "rates@example.test"]
    done = _records(retry)[-1]
    assert done["sent"] == ["cog:a1", "cog:c1"] and done["done"] is True
    # Done: the pending query leaves the row out, and even a done record read again sends nobody.
    again = await _run(_conn([_row(mail=done)]), _mail(ses))
    assert again["sent"] == 0 and len(ses.sent) == 2


@pytest.mark.asyncio
async def test_a_paused_account_stops_the_run() -> None:
    ses = FakeSes({"rates@example.test": mailer.MailError("SendingPausedException", "paused", permanent=True)})
    conn = _conn([_row()])
    with pytest.raises(PermanentError):
        await _run(conn, _mail(ses))
    # the message SES took before the pause is recorded (S291): the retry after the pause does not resend it
    assert [m.to for m in ses.sent] == ["lead@example.test"] and _records(conn)[-1]["sent"] == ["cog:a1"]


@pytest.mark.asyncio
async def test_off_no_recipients_dry_run_and_not_configured_write_nothing() -> None:
    ses = FakeSes()
    off = _conn([_row()], contract_expiry_mail_roles="[]")
    assert (await _run(off, _mail(ses)))["outcome"] == "off"
    nobody = _conn([_row()], staff=[])
    assert (await _run(nobody, _mail(ses)))["outcome"] == "no_recipients"
    dry = _conn([_row()])
    assert (await _run(dry, _mail(ses), dry_run=True)) | {"roles": None} == {
        "outcome": "dry_run",
        "roles": None,
        "contracts": 1,
        "recipients": 2,
        "contracts_done": 0,
        "sent": 0,
        "refused": 0,
        "failed": 0,
        "would_send": 2,
    }
    unset = Mailer.from_settings(Settings(database_url=None), lambda _region: None)  # type: ignore[arg-type,return-value]
    assert (await _run(_conn([_row()]), unset)) == {"outcome": "skipped", "reason": "SES_FROM_ADDRESS not set"}
    assert ses.sent == [] and not any(_records(c) for c in (off, nobody, dry))


@pytest.mark.asyncio
async def test_no_roles_value_in_force_fails_the_run() -> None:
    conn = _conn([_row()])
    conn.on(POLICY_FRAGMENT, lambda *_: [r for r in policy_rows() if r["param_key"] != "contract_expiry_mail_roles"])
    with pytest.raises(LookupError):
        await _run(conn, _mail())


@pytest.mark.asyncio
async def test_the_configured_mailer_sends_through_ses_as_the_settings_sender() -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def send_email(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"MessageId": "ses-1"}

    settings = Settings(database_url=None, ses_from_address="no-reply@example.test", environment="dev")
    mail = Mailer.from_settings(settings, lambda _region: Client())
    assert mail.skip_reason is None and mail.send is not None
    assert await mail.send(mailer.Message(to="a@example.test", subject="s", text="t", html="h")) == "ses-1"
    assert calls[0]["FromEmailAddress"] == "no-reply@example.test"


# ── The job ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_job_announces_then_mails_and_reports_both() -> None:
    ses = FakeSes()
    conn = _conn([_row()])
    conn.on("FROM booking.supplier_contracts k JOIN booking.suppliers sp", lambda *_: [])
    job = Job(
        id="j1",
        kind=contract_expiry.KIND,
        payload={"day": TODAY.isoformat(), "trigger": "schedule"},
        attempt=1,
        max_attempts=3,
    )
    out = await contract_expiry.handle_watch(conn, job, _mail(ses))  # type: ignore[arg-type]
    assert out["outcome"] == "ok" and out["expiring_count"] == 0 and out["mail"]["sent"] == 2


@pytest.mark.asyncio
async def test_the_job_raises_after_recording_when_a_send_failed() -> None:
    ses = FakeSes({"rates@example.test": mailer.MailError("Throttling", "slow", permanent=False)})
    conn = _conn([_row()])
    conn.on("FROM booking.supplier_contracts k JOIN booking.suppliers sp", lambda *_: [])
    job = Job(id="j2", kind=contract_expiry.KIND, payload={"day": TODAY.isoformat()}, attempt=1, max_attempts=3)
    with pytest.raises(contract_expiry_mail.MailFailed):
        await contract_expiry.handle_watch(conn, job, _mail(ses))  # type: ignore[arg-type]
    assert _records(conn)[0]["sent"] == ["cog:a1"]


def test_build_handler_binds_the_settings() -> None:
    handler = contract_expiry.build_handler(Settings(database_url=None))
    assert callable(handler)


def test_custom_roles_are_mailed_as_their_base_role_while_they_can_read_suppliers() -> None:
    """A custom role (AA-1155) is not a choice of its own: it follows its based_on role, if not archived and still
    holding the capability the e-mail's link needs (real rows: the integration test of the same name)."""
    sql = " ".join(contract_expiry_mail.RECIPIENTS_SQL.split())
    assert "s.role = ANY($1::text[])" in sql
    assert "FROM booking.staff_role r JOIN booking.staff_role_capability c" in sql
    assert "NOT r.archived AND r.based_on = ANY($1::text[])" in sql and "c.capability = $2" in sql
    assert contract_expiry_mail.READ_CAPABILITY == "supply.read"
    assert "supply.read" in {p for p, roles in PERMISSIONS.items() if "contracting" in roles}
