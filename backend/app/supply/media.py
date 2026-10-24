"""Supplier photos of hotels and services (bk-023 ``booking.entity_media``; AA-835 S4, root ADR 0004a decision 2).

Staff upload a photo through the API; the bytes go to the Booking supply-media bucket (``SUPPLY_MEDIA_BUCKET``) under
the key bk-023 derives from the row, ``supply-media/<entity_type>/<entity_id>/<media id>.<ext>``. CIS copies a photo
(``booking.v_supply_media``) and publishes it only while ``usage_rights`` is true: contracting confirms that AA may
use it. A photo is never deleted: ``status`` goes ``active`` → ``removed`` (final), the object stays in the bucket.

- **Upload body is JSON with the bytes in base64**, not multipart: the Booking REST API in API Gateway has no
  ``binaryMediaTypes``, so a multipart body would reach the API re-encoded as text and corrupted (lessons, S229 #660),
  and the staff admin's BFF runs on Vercel, whose functions take at most 4.5 MB of request body. Hence
  :data:`MAX_BYTES` = 3 MiB of image (4 MiB as base64); the staff UI downsizes a larger photo before sending it.
- **The type is read from the bytes** (magic numbers), never trusted from the client: JPEG, PNG or WebP only, and the
  declared ``content_type`` must agree (a renamed file is refused). Width and height come from the image header
  (stdlib parsing, no image library: nothing is decoded). Animated WebP is refused.
- **sha256 is computed here** over the stored bytes (CIS re-checks it after its copy). The same bytes twice on one
  entity is 409 ``media_exists`` (bk-023 keeps one active copy per entity).
- **Order of a write**: the row (and the bk-023 triggers: supply_version bump, outbox row) and the audit row first,
  then the S3 put, all inside the caller's transaction: a failed put rolls the row back, so no row ever names a
  missing object. A put that succeeded before a failed commit leaves an object no row names (harmless, never served).
- **Reads**: the list carries a presigned GET URL per photo (15 minutes, signed with the task role), so the staff UI
  can show a private object without a public bucket or a CDN. Bucket and key never leave the API.

Permissions: ``supply.read`` lists, ``supply.write`` uploads and changes (admin, contracting). Every write adds one
``booking.audit_logs`` row (``entity_type`` ``entity_media``), shown in the hotel's / service's recent activity.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.settings import Settings
from app.supply import records
from app.supply.records import Kind

log = logging.getLogger(__name__)

MAX_BYTES = 3 * 1024 * 1024
# Base64 of MAX_BYTES (4 output characters per 3 input bytes, padded): the request model refuses a longer string
# before anything is decoded.
MAX_BASE64_CHARS = 4 * ((MAX_BYTES + 2) // 3)
MAX_DIMENSION = 20_000  # bk-023 entity_media_size_chk
MAX_ACTIVE_PER_ENTITY = 40
PRESIGN_SECONDS = 900
OBJECT_PREFIX = "supply-media"
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
CONTENT_TYPES = tuple(EXTENSIONS)
STATUSES = ("active", "removed")


# ── Image sniffing ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Image:
    content_type: str
    width: int
    height: int


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# JPEG start-of-frame markers (C0–CF without DHT C4, JPG C8, DAC CC) carry the frame size.
JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
WEBP_ANIMATION_FLAG = 0x02


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image / start of scan before any frame header
            return None
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2:
            return None
        if marker in JPEG_SOF:
            if i + 9 > n:
                return None
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + length
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        if data[20] & WEBP_ANIMATION_FLAG:
            raise Invalid("animated_image", "animated WebP is not a photo; upload a still image")
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


def sniff(data: bytes) -> Image:
    """The image type and size read from the bytes; 422 ``unsupported_image`` for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        content_type, size = "image/jpeg", _jpeg_size(data)
    elif data[:8] == PNG_SIGNATURE:
        content_type, size = "image/png", _png_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        content_type, size = "image/webp", _webp_size(data)
    else:
        raise Invalid("unsupported_image", "upload a JPEG, PNG or WebP photo")
    if size is None:
        raise Invalid("unsupported_image", f"the {content_type} file is damaged: its size cannot be read")
    width, height = size
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise Invalid("image_dimensions", f"a photo is 1 to {MAX_DIMENSION} pixels wide and high")
    return Image(content_type, width, height)


def decode(data_base64: str) -> bytes:
    """The uploaded bytes: strict base64, 1 byte to :data:`MAX_BYTES`."""
    if len(data_base64) > MAX_BASE64_CHARS:
        raise Invalid("image_too_large", f"a photo is at most {MAX_BYTES // (1024 * 1024)} MB")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Invalid("invalid_base64", "data_base64 is not valid base64") from exc
    if not data:
        raise Invalid("empty_image", "the photo is empty")
    if len(data) > MAX_BYTES:
        raise Invalid("image_too_large", f"a photo is at most {MAX_BYTES // (1024 * 1024)} MB")
    return data


def object_key(entity_type: str, entity_id: str, media_id: str, content_type: str) -> str:
    """The key bk-023's ``entity_media_key_chk`` derives from the row."""
    return f"{OBJECT_PREFIX}/{entity_type}/{entity_id}/{media_id}.{EXTENSIONS[content_type]}"


# ── Storage ──────────────────────────────────────────────────────────────────


class MediaStore(Protocol):
    bucket: str

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None: ...

    async def urls(self, keys: list[str]) -> dict[str, str]: ...


class S3MediaStore:
    """The supply-media bucket through boto3 (blocking, so every call runs in a thread). boto3 is imported lazily:
    the API imports this module at start-up and only an upload or a photo list needs S3."""

    def __init__(self, bucket: str, region: str) -> None:
        self.bucket = bucket
        self.region = region
        self._client: Any = None

    def _s3(self) -> Any:
        if self._client is None:
            import boto3  # lazy, see the class docstring
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                region_name=self.region,
                config=Config(
                    signature_version="s3v4", s3={"addressing_style": "virtual"}, retries={"max_attempts": 3}
                ),
            )
        return self._client

    def _put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self._s3().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",  # the bucket policy refuses any other algorithm
            ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode("ascii"),  # S3 verifies the bytes
        )

    def _urls(self, keys: list[str]) -> dict[str, str]:
        s3 = self._s3()
        return {
            key: s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=PRESIGN_SECONDS
            )
            for key in keys
        }

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        await asyncio.to_thread(self._put, key, data, content_type, sha256)

    async def urls(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        return await asyncio.to_thread(self._urls, keys)


def store_from_settings(settings: Settings) -> MediaStore | None:
    """The bucket of ``SUPPLY_MEDIA_BUCKET``; None when it is not set (uploads then answer 503)."""
    if not settings.supply_media_bucket:
        return None
    return S3MediaStore(settings.supply_media_bucket, settings.supply_media_region)


# ── Rows ─────────────────────────────────────────────────────────────────────

MEDIA_COLUMNS = """
    m.id::text AS id, m.entity_type, m.entity_id::text AS entity_id, m.s3_key, m.content_type, m.bytes, m.width,
    m.height, m.sha256, m.usage_rights, m.status, m.uploaded_by, up.name AS uploaded_by_name, m.updated_by,
    m.created_at, m.updated_at
"""
MEDIA_FROM = "booking.entity_media m LEFT JOIN booking.staff_users up ON up.cis_user_id = m.uploaded_by"


def _out(row: Any, urls: dict[str, str]) -> dict[str, Any]:
    out = dict(row)
    key = out.pop("s3_key")
    out["uploaded_by"] = {"id": out["uploaded_by"], "name": out.pop("uploaded_by_name")}
    out["url"] = urls.get(key) if out["status"] == "active" else None
    return out


async def _urls(store: MediaStore | None, rows: list[Any]) -> dict[str, str]:
    """Presigned URLs of the active photos; none (logged) when the bucket is not configured or cannot sign."""
    keys = [r["s3_key"] for r in rows if r["status"] == "active"]
    if store is None or not keys:
        return {}
    try:
        return await store.urls(keys)
    except Exception as exc:  # noqa: BLE001 — a missing preview must never fail the read
        log.warning("supply_media_presign_failed error=%s", type(exc).__name__)
        return {}


def _label(kind: Kind, record: dict[str, Any]) -> str:
    return f"{records.label(kind, record)} · photo"


async def list_media(
    conn: asyncpg.Connection, store: MediaStore | None, kind: Kind, ref: str, *, include_removed: bool = False
) -> dict[str, Any]:
    record = await records.resolve(conn, kind, ref)
    rows = await conn.fetch(
        f"SELECT {MEDIA_COLUMNS} FROM {MEDIA_FROM}"  # noqa: S608 — constants only
        " WHERE m.entity_type = $1 AND m.entity_id = $2::uuid AND ($3 OR m.status = 'active')"
        " ORDER BY m.status, m.created_at, m.id",
        kind.entity,
        record["id"],
        include_removed,
    )
    urls = await _urls(store, rows)
    return {"items": [_out(r, urls) for r in rows], "max_bytes": MAX_BYTES, "max_active": MAX_ACTIVE_PER_ENTITY}


async def get_media(conn: asyncpg.Connection, media_id: str, *, lock: bool = False) -> Any:
    mid = records.as_uuid(media_id, "media_not_found", "photo")
    row = await conn.fetchrow(
        f"SELECT {MEDIA_COLUMNS} FROM {MEDIA_FROM} WHERE m.id = $1::uuid"  # noqa: S608 — constants only
        + (" FOR UPDATE OF m" if lock else ""),
        mid,
    )
    if row is None:
        raise NotFound("media_not_found", f"photo {media_id} not found")
    return row


async def upload_media(
    conn: asyncpg.Connection,
    store: MediaStore | None,
    kind: Kind,
    staff: Staff,
    ref: str,
    *,
    data_base64: str,
    content_type: str,
    usage_rights: bool = False,
    filename: str | None = None,
) -> dict[str, Any]:
    """Check the bytes, write the row (+ audit), then put the object; run inside the caller's transaction."""
    if store is None:
        raise Unavailable("media_storage_unavailable", "photo storage is not configured for this environment")
    data = decode(data_base64)
    image = sniff(data)
    if content_type != image.content_type:
        raise Invalid(
            "content_type_mismatch",
            f"the file is {image.content_type}, not {content_type}",
            detected=image.content_type,
        )
    sha256 = hashlib.sha256(data).hexdigest()
    record = await records.resolve(conn, kind, ref, lock=True)
    if record["status"] != "active":
        raise Conflict(f"{kind.entity}_inactive", f"activate {kind.entity} {record['code']} before adding photos")
    counts = await conn.fetchrow(
        "SELECT count(*) AS active, count(*) FILTER (WHERE sha256 = $3) AS same FROM booking.entity_media"
        " WHERE entity_type = $1 AND entity_id = $2::uuid AND status = 'active'",
        kind.entity,
        record["id"],
        sha256,
    )
    if counts["same"]:
        raise Conflict("media_exists", f"this photo is already on {kind.entity} {record['code']}")
    if counts["active"] >= MAX_ACTIVE_PER_ENTITY:
        raise Conflict(
            "too_many_photos", f"a {kind.entity} has at most {MAX_ACTIVE_PER_ENTITY} photos; remove one first"
        )
    media_id = str(uuid.uuid4())
    key = object_key(kind.entity, record["id"], media_id, image.content_type)
    try:
        await conn.execute(
            "INSERT INTO booking.entity_media (id, entity_type, entity_id, s3_bucket, s3_key, content_type, bytes,"
            " width, height, sha256, usage_rights, uploaded_by, updated_by)"
            " VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12)",
            media_id,
            kind.entity,
            record["id"],
            store.bucket,
            key,
            image.content_type,
            len(data),
            image.width,
            image.height,
            sha256,
            usage_rights,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:  # a concurrent upload of the same bytes
        raise Conflict("media_exists", f"this photo is already on {kind.entity} {record['code']}") from exc
    name = (filename or "").strip()[:200] or None
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="entity_media.uploaded",
        entity_type="entity_media",
        entity_id=media_id,
        after={
            "entity_type": kind.entity,
            "entity_id": record["id"],
            "content_type": image.content_type,
            "bytes": len(data),
            "width": image.width,
            "height": image.height,
            "sha256": sha256,
            "usage_rights": usage_rights,
            **({"filename": name} if name else {}),
        },
        entity_label=_label(kind, record),
        summary="Added a photo" + (" (usage rights confirmed)" if usage_rights else " (usage rights not confirmed)"),
    )
    try:
        await store.put(key, data, image.content_type, sha256)
    except Exception as exc:  # noqa: BLE001 — any storage failure rolls the row back (the caller's transaction)
        log.warning("supply_media_put_failed entity=%s error=%s", kind.entity, type(exc).__name__)
        raise Unavailable("media_storage_unavailable", "the photo could not be stored; try again") from exc
    row = await get_media(conn, media_id)
    return _out(row, await _urls(store, [row]))


async def update_media(
    conn: asyncpg.Connection, store: MediaStore | None, staff: Staff, media_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """``usage_rights`` (true / false) and ``status`` ``removed`` (final: bk-023 refuses any change after it)."""
    current = await get_media(conn, media_id, lock=True)
    if current["status"] == "removed":
        raise Conflict("media_removed", "this photo was removed; upload it again instead")
    if changes.get("status") not in (None, "active", "removed"):
        raise Invalid("unknown_status", f"status must be one of {', '.join(STATUSES)}")
    changed = {
        k: v for k, v in changes.items() if k in ("usage_rights", "status") and v is not None and current[k] != v
    }
    if not changed:
        return {**_out(current, await _urls(store, [current])), "changed": []}
    kind = records.KINDS[current["entity_type"]]
    record = await records.resolve(conn, kind, current["entity_id"])
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    await conn.execute(
        f"UPDATE booking.entity_media SET {sets}, updated_by = ${len(changed) + 2} WHERE id = $1::uuid",  # noqa: S608
        current["id"],
        *changed.values(),
        staff.cis_user_id,
    )
    removed = changed.get("status") == "removed"
    if removed:
        summary = "Removed a photo"
    else:
        summary = "Confirmed usage rights of a photo" if changed["usage_rights"] else "Withdrew usage rights of a photo"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="entity_media.removed" if removed else "entity_media.updated",
        entity_type="entity_media",
        entity_id=current["id"],
        before={k: current[k] for k in changed},
        after=changed,
        entity_label=_label(kind, record),
        summary=summary,
    )
    row = await get_media(conn, current["id"])
    return {**_out(row, await _urls(store, [row])), "changed": sorted(changed)}
