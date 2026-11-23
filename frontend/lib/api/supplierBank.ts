// lib/api/supplierBank.ts
// AA-1158 part 2 (bk-067) — typed calls for supplier bank accounts (backend routers/supplier_bank.py).
//
//   GET  /booking/suppliers/{ref}/bank-accounts              masked list + payout lock      (supply.read)
//   POST /booking/suppliers/{ref}/bank-accounts              enter one, pending              (supplier_bank.write)
//   POST /booking/supplier-bank-accounts/{id}/decision       {action, note}                  approve: supplier_bank.approve
//                                                            + a call-back note; reject: same; withdraw: the enterer
//   POST /booking/supplier-bank-accounts/{id}/reveal         full details, audited           (supplier_bank.read)
//
// The details are KMS-encrypted on the server; this page only ever holds them while the reveal dialog is open.

import { bkGet, bkSend } from "./client";
import type { IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type BankAccountStatus = "pending" | "approved" | "rejected" | "withdrawn" | "superseded";
export type Person = { id: string | null; name: string | null } | null;

export interface SupplierBankAccount {
  id: string;
  supplier_id: string;
  status: BankAccountStatus;
  bank_name: string;
  bank_country: string | null;
  currency: string;
  account_last4: string;
  account_masked: string;
  reason: string;
  entered_by: Person;
  entered_at: IsoDateTime;
  approved_by: Person;
  approved_at: IsoDateTime | null;
  callback_note: string | null;
  owner_self_approved: boolean;
  closed_by: Person;
  closed_at: IsoDateTime | null;
  close_reason: string | null;
  actions: { reveal: boolean; approve: boolean; approve_self: boolean; reject: boolean; withdraw: boolean };
}

/** `none`: no account on file; `pending`: a change waits; `locked` until `until`; `open`. */
export interface PayoutLock {
  state: "none" | "pending" | "locked" | "open" | "unknown";
  until: IsoDateTime | null;
  hours?: number;
}

export interface SupplierBankList {
  supplier: { id: string; code: string; company_name: string };
  items: SupplierBankAccount[];
  payout_lock: PayoutLock;
  actions: { enter: boolean };
  can_read_details: boolean;
}

export interface NewBankAccount {
  account_holder: string;
  account_number: string;
  bank_name: string;
  currency: string;
  bank_country?: string | null;
  swift_bic?: string | null;
  bank_code?: string | null;
  bank_address?: string | null;
  reason: string;
}

export interface BankDetails {
  id: string;
  account_holder: string;
  account_number: string;
  swift_bic: string | null;
  bank_code: string | null;
  bank_address: string | null;
}

export type BankDecision = "approve" | "reject" | "withdraw";

export const supplierBankApi = {
  list: (ref: string) => bkGet<SupplierBankList>(`suppliers/${enc(ref)}/bank-accounts`),
  enter: (ref: string, body: NewBankAccount) => bkSend<SupplierBankAccount>(`suppliers/${enc(ref)}/bank-accounts`, { body }),
  decide: (id: string, action: BankDecision, note: string) =>
    bkSend<SupplierBankAccount>(`supplier-bank-accounts/${enc(id)}/decision`, { body: { action, note } }),
  reveal: (id: string) => bkSend<BankDetails>(`supplier-bank-accounts/${enc(id)}/reveal`, { body: {} }),
};
