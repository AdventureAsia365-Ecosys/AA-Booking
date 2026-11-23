"""Supplier bank account endpoints (AA-1158 part 2, bk-067; :mod:`app.suppliers.bank_accounts`).

- ``GET /booking/suppliers/{ref}/bank-accounts`` (``supply.read``): the masked list, the payout lock, what the caller
  may do. Never a full account number.
- ``POST /booking/suppliers/{ref}/bank-accounts`` (``supplier_bank.write``): enter one, pending.
- ``POST /booking/supplier-bank-accounts/{id}/decision`` (``approve`` with a call-back note / ``reject`` with a reason:
  ``supplier_bank.approve``, never the person who entered it unless the business owner, flagged; ``withdraw``: the
  person who entered it).
- ``POST /booking/supplier-bank-accounts/{id}/reveal`` (``supplier_bank.read``): the full details, audited. A POST: a
  read that writes an audit row, refused to read-only sessions.

A 422 on the entry or the decision body names the fields and error types only, never a submitted value
(:func:`app.errors.redact_validation`); no route logs a body.

Entry and reveal need the KMS key (``SUPPLIER_BANK_KMS_KEY_ID``): 503 ``bank_crypto_unavailable`` without it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.errors import redact_validation
from app.suppliers import bank_accounts, bank_crypto

router = APIRouter(prefix="/booking", tags=["supplier bank accounts"])

SupplierRef = Annotated[str, Path(min_length=1, max_length=64, description="Supplier UUID or code (SUP-000123)")]
AccountId = Annotated[UUID, Path(description="supplier bank account UUID")]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("supply.read"))]
CanEnter = Annotated[Staff, Depends(require("supplier_bank.write"))]
CanReveal = Annotated[Staff, Depends(require("supplier_bank.read"))]
# The decision route checks supplier_bank.approve (approve / reject) or supplier_bank.write (withdraw) in the service.
CanDecide = Annotated[Staff, Depends(require("supply.read"))]


def _provider(request: Request) -> bank_crypto.KeyProvider | None:
    return request.app.state.bank_key


Provider = Annotated[bank_crypto.KeyProvider | None, Depends(_provider)]


class NewBankAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_holder: str = Field(min_length=1, max_length=140)
    account_number: str = Field(min_length=4, max_length=64, description="account number or IBAN")
    bank_name: str = Field(min_length=1, max_length=120)
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    bank_country: str | None = Field(default=None, pattern=r"^\s*([A-Za-z]{2})?\s*$")
    swift_bic: str | None = Field(default=None, max_length=64)
    bank_code: str | None = Field(default=None, max_length=34)
    bank_address: str | None = Field(default=None, max_length=300)
    reason: str = Field(min_length=1, max_length=300, description="why the account is entered or changed")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "withdraw"]
    note: str | None = Field(
        default=None,
        max_length=500,
        description="approve: the call-back (who confirmed, on which known number); reject / withdraw: the reason",
    )


@router.get("/suppliers/{supplier_ref}/bank-accounts")
async def list_bank_accounts(supplier_ref: SupplierRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bank_accounts.list_for(conn, staff, supplier_ref)


@router.post("/suppliers/{supplier_ref}/bank-accounts", status_code=201)
@redact_validation  # a 422 never echoes a submitted account number
async def enter_bank_account(
    body: NewBankAccount, supplier_ref: SupplierRef, staff: CanEnter, pool: Pool, provider: Provider
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await bank_accounts.enter(conn, staff, provider, supplier_ref, body.model_dump())


@router.post("/supplier-bank-accounts/{account_id}/decision")
@redact_validation  # the call-back note names a person and a number
async def decide_bank_account(body: Decision, account_id: AccountId, staff: CanDecide, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bank_accounts.decide(conn, staff, str(account_id), body.action, body.note)


@router.post("/supplier-bank-accounts/{account_id}/reveal")
async def reveal_bank_account(
    account_id: AccountId, staff: CanReveal, pool: Pool, provider: Provider
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await bank_accounts.reveal(conn, staff, provider, str(account_id))
