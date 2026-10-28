"""Operations documents (bk-026 ``booking.ops_files``; AA-682 design D10): supplier vouchers and confirmations
(S1), payment evidence (S3, bk-028), supplier bill documents and payout evidence (S4, bk-030).

- **Private bucket, SSE-S3.** The bytes go to the ops-docs bucket (``OPS_DOCS_BUCKET``, AA-CIS-Infra
  ``booking_ops_docs.tf``: SSE-S3 by default, a put naming another algorithm is denied, and every object verb is
  denied to any principal but this app's task role) under the key bk-026 derives from the row,
  ``ops-docs/<kind>/<id>.<ext>``. Evidence and vouchers may hold bank references and traveller names
  (``pii:payment``), so nothing is public: staff read a file through a 300 s presigned GET that forces a download
  (``Content-Disposition: attachment``, the stored content type), never inline in the admin, and every download writes
  an audit row. (The S252 design asked for a KMS CMK; a new paid key waits for its own approval, and the bucket
  policy keeps the other roles of the account out instead.)
- **Upload body is JSON with the bytes in base64** (the AA-835 path, lessons S229: the Booking REST API has no
  ``binaryMediaTypes``), capped at :data:`MAX_BYTES` (3 MiB) by the staff BFF's 4.5 MB Vercel body limit; the database
  allows up to ``ops_docs_max_bytes`` (10 MB) for a later presigned-POST path (design Q10).
- **The type is read from the bytes**: PDF (``%PDF-``), JPEG, PNG or WebP; the declared type must agree. PDFs are stored
  as uploaded (no re-encode); the staff UI re-encodes photos before sending them (no EXIF).
- **Order of a write** (as AA-835): the row and the audit row, then the S3 put, inside the caller's transaction; a
  failed put rolls the row back.
- **B2B cases take no document** (design D12, DM-6.8: a voucher or a supplier confirmation names travellers of an agency
  trip, which never enter ``booking.*``): 422 ``document_not_allowed_b2b``; bk-026 refuses it too.

- **Payment evidence** (bk-028, critic note 6): record the payment first, then upload with the payment as entity; the
  upload makes the file the payment's ``evidence_file_id`` in the same transaction (the evidence a verifier needs).
  Only while the payment is not verified or voided; the evidence of a verified payment is never removed (409
  ``evidence_locked``, refused by bk-028 too). Removing the current evidence of a recorded payment detaches it first
  (``evidence_file_id`` NULL, so the payment needs new evidence before it verifies); bk-028 refuses removing a file a
  non-voided payment still points at, and re-checks at verify that the evidence is an active file.

- **Supplier bill documents** (bk-030): the supplier's PDF, uploaded with the bill as entity while the bill is
  ``received`` (it is what the approver checks); the newest becomes the bill's ``document_file_id``. Never on a bill
  that touches a B2B case (its case or any line's case: a hotel bill names the guests, DM-6.8): 422
  ``document_not_allowed_b2b``. The document of an approved / paid bill stays (409 ``document_locked``); a received
  bill's attached document is detached before its removal.
- **Payout evidence** (bk-030): AA's bank slip, uploaded with the payout as entity while it is live (allowed on B2B
  cases: it names no traveller); the newest becomes ``evidence_file_id``. The evidence a live payout points at is
  replaced, never removed (409 ``evidence_locked``).

Permissions: a file inherits the permission of its parent (supplier booking: ``ops.read`` / ``ops.write``; payment:
``billing.read`` / ``billing.write``; supplier bill: ``bill.read`` / ``bill.write``; payout: ``bill.read`` /
``payout.write``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.settings import Settings
from app.supply import media

log = logging.getLogger(__name__)

# The staff BFF on Vercel takes at most 4.5 MB of body; 3 MiB of bytes is 4 MiB of base64 (same cap as AA-835).
MAX_BYTES = 3 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_BYTES + 2) // 3)
PRESIGN_SECONDS = 300
OBJECT_PREFIX = "ops-docs"
EXTENSIONS = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
CONTENT_TYPES = tuple(EXTENSIONS)
KINDS = ("payment_evidence", "supplier_voucher", "supplier_confirmation", "supplier_bill", "payout_evidence")
# The entity each kind belongs to (bk-026 vouchers / confirmations, bk-028 payment evidence, bk-030 bills / payouts).
KIND_ENTITY = {
    "supplier_voucher": "supplier_booking",
    "supplier_confirmation": "supplier_booking",
    "payment_evidence": "payment",
    "supplier_bill": "supplier_bill",
    "payout_evidence": "supplier_payout",
}
PERMISSIONS = {
    "supplier_booking": ("ops.read", "ops.write"),
    "payment": ("billing.read", "billing.write"),
    "supplier_bill": ("bill.read", "bill.write"),
    "supplier_payout": ("bill.read", "payout.write"),
}
NOT_FOUND = {
    "supplier_booking": "supplier_booking_not_found",
    "payment": "payment_not_found",
    "supplier_bill": "supplier_bill_not_found",
    "supplier_payout": "supplier_payout_not_found",
}
# The column holding the parent's current file, set by an upload (the newest wins) and cleared before a removal.
CURRENT_FILE = {
    "payment": "payments SET evidence_file_id",
    "supplier_bill": "supplier_bills SET document_file_id",
    "supplier_payout": "supplier_payouts SET evidence_file_id",
}
MAX_ACTIVE_PER_ENTITY = 20
MAX_NAME = 200
# Same bounds as the AA-835 photo store: a slow S3 fails before API Gateway's 29 s.
S3_CONNECT_TIMEOUT = media.S3_CONNECT_TIMEOUT
S3_READ_TIMEOUT = media.S3_READ_TIMEOUT
S3_TOTAL_ATTEMPTS = media.S3_TOTAL_ATTEMPTS


# ── Bytes ────────────────────────────────────────────────────────────────────


def decode(data_base64: str) -> bytes:
    """The uploaded bytes: strict base64, 1 byte to :data:`MAX_BYTES`."""
    if len(data_base64) > MAX_BASE64_CHARS:
        raise Invalid("file_too_large", f"a document is at most {MAX_BYTES // (1024 * 1024)} MB")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Invalid("invalid_base64", "data_base64 is not valid base64") from exc
    if not data:
        raise Invalid("empty_file", "the file is empty")
    if len(data) > MAX_BYTES:
        raise Invalid("file_too_large", f"a document is at most {MAX_BYTES // (1024 * 1024)} MB")
    return data


def sniff(data: bytes) -> str:
    """The content type read from the bytes: a PDF, or a JPEG / PNG / WebP image; 422 ``unsupported_file`` else."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    try:
        return media.sniff(data).content_type
    except Invalid as exc:
        if exc.code in ("unsupported_image",):
            raise Invalid("unsupported_file", "upload a PDF, JPEG, PNG or WebP file") from exc
        raise


def object_key(kind: str, file_id: str, content_type: str) -> str:
    """The key bk-026's ``ops_files_key_chk`` derives from the row."""
    return f"{OBJECT_PREFIX}/{kind}/{file_id}.{EXTENSIONS[content_type]}"


def clean_name(file_name: str) -> str:
    name = " ".join((file_name or "").replace("/", " ").replace("\\", " ").split())[:MAX_NAME]
    if not name:
        raise Invalid("file_name_required", "the file needs a name")
    return name


# ── Storage ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Download:
    url: str
    expires_in: int


class OpsStore(Protocol):
    bucket: str

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None: ...

    async def download_url(self, key: str, file_name: str, content_type: str) -> str: ...


def _disposition(file_name: str) -> str:
    ascii_name = file_name.encode("ascii", "replace").decode("ascii").replace('"', "'").replace("?", "_")
    return f'attachment; filename="{ascii_name}"'


class S3OpsStore:
    """The ops-docs bucket through boto3 (blocking calls in a thread; boto3 imported lazily, as the photo store)."""

    def __init__(self, bucket: str, region: str) -> None:
        self.bucket = bucket
        self.region = region
        self._client: Any = None
        self._lock = threading.Lock()

    def _s3(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                self._client = boto3.client(
                    "s3",
                    region_name=self.region,
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "virtual"},
                        connect_timeout=S3_CONNECT_TIMEOUT,
                        read_timeout=S3_READ_TIMEOUT,
                        retries={"total_max_attempts": S3_TOTAL_ATTEMPTS},
                    ),
                )
            return self._client

    def _put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self._s3().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",  # the bucket default (SSE-S3); the bucket policy refuses any other
            ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode("ascii"),
        )

    def _url(self, key: str, file_name: str, content_type: str) -> str:
        return self._s3().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": _disposition(file_name),
                "ResponseContentType": content_type,
            },
            ExpiresIn=PRESIGN_SECONDS,
        )

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        await asyncio.to_thread(self._put, key, data, content_type, sha256)

    async def download_url(self, key: str, file_name: str, content_type: str) -> str:
        return await asyncio.to_thread(self._url, key, file_name, content_type)


def store_from_settings(settings: Settings) -> OpsStore | None:
    """The bucket of ``OPS_DOCS_BUCKET``; None when it is not set (uploads and downloads then answer 503)."""
    if not settings.ops_docs_bucket:
        return None
    return S3OpsStore(settings.ops_docs_bucket, settings.ops_docs_region)


# ── Rows ─────────────────────────────────────────────────────────────────────

FILE_COLUMNS = """
    f.id::text AS id, f.kind, f.entity_type, f.entity_id::text AS entity_id, f.trip_case_id::text AS trip_case_id,
    f.s3_key, f.content_type, f.bytes, f.sha256, f.file_name, f.status, f.removed_reason, f.removed_at,
    f.uploaded_by, up.name AS uploaded_by_name, f.created_at
"""
FILE_FROM = "booking.ops_files f LEFT JOIN booking.staff_users up ON up.cis_user_id = f.uploaded_by"


PARENT_SQL = """
SELECT sb.id::text AS id, sb.code, sb.status, sb.trip_case_id::text AS trip_case_id,
       sb.voucher_file_id::text AS voucher_file_id, tc.code AS case_code, tc.channel
FROM booking.supplier_bookings sb JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id
WHERE sb.id = $1::uuid"""
PAYMENT_PARENT_SQL = """
SELECT p.id::text AS id, p.ref AS code,
       CASE WHEN p.voided_at IS NOT NULL THEN 'voided' WHEN p.verified_at IS NOT NULL THEN 'verified'
            ELSE 'recorded' END AS status,
       i.trip_case_id::text AS trip_case_id, p.evidence_file_id::text AS voucher_file_id, tc.code AS case_code,
       tc.channel, i.id::text AS invoice_id, p.verified_at IS NOT NULL AS verified
FROM booking.payments p
JOIN booking.invoices i ON i.id = p.invoice_id
JOIN booking.trip_cases tc ON tc.id = i.trip_case_id
WHERE p.id = $1::uuid"""
# bk-030: a bill's "voucher_file_id" is its document; channel 'b2b' when the bill touches a B2B case (its own case or
# the case of any line), the DM-6.8 rule of ops_files_guard.
BILL_PARENT_SQL = """
SELECT b.id::text AS id, b.code, b.status, b.trip_case_id::text AS trip_case_id,
       b.document_file_id::text AS voucher_file_id, coalesce(tc.code, '-') AS case_code,
       CASE WHEN tc.channel = 'b2b' OR EXISTS (
                SELECT 1 FROM booking.supplier_bill_items it
                  JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
                  JOIN booking.trip_cases c2 ON c2.id = sb.trip_case_id
                 WHERE it.bill_id = b.id AND c2.channel = 'b2b') THEN 'b2b' ELSE 'b2c' END AS channel
FROM booking.supplier_bills b LEFT JOIN booking.trip_cases tc ON tc.id = b.trip_case_id
WHERE b.id = $1::uuid"""
# bk-030: a payout's "voucher_file_id" is its evidence; status live / voided; its bank slip is allowed on B2B cases.
PAYOUT_PARENT_SQL = """
SELECT o.id::text AS id, o.code, CASE WHEN o.voided_at IS NOT NULL THEN 'voided' ELSE 'live' END AS status,
       b.trip_case_id::text AS trip_case_id, o.evidence_file_id::text AS voucher_file_id,
       coalesce(tc.code, '-') AS case_code, 'b2c' AS channel, b.id::text AS bill_id, b.code AS bill_code
FROM booking.supplier_payouts o
JOIN booking.supplier_bills b ON b.id = o.bill_id
LEFT JOIN booking.trip_cases tc ON tc.id = b.trip_case_id
WHERE o.id = $1::uuid"""


def file_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out.pop("s3_key", None)
    out["uploaded_by"] = (
        {"id": out["uploaded_by"], "name": out.pop("uploaded_by_name")}
        if out["uploaded_by"]
        else out.pop("uploaded_by_name")
    )
    return out


async def files_of(conn: asyncpg.Connection, entity_type: str, entity_ids: list[str]) -> dict[str, list[dict]]:
    """Active files per entity id (no URL: a download is its own audited call)."""
    if not entity_ids:
        return {}
    rows = await conn.fetch(
        f"SELECT {FILE_COLUMNS} FROM {FILE_FROM}"  # noqa: S608 — constants only
        " WHERE f.entity_type = $1 AND f.entity_id = ANY($2::uuid[]) AND f.status = 'active'"
        " ORDER BY f.created_at, f.id",
        entity_type,
        entity_ids,
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append(file_out(r))
    return out


async def get_file(conn: asyncpg.Connection, file_id: str, *, lock: bool = False) -> Any:
    try:
        fid = str(uuid.UUID(file_id))
    except ValueError:
        raise NotFound("file_not_found", f"file {file_id} not found") from None
    row = await conn.fetchrow(
        f"SELECT {FILE_COLUMNS} FROM {FILE_FROM} WHERE f.id = $1::uuid"  # noqa: S608 — constants only
        + (" FOR UPDATE OF f" if lock else ""),
        fid,
    )
    if row is None:
        raise NotFound("file_not_found", f"file {file_id} not found")
    return row


def _require(staff: Staff, entity_type: str, *, write: bool) -> None:
    pair = PERMISSIONS.get(entity_type)
    permission = pair[1 if write else 0] if pair else None
    if permission is None or not staff.can(permission):
        raise Forbidden("forbidden", f"{'writing' if write else 'reading'} this file needs {permission or 'a role'}")


async def _parent(conn: asyncpg.Connection, entity_type: str, entity_id: str, *, lock: bool) -> Any:
    """The supplier booking, payment, supplier bill or payout a file belongs to, with its case channel (404 when it
    does not exist). The ``voucher_file_id`` column is the parent's current file (voucher, payment evidence, bill
    document, payout evidence). A payment is locked in the money order (case, invoice, payment; critic B4), a payout
    in the bill order (bill, payout)."""
    if entity_type not in PERMISSIONS:
        raise Invalid("unknown_entity", f"no file belongs to a {entity_type}")
    missing = NOT_FOUND[entity_type]
    label = entity_type.replace("_", " ")
    try:
        ident = str(uuid.UUID(entity_id))
    except ValueError:
        raise NotFound(missing, f"{label} {entity_id} not found") from None
    if entity_type == "payment":
        row = await conn.fetchrow(PAYMENT_PARENT_SQL, ident)
        if row is not None and lock:
            await conn.execute("SELECT 1 FROM booking.trip_cases WHERE id = $1::uuid FOR UPDATE", row["trip_case_id"])
            await conn.execute("SELECT 1 FROM booking.invoices WHERE id = $1::uuid FOR UPDATE", row["invoice_id"])
            row = await conn.fetchrow(PAYMENT_PARENT_SQL + " FOR UPDATE OF p", ident)
    elif entity_type == "supplier_bill":
        row = await conn.fetchrow(BILL_PARENT_SQL + (" FOR UPDATE OF b" if lock else ""), ident)
    elif entity_type == "supplier_payout":
        row = await conn.fetchrow(PAYOUT_PARENT_SQL, ident)
        if row is not None and lock:
            await conn.execute("SELECT 1 FROM booking.supplier_bills WHERE id = $1::uuid FOR UPDATE", row["bill_id"])
            row = await conn.fetchrow(PAYOUT_PARENT_SQL + " FOR UPDATE OF o", ident)
    else:
        row = await conn.fetchrow(PARENT_SQL + (" FOR UPDATE OF sb" if lock else ""), ident)
    if row is None:
        raise NotFound(missing, f"{label} {entity_id} not found")
    return row


async def upload(
    conn: asyncpg.Connection,
    store: OpsStore | None,
    staff: Staff,
    *,
    kind: str,
    entity_type: str,
    entity_id: str,
    file_name: str,
    content_type: str,
    data_base64: str,
) -> dict[str, Any]:
    """Check the bytes, write the row (+ audit), then put the object; run inside the caller's transaction."""
    if kind not in KINDS:
        raise Invalid("unknown_kind", f"kind must be one of {', '.join(KINDS)}")
    if KIND_ENTITY[kind] != entity_type:
        raise Invalid("kind_entity_mismatch", f"a {kind} file belongs to a {KIND_ENTITY[kind]}")
    _require(staff, entity_type, write=True)
    parent = await _parent(conn, entity_type, entity_id, lock=True)
    if parent["channel"] == "b2b":
        raise Invalid(
            "document_not_allowed_b2b",
            "a B2B trip keeps the supplier's confirmation ref only: no voucher or document (traveller data stays in the"
            " agency's CRM)",
        )
    if entity_type == "supplier_bill":
        if parent["status"] != "received":
            raise Conflict(
                "supplier_bill_closed", f"supplier bill {parent['code']} is {parent['status']}: its document is fixed"
            )
    elif entity_type == "supplier_payout":
        if parent["status"] == "voided":
            raise Conflict("payout_closed", f"payout {parent['code']} is voided: its evidence is fixed")
    elif parent["status"] in ("declined", "cancelled"):
        raise Conflict("supplier_booking_closed", f"supplier booking {parent['code']} is {parent['status']}")
    elif parent["status"] in ("verified", "voided"):
        raise Conflict("payment_closed", f"payment {parent['code']} is {parent['status']}: its evidence is fixed")
    if store is None:
        raise Unavailable("ops_storage_unavailable", "document storage is not configured for this environment")
    name = clean_name(file_name)
    data = decode(data_base64)
    detected = sniff(data)
    if content_type != detected:
        raise Invalid("content_type_mismatch", f"the file is {detected}, not {content_type}", detected=detected)
    sha256 = hashlib.sha256(data).hexdigest()
    counts = await conn.fetchrow(
        "SELECT count(*) AS active, count(*) FILTER (WHERE sha256 = $3) AS same FROM booking.ops_files"
        " WHERE entity_type = $1 AND entity_id = $2::uuid AND status = 'active'",
        entity_type,
        parent["id"],
        sha256,
    )
    if counts["same"]:
        raise Conflict("file_exists", f"this file is already on {parent['code']}")
    if counts["active"] >= MAX_ACTIVE_PER_ENTITY:
        raise Conflict("too_many_files", f"{parent['code']} has {MAX_ACTIVE_PER_ENTITY} files; remove one first")
    file_id = str(uuid.uuid4())
    key = object_key(kind, file_id, detected)
    try:
        await conn.execute(
            "INSERT INTO booking.ops_files (id, kind, entity_type, entity_id, trip_case_id, s3_bucket, s3_key,"
            " content_type, bytes, sha256, file_name, uploaded_by)"
            " VALUES ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6, $7, $8, $9, $10, $11, $12)",
            file_id,
            kind,
            entity_type,
            parent["id"],
            parent["trip_case_id"],
            store.bucket,
            key,
            detected,
            len(data),
            sha256,
            name,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:  # a concurrent upload of the same bytes
        raise Conflict("file_exists", f"this file is already on {parent['code']}") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="ops_file.uploaded",
        entity_type=entity_type,
        entity_id=parent["id"],
        after={"file_id": file_id, "kind": kind, "content_type": detected, "bytes": len(data), "sha256": sha256},
        entity_label=f"{parent['case_code']} {parent['code']}",
        summary=f"Added a {kind.replace('_', ' ')} ({detected}, {len(data)} bytes)",
    )
    if entity_type in CURRENT_FILE:
        # The newest file is the parent's current one (critic note 6): payment / payout evidence, bill document; the
        # guards check it is an active file of that parent and kind.
        await conn.execute(
            f"UPDATE booking.{CURRENT_FILE[entity_type]} = $2::uuid, updated_by = $3 WHERE id = $1::uuid",  # noqa: S608
            parent["id"],
            file_id,
            staff.cis_user_id,
        )
    try:
        await store.put(key, data, detected, sha256)
    except Exception as exc:  # noqa: BLE001 — any storage failure rolls the row back (the caller's transaction)
        log.warning("ops_file_put_failed kind=%s error=%s", kind, type(exc).__name__)
        raise Unavailable("ops_storage_unavailable", "the file could not be stored; try again") from exc
    return file_out(await get_file(conn, file_id))


async def download(conn: asyncpg.Connection, store: OpsStore | None, staff: Staff, file_id: str) -> dict[str, Any]:
    """A 300 s presigned GET (attachment) of an active file, audited ``ops_file.downloaded``."""
    row = await get_file(conn, file_id)
    _require(staff, row["entity_type"], write=False)
    if row["status"] != "active":
        raise Conflict("file_removed", "this file was removed")
    if store is None:
        raise Unavailable("ops_storage_unavailable", "document storage is not configured for this environment")
    try:
        url = await store.download_url(row["s3_key"], row["file_name"], row["content_type"])
    except Exception as exc:  # noqa: BLE001
        log.warning("ops_file_presign_failed error=%s", type(exc).__name__)
        raise Unavailable("ops_storage_unavailable", "the download link could not be made; try again") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="ops_file.downloaded",
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        after={"file_id": row["id"], "kind": row["kind"]},
        entity_label=f"file {row['id'][:8]}",
        summary=f"Downloaded a {row['kind'].replace('_', ' ')}",
    )
    return {"url": url, "expires_in": PRESIGN_SECONDS}


# Parents whose status is final: their current-file pointer is never written again (the DB refuses it), so removing
# their file leaves the pointer. Every other parent (a supplier booking in any status, cancelled included) detaches.
FINAL_PARENT: dict[str, tuple[str, ...]] = {
    "payment": ("voided",),
    "supplier_payout": ("voided",),
    "supplier_bill": ("cancelled",),
}


async def remove(conn: asyncpg.Connection, staff: Staff, file_id: str, reason: str) -> dict[str, Any]:
    """Remove a file (final; the object stays until retention). A removed voucher or payment evidence leaves its
    parent first.

    Lock order: the parent (case, invoice, payment; or the supplier booking), THEN the file, the order of every other
    money write: a payment verify locks case, invoice, payment and bk-028 takes its evidence file FOR SHARE, so locking
    the file first here could deadlock with it. The evidence a non-voided payment points at is detached before the
    file is removed (bk-028 refuses removing attached evidence; review S253)."""
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the file is removed")
    first = await get_file(conn, file_id)
    _require(staff, first["entity_type"], write=True)
    if first["status"] == "removed":
        return file_out(first)
    parent = await _parent(conn, first["entity_type"], first["entity_id"], lock=True)
    row = await get_file(conn, first["id"], lock=True)
    if row["status"] == "removed":  # removed while this call waited for the parent
        return file_out(row)
    current = parent["voucher_file_id"] == row["id"]
    if row["entity_type"] == "payment" and current and parent.get("verified"):
        raise Conflict("evidence_locked", f"this file is the evidence of verified payment {parent['code']}; it stays")
    if row["entity_type"] == "supplier_bill" and current and parent["status"] in ("approved", "paid"):
        raise Conflict("document_locked", f"this file is the document of approved bill {parent['code']}; it stays")
    if row["entity_type"] == "supplier_payout" and current and parent["status"] == "live":
        raise Conflict(
            "evidence_locked", f"this file is the evidence of payout {parent['code']}; upload a replacement first"
        )
    if current and parent["status"] not in FINAL_PARENT.get(row["entity_type"], ()):
        table = CURRENT_FILE.get(row["entity_type"], "supplier_bookings SET voucher_file_id")
        await conn.execute(
            f"UPDATE booking.{table} = NULL, updated_by = $2 WHERE id = $1::uuid",  # noqa: S608 — fixed names
            parent["id"],
            staff.cis_user_id,
        )
    await conn.execute(
        "UPDATE booking.ops_files SET status = 'removed', removed_reason = $2, removed_at = now(), removed_by = $3"
        " WHERE id = $1::uuid",
        row["id"],
        reason[:500],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="ops_file.removed",
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        before={"file_id": row["id"], "status": "active"},
        after={"file_id": row["id"], "status": "removed", "reason": reason[:500]},
        entity_label=f"{parent['case_code']} {parent['code']}",
        summary=f"Removed a {row['kind'].replace('_', ' ')}",
    )
    return file_out(await get_file(conn, row["id"]))
