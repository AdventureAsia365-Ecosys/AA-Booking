"""AA-682 S1 (bk-026) operations documents without a database or S3: the Python branches of ``app.ops.files`` (kind and
permission rules, B2B refusal, byte checks, duplicate and cap rules, write order, storage failures, audited download,
remove and the voucher link) driven through :class:`ScriptedConn` and a fake store, plus the boto3 calls of
:class:`S3OpsStore` against a fake client. The SQL itself is covered in tests/integration/test_bk026_*.py."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from typing import Any

import asyncpg
import boto3
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.ops import files as ops_files
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")

BUCKET = "aa-booking-dev-ops-docs-000000000000"
CASE_ID = "00000000-0000-4000-8000-0000000c0001"
SB_ID = "00000000-0000-4000-8000-0000000b0001"
SB2_ID = "00000000-0000-4000-8000-0000000b0002"
FILE_ID = "00000000-0000-4000-8000-000000010001"
PDF = b"%PDF-1.4\n%%EOF\n"
PDF_B64 = base64.b64encode(PDF).decode()
PDF_SHA = hashlib.sha256(PDF).hexdigest()

PARENT = "tc.code AS case_code, tc.channel FROM booking.supplier_bookings sb"
GET_FILE = "WHERE f.id = $1::uuid"
COUNTS = "AS same FROM booking.ops_files"
INSERT = "INSERT INTO booking.ops_files"
AUDIT = "INSERT INTO booking.audit_logs"
REMOVE = "UPDATE booking.ops_files SET status = 'removed'"
UNLINK_VOUCHER = "UPDATE booking.supplier_bookings SET voucher_file_id = NULL"


def file_row(**over: Any) -> dict[str, Any]:
    return {
        "id": FILE_ID,
        "kind": "supplier_voucher",
        "entity_type": "supplier_booking",
        "entity_id": SB_ID,
        "trip_case_id": CASE_ID,
        "s3_key": f"ops-docs/supplier_voucher/{FILE_ID}.pdf",
        "content_type": "application/pdf",
        "bytes": len(PDF),
        "sha256": PDF_SHA,
        "file_name": "voucher.pdf",
        "status": "active",
        "removed_reason": None,
        "removed_at": None,
        "uploaded_by": "u-1",
        "uploaded_by_name": "Sales One",
        "created_at": None,
        **over,
    }


def parent_row(**over: Any) -> dict[str, Any]:
    return {
        "id": SB_ID,
        "code": "SB-000001",
        "status": "requested",
        "trip_case_id": CASE_ID,
        "voucher_file_id": None,
        "case_code": "TC-000001",
        "channel": "b2c",
        **over,
    }


class FakeStore:
    bucket = BUCKET

    def __init__(self, conn: ScriptedConn | None = None, fail: Exception | None = None) -> None:
        self.conn = conn
        self.fail = fail
        self.puts: list[tuple[Any, ...]] = []
        self.audited_before_put: list[bool] = []
        self.urls: list[tuple[str, str, str]] = []

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        if self.conn is not None:
            self.audited_before_put.append(bool(self.conn.sql(INSERT)) and bool(self.conn.sql(AUDIT)))
        if self.fail:
            raise self.fail
        self.puts.append((key, data, content_type, sha256))

    async def download_url(self, key: str, file_name: str, content_type: str) -> str:
        if self.fail:
            raise self.fail
        self.urls.append((key, file_name, content_type))
        return "https://example.test/signed"


def audits(conn: ScriptedConn) -> list[dict[str, Any]]:
    keys = ("actor_type", "actor_id", "actor_ref", "action", "entity_type", "entity_id", "before", "after",
            "entity_label", "summary")  # fmt: skip
    out = []
    for args in conn.sql(AUDIT):
        row = dict(zip(keys, args, strict=True))
        row["before"] = json.loads(row["before"]) if row["before"] else None
        row["after"] = json.loads(row["after"]) if row["after"] else None
        out.append(row)
    return out


def upload_conn(**rules: Any) -> ScriptedConn:
    base: dict[str, Any] = {
        PARENT: parent_row(),
        COUNTS: {"active": 0, "same": 0},
        GET_FILE: lambda fid: file_row(id=fid),
    }
    base.update(rules)
    return ScriptedConn(list(base.items()))


def upload_args(**over: Any) -> dict[str, Any]:
    return {
        "kind": "supplier_voucher",
        "entity_type": "supplier_booking",
        "entity_id": SB_ID,
        "file_name": " vouchers/Delta.pdf ",
        "content_type": "application/pdf",
        "data_base64": PDF_B64,
        **over,
    }


# ── Rows ─────────────────────────────────────────────────────────────────────


def test_file_out_hides_the_key_and_nests_the_uploader() -> None:
    out = ops_files.file_out(file_row())
    assert "s3_key" not in out and "uploaded_by_name" not in out
    assert out["uploaded_by"] == {"id": "u-1", "name": "Sales One"}
    assert ops_files.file_out(file_row(uploaded_by=None, uploaded_by_name=None))["uploaded_by"] is None


@pytest.mark.asyncio
async def test_files_of_groups_active_files_by_entity() -> None:
    conn = ScriptedConn()
    assert await ops_files.files_of(conn, "supplier_booking", []) == {}
    assert conn.calls == []  # no ids, no query

    rows = [file_row(id="f-1"), file_row(id="f-2", entity_id=SB2_ID), file_row(id="f-3")]
    conn = ScriptedConn([("f.entity_id = ANY($2::uuid[])", rows)])
    out = await ops_files.files_of(conn, "supplier_booking", [SB_ID, SB2_ID])
    assert {k: [f["id"] for f in v] for k, v in out.items()} == {SB_ID: ["f-1", "f-3"], SB2_ID: ["f-2"]}
    assert conn.sql("f.status = 'active'") == [("supplier_booking", [SB_ID, SB2_ID])]


@pytest.mark.asyncio
async def test_get_file_not_found_and_lock() -> None:
    conn = ScriptedConn()
    with pytest.raises(NotFound) as exc:
        await ops_files.get_file(conn, "voucher.pdf")
    assert exc.value.code == "file_not_found" and conn.calls == []
    with pytest.raises(NotFound):
        await ops_files.get_file(conn, FILE_ID)
    conn = ScriptedConn([(GET_FILE, file_row())])
    assert (await ops_files.get_file(conn, FILE_ID.upper(), lock=True))["id"] == FILE_ID
    assert conn.calls[0][0].endswith("FOR UPDATE OF f") and conn.calls[0][1] == (FILE_ID,)


def test_files_inherit_the_permission_of_their_parent() -> None:
    ops_files._require(SALES, "supplier_booking", write=True)
    ops_files._require(SALES, "supplier_booking", write=False)
    for staff, entity_type, write in ((CONTENT, "supplier_booking", False), (CONTENT, "supplier_booking", True),
                                      (ADMIN, "supplier_bill", False)):  # fmt: skip
        with pytest.raises(Forbidden):
            ops_files._require(staff, entity_type, write=write)


@pytest.mark.asyncio
async def test_parent_kinds_ids_and_not_found() -> None:
    conn = ScriptedConn()
    with pytest.raises(Invalid) as exc:
        await ops_files._parent(conn, "payment", SB_ID, lock=False)
    assert exc.value.code == "kind_not_available"
    with pytest.raises(NotFound) as exc:
        await ops_files._parent(conn, "supplier_booking", "SB-000001", lock=False)
    assert exc.value.code == "supplier_booking_not_found" and conn.calls == []
    with pytest.raises(NotFound):
        await ops_files._parent(conn, "supplier_booking", SB_ID, lock=True)
    assert conn.calls[0][0].endswith("FOR UPDATE OF sb")


# ── Upload ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_writes_the_row_and_audit_then_puts_the_object() -> None:
    conn = upload_conn()
    store = FakeStore(conn)
    out = await ops_files.upload(conn, store, SALES, **upload_args())
    (args,) = conn.sql(INSERT)
    file_id = args[0]
    assert uuid.UUID(file_id)
    key = f"ops-docs/supplier_voucher/{file_id}.pdf"
    assert args[1:] == ("supplier_voucher", "supplier_booking", SB_ID, CASE_ID, BUCKET, key, "application/pdf",
                        len(PDF), PDF_SHA, "vouchers Delta.pdf", "u-1")  # fmt: skip
    assert store.puts == [(key, PDF, "application/pdf", PDF_SHA)]
    assert store.audited_before_put == [True]  # the row and the audit row first: a failed put rolls them back
    (row,) = audits(conn)
    assert row["action"] == "ops_file.uploaded" and row["entity_label"] == "TC-000001 SB-000001"
    assert row["after"] == {"file_id": file_id, "kind": "supplier_voucher", "content_type": "application/pdf",
                            "bytes": len(PDF), "sha256": PDF_SHA}  # fmt: skip
    assert out["id"] == file_id and "s3_key" not in out
    assert conn.sql(PARENT)[0] == (SB_ID,) and "FOR UPDATE OF sb" in conn.calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("staff", "rules", "args", "error", "code"),
    [
        (SALES, {}, {"kind": "invoice"}, Invalid, "unknown_kind"),
        (SALES, {}, {"kind": "payment_evidence"}, Invalid, "kind_not_available"),
        (SALES, {}, {"entity_type": "trip_case"}, Invalid, "kind_entity_mismatch"),
        (CONTENT, {}, {}, Forbidden, "forbidden"),
        (SALES, {}, {"entity_id": "SB-000001"}, NotFound, "supplier_booking_not_found"),
        (SALES, {PARENT: None}, {}, NotFound, "supplier_booking_not_found"),
        (SALES, {PARENT: parent_row(channel="b2b")}, {}, Invalid, "document_not_allowed_b2b"),
        (SALES, {PARENT: parent_row(status="declined")}, {}, Conflict, "supplier_booking_closed"),
        (SALES, {PARENT: parent_row(status="cancelled")}, {}, Conflict, "supplier_booking_closed"),
        (SALES, {}, {"file_name": "  /  "}, Invalid, "file_name_required"),
        (SALES, {}, {"data_base64": "not base64!"}, Invalid, "invalid_base64"),
        (SALES, {}, {"data_base64": base64.b64encode(b"MZ\x90\x00 exe").decode()}, Invalid, "unsupported_file"),
        (SALES, {}, {"content_type": "image/png"}, Invalid, "content_type_mismatch"),
        (SALES, {COUNTS: {"active": 1, "same": 1}}, {}, Conflict, "file_exists"),
        (SALES, {COUNTS: {"active": ops_files.MAX_ACTIVE_PER_ENTITY, "same": 0}}, {}, Conflict, "too_many_files"),
        (SALES, {INSERT: asyncpg.UniqueViolationError("ops_files_entity_sha_uniq")}, {}, Conflict, "file_exists"),
    ],
)
async def test_upload_refusals(staff: Staff, rules: dict, args: dict, error: type[Exception], code: str) -> None:
    conn = upload_conn(**rules)
    store = FakeStore(conn)
    with pytest.raises(error) as exc:
        await ops_files.upload(conn, store, staff, **upload_args(**args))
    assert exc.value.code == code
    assert store.puts == [] and store.audited_before_put == [] and conn.sql(AUDIT) == []
    if code == "content_type_mismatch":
        assert exc.value.extra == {"detected": "application/pdf"}


@pytest.mark.asyncio
async def test_upload_without_a_bucket_or_with_a_failing_put_is_unavailable() -> None:
    conn = upload_conn()
    with pytest.raises(Unavailable) as exc:
        await ops_files.upload(conn, None, SALES, **upload_args())
    assert exc.value.code == "ops_storage_unavailable" and conn.sql(INSERT) == []

    conn = upload_conn()
    store = FakeStore(conn, fail=RuntimeError("SlowDown"))
    with pytest.raises(Unavailable) as exc:
        await ops_files.upload(conn, store, SALES, **upload_args())
    assert exc.value.code == "ops_storage_unavailable"
    assert store.audited_before_put == [True] and store.puts == []  # the caller's transaction rolls the row back
    assert conn.sql(GET_FILE) == []


# ── Download and remove ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_is_a_presigned_link_and_an_audit_row() -> None:
    conn = ScriptedConn([(GET_FILE, file_row())])
    store = FakeStore()
    out = await ops_files.download(conn, store, SALES, FILE_ID)
    assert out == {"url": "https://example.test/signed", "expires_in": ops_files.PRESIGN_SECONDS}
    assert store.urls == [(f"ops-docs/supplier_voucher/{FILE_ID}.pdf", "voucher.pdf", "application/pdf")]
    (row,) = audits(conn)
    assert row["action"] == "ops_file.downloaded" and row["actor_ref"] == "u-1"
    assert row["after"] == {"file_id": FILE_ID, "kind": "supplier_voucher"}


@pytest.mark.asyncio
async def test_download_refusals() -> None:
    with pytest.raises(Forbidden):
        await ops_files.download(ScriptedConn([(GET_FILE, file_row())]), FakeStore(), CONTENT, FILE_ID)
    with pytest.raises(Conflict) as exc:
        await ops_files.download(ScriptedConn([(GET_FILE, file_row(status="removed"))]), FakeStore(), SALES, FILE_ID)
    assert exc.value.code == "file_removed"
    with pytest.raises(Unavailable):
        await ops_files.download(ScriptedConn([(GET_FILE, file_row())]), None, SALES, FILE_ID)
    conn = ScriptedConn([(GET_FILE, file_row())])
    with pytest.raises(Unavailable) as exc:
        await ops_files.download(conn, FakeStore(fail=RuntimeError("no creds")), SALES, FILE_ID)
    assert exc.value.code == "ops_storage_unavailable" and conn.sql(AUDIT) == []  # no link, no download audited


@pytest.mark.asyncio
async def test_remove_a_voucher_unlinks_it_from_its_booking() -> None:
    conn = ScriptedConn([(GET_FILE, file_row()), (PARENT, parent_row(status="confirmed", voucher_file_id=FILE_ID))])
    await ops_files.remove(conn, SALES, FILE_ID, "  wrong hotel " + "x" * 600)
    (removed,) = conn.sql(REMOVE)
    assert removed[0] == FILE_ID and removed[1].startswith("wrong hotel") and len(removed[1]) == 500
    assert removed[2] == "u-1"
    assert conn.sql(UNLINK_VOUCHER) == [(SB_ID, "u-1")]
    (row,) = audits(conn)
    assert row["action"] == "ops_file.removed" and row["entity_label"] == "TC-000001 SB-000001"
    assert row["before"] == {"file_id": FILE_ID, "status": "active"}
    assert row["after"]["status"] == "removed"


@pytest.mark.asyncio
async def test_remove_another_file_keeps_the_voucher_and_removing_twice_is_a_no_op() -> None:
    other = "00000000-0000-4000-8000-000000010002"
    conn = ScriptedConn([(GET_FILE, file_row()), (PARENT, parent_row(voucher_file_id=other))])
    await ops_files.remove(conn, SALES, FILE_ID, "duplicate")
    assert len(conn.sql(REMOVE)) == 1 and conn.sql(UNLINK_VOUCHER) == []

    conn = ScriptedConn([(GET_FILE, file_row(status="removed", removed_reason="duplicate"))])
    out = await ops_files.remove(conn, SALES, FILE_ID, "again")
    assert out["status"] == "removed" and conn.sql(REMOVE) == [] and conn.sql(AUDIT) == []


@pytest.mark.asyncio
async def test_remove_refusals() -> None:
    conn = ScriptedConn([(GET_FILE, file_row())])
    with pytest.raises(Invalid) as exc:
        await ops_files.remove(conn, SALES, FILE_ID, "   ")
    assert exc.value.code == "reason_required" and conn.calls == []
    with pytest.raises(Forbidden):
        await ops_files.remove(conn, CONTENT, FILE_ID, "x")
    assert conn.sql(REMOVE) == []


# ── The S3 store ─────────────────────────────────────────────────────────────


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.presigns: list[tuple[str, dict[str, Any], int]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)

    def generate_presigned_url(self, method: str, *, Params: dict[str, Any], ExpiresIn: int) -> str:  # noqa: N803
        self.presigns.append((method, Params, ExpiresIn))
        return "https://s3.example.test/presigned"


@pytest.mark.asyncio
async def test_s3_store_puts_with_kms_and_checksum_and_presigns_an_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    made: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def client(*args: Any, **kwargs: Any) -> FakeS3:
        made.append((args, kwargs))
        return fake

    monkeypatch.setattr(boto3, "client", client)
    store = ops_files.store_from_settings(Settings(database_url=None, ops_docs_bucket=BUCKET))
    assert isinstance(store, ops_files.S3OpsStore) and store.bucket == BUCKET

    key = f"ops-docs/supplier_voucher/{FILE_ID}.pdf"
    await store.put(key, PDF, "application/pdf", PDF_SHA)
    url = await store.download_url(key, 'Voucher "Delta".pdf', "application/pdf")

    assert len(made) == 1  # one client, made lazily and reused
    (args, kwargs) = made[0]
    assert args == ("s3",) and kwargs["region_name"] == store.region
    assert kwargs["config"].signature_version == "s3v4"
    assert fake.puts == [
        {
            "Bucket": BUCKET,
            "Key": key,
            "Body": PDF,
            "ContentType": "application/pdf",
            "ServerSideEncryption": "aws:kms",
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(PDF).digest()).decode("ascii"),
        }
    ]
    assert url == "https://s3.example.test/presigned"
    assert fake.presigns == [
        (
            "get_object",
            {
                "Bucket": BUCKET,
                "Key": key,
                "ResponseContentDisposition": "attachment; filename=\"Voucher 'Delta'.pdf\"",
                "ResponseContentType": "application/pdf",
            },
            ops_files.PRESIGN_SECONDS,
        )
    ]
