"""AA-835 S4 supplier photos (app.supply.media): image sniffing from the bytes, base64 limits, upload and change
branches on a scripted connection with a fake bucket, and route permissions. The bk-023 CHECKs, grants and triggers
on a real PostgreSQL are covered by tests/integration/test_bk023_media_api.py."""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.main import create_app
from app.settings import Settings, SettingsError, load_settings
from app.supply import media
from app.supply.records import HOTEL, SERVICE
from tests.unit.fakes import ScriptedConn
from tests.unit.test_supply import HID, SID, hotel_row, service_row

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
PREVIEW_ADMIN = Staff("a-2", None, "Admin (preview)", "admin", True, "admin", preview=True)
MID = "77777777-7777-4777-8777-777777777777"
BUCKET = "aa-booking-dev-supply-media-000000000000"


# ── Image fixtures (headers only: nothing decodes the pixels) ─────────────────


def png(width: int = 640, height: int = 480) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return media.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00" + b"\x00" * 16


def jpeg(width: int = 800, height: int = 600) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xda" + b"\x00" * 32 + b"\xff\xd9"


def riff(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_lossy(width: int = 1024, height: int = 768) -> bytes:
    payload = b"\x30\x01\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 16
    return riff(b"VP8 ", payload)


def webp_lossless(width: int = 300, height: int = 200) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return riff(b"VP8L", b"\x2f" + struct.pack("<I", bits) + b"\x00" * 16)


def webp_extended(width: int = 2000, height: int = 1000, flags: int = 0) -> bytes:
    payload = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return riff(b"VP8X", payload + b"\x00" * 16)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ── Sniffing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png(), media.Image("image/png", 640, 480)),
        (jpeg(), media.Image("image/jpeg", 800, 600)),
        (webp_lossy(), media.Image("image/webp", 1024, 768)),
        (webp_lossless(), media.Image("image/webp", 300, 200)),
        (webp_extended(), media.Image("image/webp", 2000, 1000)),
    ],
)
def test_sniff_reads_type_and_size_from_the_bytes(data: bytes, expected: media.Image) -> None:
    assert media.sniff(data) == expected


def test_sniff_skips_jpeg_fill_bytes_and_tables_before_the_frame() -> None:
    dht = b"\xff\xc4" + struct.pack(">H", 5) + b"\x00\x00\x00"
    data = jpeg(1200, 900)
    assert media.sniff(data[:2] + b"\xff" + dht + data[2:]) == media.Image("image/jpeg", 1200, 900)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"GIF89a" + b"\x00" * 40, "unsupported_image"),
        (b"%PDF-1.7 " + b"\x00" * 40, "unsupported_image"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "unsupported_image"),
        (media.PNG_SIGNATURE + b"\x00" * 4, "unsupported_image"),  # truncated: no IHDR
        (b"\xff\xd8\xff\xda" + b"\x00" * 20, "unsupported_image"),  # scan before any frame header
        (riff(b"VP8 ", b"\x00" * 26), "unsupported_image"),  # no VP8 start code
        (png(0, 10), "image_dimensions"),
        (jpeg(20_001, 10), "image_dimensions"),
        (webp_extended(flags=media.WEBP_ANIMATION_FLAG), "animated_image"),
    ],
)
def test_sniff_refuses_other_or_damaged_files(data: bytes, code: str) -> None:
    with pytest.raises(Invalid) as err:
        media.sniff(data)
    assert err.value.code == code


def test_decode_limits() -> None:
    assert media.decode(b64(png())) == png()
    for raw, code in (
        ("not base64!", "invalid_base64"),
        ("", "empty_image"),
        ("A" * (media.MAX_BASE64_CHARS + 4), "image_too_large"),
        (b64(b"\x00" * (media.MAX_BYTES + 1)), "image_too_large"),
    ):
        with pytest.raises(Invalid) as err:
            media.decode(raw)
        assert err.value.code == code
    assert len(media.decode(b64(b"\x01" * media.MAX_BYTES))) == media.MAX_BYTES


def test_object_key_matches_the_bk023_check() -> None:
    assert media.object_key("hotel", HID, MID, "image/webp") == f"supply-media/hotel/{HID}/{MID}.webp"
    assert media.object_key("service", SID, MID, "image/jpeg").endswith(".jpg")


def test_settings_bucket_name_is_checked() -> None:
    assert load_settings({"SUPPLY_MEDIA_BUCKET": BUCKET}).supply_media_bucket == BUCKET
    assert load_settings({}).supply_media_bucket == ""
    for bad in ("aa-cis-bronze-000000000000", "aa-booking-dev-supply-media-123", f"{BUCKET}/x"):
        with pytest.raises(SettingsError):
            load_settings({"SUPPLY_MEDIA_BUCKET": bad})
    with pytest.raises(SettingsError):
        load_settings({"SUPPLY_MEDIA_REGION": "west"})
    assert media.store_from_settings(Settings(database_url=None)) is None
    store = media.store_from_settings(Settings(database_url=None, supply_media_bucket=BUCKET))
    assert isinstance(store, media.S3MediaStore) and store.bucket == BUCKET and store.region == "us-west-1"


# ── Upload / change on a scripted connection ─────────────────────────────────


class FakeStore:
    bucket = BUCKET

    def __init__(self, fail: bool = False, conn: ScriptedConn | None = None) -> None:
        self.fail = fail
        self.conn = conn
        self.puts: list[tuple[str, bytes, str, str]] = []
        self.sql_before_put: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        if self.conn is not None:
            self.sql_before_put = [flat for flat, _ in self.conn.calls]
        if self.fail:
            raise OSError("s3 down")
        self.puts.append((key, data, content_type, sha256))

    async def urls(self, keys: list[str]) -> dict[str, str]:
        return {k: f"https://signed.example/{k}" for k in keys}


def media_row(**kw: Any) -> dict[str, Any]:
    return {
        "id": MID,
        "entity_type": "hotel",
        "entity_id": HID,
        "s3_key": f"supply-media/hotel/{HID}/{MID}.png",
        "content_type": "image/png",
        "bytes": 100,
        "width": 640,
        "height": 480,
        "sha256": "0" * 64,
        "usage_rights": False,
        "status": "active",
        "uploaded_by": "u-5",
        "uploaded_by_name": "Contracts",
        "updated_by": "u-5",
        "created_at": None,
        "updated_at": None,
    } | kw


def media_conn(*, active: int = 0, same: int = 0, row: dict[str, Any] | None = None, **hotel: Any) -> ScriptedConn:
    return ScriptedConn(
        [
            ("FROM booking.hotels r JOIN", hotel_row(**hotel)),
            ("FROM booking.services r JOIN", service_row()),
            ("count(*) FILTER (WHERE sha256 = $3)", {"active": active, "same": same}),
            ("WHERE m.id = $1::uuid", media_row(**(row or {}))),
            ("WHERE m.entity_type = $1", [media_row(**(row or {}))]),
        ]
    )


@pytest.mark.asyncio
async def test_upload_writes_row_audit_then_object() -> None:
    conn, data = media_conn(), png()
    store = FakeStore(conn=conn)
    out = await media.upload_media(
        conn, store, HOTEL, CONTRACTING, "HTL-000001", data_base64=b64(data), content_type="image/png", filename="a.png"
    )
    (insert,) = conn.sql("INSERT INTO booking.entity_media")
    media_id = insert[0]
    sha = hashlib.sha256(data).hexdigest()
    key = f"supply-media/hotel/{HID}/{media_id}.png"
    assert insert[1:] == ("hotel", HID, BUCKET, key, "image/png", len(data), 640, 480, sha, False, "u-5")
    assert store.puts == [(key, data, "image/png", sha)]
    (audit_args,) = conn.sql("INSERT INTO booking.audit_logs")
    assert audit_args[3] == "entity_media.uploaded" and audit_args[4] == "entity_media"
    assert '"filename": "a.png"' in audit_args[7] and "usage rights not confirmed" in audit_args[9]
    # Row and audit row are written before the put: a failed put rolls both back with the caller's transaction.
    written = [flat.split(" (")[0] for flat in store.sql_before_put if flat.startswith("INSERT")]
    assert written == ["INSERT INTO booking.entity_media", "INSERT INTO booking.audit_logs"]
    assert out["url"].startswith("https://signed.example/") and "s3_key" not in out and "s3_bucket" not in out
    assert out["uploaded_by"] == {"id": "u-5", "name": "Contracts"}


@pytest.mark.asyncio
async def test_upload_refusals() -> None:
    good = {"data_base64": b64(png()), "content_type": "image/png"}
    cases: list[tuple[ScriptedConn, Any, dict[str, Any], type[Exception], str]] = [
        (media_conn(), None, good, Unavailable, "media_storage_unavailable"),
        (media_conn(), FakeStore(), {**good, "content_type": "image/jpeg"}, Invalid, "content_type_mismatch"),
        (
            media_conn(),
            FakeStore(),
            {"data_base64": b64(b"<html>" + b"\x00" * 30), "content_type": "image/png"},
            Invalid,
            "unsupported_image",
        ),
        (media_conn(status="inactive"), FakeStore(), good, Conflict, "hotel_inactive"),
        (media_conn(same=1, active=1), FakeStore(), good, Conflict, "media_exists"),
        (media_conn(active=media.MAX_ACTIVE_PER_ENTITY), FakeStore(), good, Conflict, "too_many_photos"),
        (media_conn(), FakeStore(fail=True), good, Unavailable, "media_storage_unavailable"),
    ]
    for conn, store, body, exc, code in cases:
        with pytest.raises(exc) as err:
            await media.upload_media(conn, store, HOTEL, CONTRACTING, HID, **body)
        assert err.value.code == code


@pytest.mark.asyncio
async def test_upload_spoofed_type_writes_nothing() -> None:
    conn, store = media_conn(), FakeStore()
    with pytest.raises(Invalid):
        await media.upload_media(conn, store, SERVICE, ADMIN, SID, data_base64=b64(jpeg()), content_type="image/webp")
    assert conn.calls == [] and store.puts == []


@pytest.mark.asyncio
async def test_update_rights_and_remove() -> None:
    conn = media_conn()
    out = await media.update_media(conn, FakeStore(), CONTRACTING, MID, {"usage_rights": True})
    assert out["changed"] == ["usage_rights"]
    assert conn.sql("UPDATE booking.entity_media SET usage_rights = $2, updated_by = $3") == [(MID, True, "u-5")]
    assert "Confirmed usage rights" in conn.sql("INSERT INTO booking.audit_logs")[0][9]

    conn = media_conn()
    out = await media.update_media(conn, FakeStore(), ADMIN, MID, {"status": "removed"})
    assert out["changed"] == ["status"]
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "entity_media.removed"

    conn = media_conn()
    assert (await media.update_media(conn, FakeStore(), ADMIN, MID, {"usage_rights": False}))["changed"] == []
    assert not conn.sql("UPDATE booking.entity_media")

    with pytest.raises(Conflict) as err:
        await media.update_media(media_conn(row={"status": "removed"}), FakeStore(), ADMIN, MID, {"usage_rights": True})
    assert err.value.code == "media_removed"
    with pytest.raises(NotFound):
        await media.update_media(ScriptedConn(), FakeStore(), ADMIN, "not-a-uuid", {"usage_rights": True})


@pytest.mark.asyncio
async def test_list_signs_active_photos_only_and_survives_a_signing_failure() -> None:
    conn = media_conn()
    listed = await media.list_media(conn, FakeStore(), HOTEL, "HTL-000001")
    assert listed["items"][0]["url"].endswith(f"{MID}.png") and listed["max_bytes"] == media.MAX_BYTES
    assert conn.sql("WHERE m.entity_type = $1")[0] == ("hotel", HID, False)

    removed = await media.list_media(
        media_conn(row={"status": "removed"}), FakeStore(), HOTEL, HID, include_removed=True
    )
    assert removed["items"][0]["url"] is None

    class Broken(FakeStore):
        async def urls(self, keys: list[str]) -> dict[str, str]:
            raise RuntimeError("no credentials")

    assert (await media.list_media(media_conn(), Broken(), HOTEL, HID))["items"][0]["url"] is None
    assert (await media.list_media(media_conn(), None, HOTEL, HID))["items"][0]["url"] is None


# ── Routes ───────────────────────────────────────────────────────────────────


def client(staff: Staff, conn: ScriptedConn | None = None, store: Any = None) -> TestClient:
    app = create_app(
        Settings(database_url=None, environment="test"), media_store=store if store is not None else FakeStore()
    )
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn or media_conn()
    return TestClient(app)


UPLOAD = {"content_type": "image/png", "data_base64": b64(png()), "usage_rights": True}
WRITES = [
    ("post", f"/booking/hotels/{HID}/media", UPLOAD),
    ("post", f"/booking/services/{SID}/media", UPLOAD),
    ("patch", f"/booking/media/{MID}", {"usage_rights": True}),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_photo_writes_need_supply_write(method: str, path: str, body: Any) -> None:
    for staff in (SALES, CONTENT, PREVIEW_ADMIN):
        assert client(staff).request(method, path, json=body).status_code == 403
    for staff in (CONTRACTING, ADMIN):
        assert client(staff).request(method, path, json=body).status_code in (200, 201)


@pytest.mark.parametrize(
    "path", [f"/booking/hotels/{HID}/media", f"/booking/services/{SID}/media?include_removed=true"]
)
def test_photo_lists_are_supply_read(path: str) -> None:
    for staff in (SALES, PREVIEW_ADMIN):
        assert client(staff).get(path).status_code == 200
    assert client(CONTENT).get(path).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {**UPLOAD, "content_type": "image/gif"},
        {**UPLOAD, "url": "https://example.com/x.png"},  # never a free URL
        {"content_type": "image/png"},
    ],
)
def test_upload_body_is_checked(body: dict[str, Any]) -> None:
    assert client(ADMIN).post(f"/booking/hotels/{HID}/media", json=body).status_code == 422


def test_oversized_upload_answers_image_too_large() -> None:
    res = client(ADMIN).post(
        f"/booking/hotels/{HID}/media", json={**UPLOAD, "data_base64": "A" * (media.MAX_BASE64_CHARS + 1)}
    )
    assert res.status_code == 422 and res.json()["detail"] == "image_too_large"


def test_s3_client_is_bounded_under_the_api_gateway_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    made: list[Any] = []
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: made.append(kw["config"]) or object())
    store = media.S3MediaStore(BUCKET, "us-west-1")
    assert store._s3() is store._s3() and len(made) == 1  # built once
    config = made[0]
    assert config.connect_timeout == media.S3_CONNECT_TIMEOUT and config.read_timeout == media.S3_READ_TIMEOUT
    assert config.retries == {"total_max_attempts": media.S3_TOTAL_ATTEMPTS}
    assert media.S3_TOTAL_ATTEMPTS * (media.S3_CONNECT_TIMEOUT + media.S3_READ_TIMEOUT) < 29


def test_upload_without_bucket_is_503_and_patch_body_is_checked() -> None:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: ADMIN
    app.dependency_overrides[get_pool] = lambda: media_conn()
    res = TestClient(app).post(f"/booking/hotels/{HID}/media", json=UPLOAD)
    assert res.status_code == 503 and res.json()["detail"] == "media_storage_unavailable"
    assert client(ADMIN).patch(f"/booking/media/{MID}", json={"status": "active"}).status_code == 422
    assert client(ADMIN).patch(f"/booking/media/{MID}", json={"s3_key": "x"}).status_code == 422
