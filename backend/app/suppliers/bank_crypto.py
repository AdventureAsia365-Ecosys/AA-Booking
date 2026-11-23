"""Envelope encryption of supplier bank accounts (AA-1158 part 2, bk-067; AA-CIS-Infra ``booking_supplier_bank.tf``).

Per bank account row: KMS ``GenerateDataKey`` (AES-256) under the encryption context ``{purpose:
supplier_bank_account, supplier_id: <uuid>}`` returns a plaintext data key and the same key wrapped by the customer
managed key. The details (holder, account number / IBAN, bank codes) are encrypted here with AES-256-GCM: a fresh
12-byte nonce, associated data = ``supplier id + ":" + row id`` (a ciphertext copied onto another row or supplier
fails to open). The database stores the nonce + ciphertext + tag, the wrapped key and the key ARN KMS returned; the
plaintext key is dropped at once. Reading = KMS ``Decrypt`` of the wrapped key under the same context, then AES-GCM.

Only the Booking task role may call KMS with that key and context (the key policy denies every other principal), so a
database dump, a read-only role or another app never sees an account number. Nothing here logs a detail, a key or a
ciphertext; an error names only the step that failed.

The KMS client is boto3, created lazily (thread-safe) and called in a thread like the S3 stores; tests pass a fake
:class:`KeyProvider`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.errors import Unavailable
from app.settings import Settings

log = logging.getLogger(__name__)

PURPOSE = "supplier_bank_account"
ALG = "AES-256-GCM"
NONCE_BYTES = 12  # technical-constant: the AES-GCM standard nonce size
KMS_CONNECT_TIMEOUT = 3  # technical-constant: a staff request waits on KMS, never long
KMS_READ_TIMEOUT = 5  # technical-constant: as above
KMS_ATTEMPTS = 3  # technical-constant: botocore standard retries


class BankCryptoError(Exception):
    """KMS or the cipher refused (never carries a detail, a key or a ciphertext)."""


@dataclass(frozen=True)
class DataKey:
    plaintext: bytes
    wrapped: bytes
    key_id: str


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    wrapped_key: bytes
    key_id: str


class KeyProvider(Protocol):
    async def generate(self, context: dict[str, str]) -> DataKey: ...

    async def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes: ...


def context(supplier_id: str) -> dict[str, str]:
    """The KMS encryption context: exactly these two keys (the key policy requires them)."""
    return {"purpose": PURPOSE, "supplier_id": str(supplier_id)}


def _aad(supplier_id: str, row_id: str) -> bytes:
    return f"{supplier_id}:{row_id}".encode("ascii")


async def seal(provider: KeyProvider, supplier_id: str, row_id: str, details: dict[str, Any]) -> Sealed:
    """Encrypt ``details`` (a JSON-able dict) for the row ``row_id`` of ``supplier_id``."""
    key = await provider.generate(context(supplier_id))
    if len(key.plaintext) != 32:
        raise BankCryptoError("the data key is not AES-256")
    nonce = os.urandom(NONCE_BYTES)
    body = json.dumps(details, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = nonce + AESGCM(key.plaintext).encrypt(nonce, body, _aad(supplier_id, row_id))
    return Sealed(ciphertext=ciphertext, wrapped_key=key.wrapped, key_id=key.key_id)


async def open_sealed(
    provider: KeyProvider, supplier_id: str, row_id: str, ciphertext: bytes, wrapped_key: bytes
) -> dict[str, Any]:
    """The details of one row (raises :class:`BankCryptoError` on any mismatch: wrong row, supplier or key)."""
    plaintext_key = await provider.unwrap(bytes(wrapped_key), context(supplier_id))
    blob = bytes(ciphertext)
    try:
        body = AESGCM(plaintext_key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], _aad(supplier_id, row_id))
    except (InvalidTag, ValueError) as exc:
        raise BankCryptoError("the ciphertext does not open for this row") from exc
    return json.loads(body)


class KmsKeyProvider:
    """The supplier bank account key through boto3 KMS (blocking calls in a thread, the client made once)."""

    def __init__(self, key_id: str, region: str) -> None:
        self.key_id = key_id
        self.region = region
        self._client: Any = None
        self._lock = threading.Lock()

    def _kms(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3  # lazy: only bank account entry and reads need it
                from botocore.config import Config

                self._client = boto3.client(
                    "kms",
                    region_name=self.region,
                    config=Config(
                        connect_timeout=KMS_CONNECT_TIMEOUT,
                        read_timeout=KMS_READ_TIMEOUT,
                        retries={"total_max_attempts": KMS_ATTEMPTS, "mode": "standard"},
                    ),
                )
            return self._client

    def _generate(self, ctx: dict[str, str]) -> DataKey:
        try:
            out = self._kms().generate_data_key(KeyId=self.key_id, KeySpec="AES_256", EncryptionContext=ctx)
        except Exception as exc:  # noqa: BLE001 — any botocore error: say which step, never the request
            log.warning("supplier_bank_kms_failed step=generate error=%s", type(exc).__name__)
            raise BankCryptoError("KMS GenerateDataKey failed") from None
        return DataKey(plaintext=out["Plaintext"], wrapped=out["CiphertextBlob"], key_id=out["KeyId"])

    def _unwrap(self, wrapped: bytes, ctx: dict[str, str]) -> bytes:
        try:
            out = self._kms().decrypt(CiphertextBlob=wrapped, EncryptionContext=ctx, KeyId=self.key_id)
        except Exception as exc:  # noqa: BLE001 — as above
            log.warning("supplier_bank_kms_failed step=decrypt error=%s", type(exc).__name__)
            raise BankCryptoError("KMS Decrypt failed") from None
        return out["Plaintext"]

    async def generate(self, context: dict[str, str]) -> DataKey:
        return await asyncio.to_thread(self._generate, context)

    async def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        return await asyncio.to_thread(self._unwrap, wrapped, context)


def provider_from_settings(settings: Settings) -> KeyProvider | None:
    """The key of ``SUPPLIER_BANK_KMS_KEY_ID``; None when it is not set (entry and reads then answer 503)."""
    if not settings.supplier_bank_kms_key_id:
        return None
    return KmsKeyProvider(settings.supplier_bank_kms_key_id, settings.supplier_bank_kms_region)


def require(provider: KeyProvider | None) -> KeyProvider:
    if provider is None:
        raise Unavailable("bank_crypto_unavailable", "supplier bank accounts cannot be entered or read here (no key)")
    return provider
