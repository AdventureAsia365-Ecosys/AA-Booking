"use client";
// app/(staff)/suppliers/_parts/SupplierBankTab.tsx — AA-1158 part 2 (bk-067): the supplier's bank accounts. Every
// reader sees the masked list (bank, •••• last 4, who entered and approved it). A new account is entered pending
// (supplier_bank.write); a second person with supplier_bank.approve approves it after calling the supplier back on a
// number AA already knew (the call-back note is required), never the person who entered it, except the business
// owner (flagged). Payouts to the supplier wait while a change is pending and for the policy hours after an approval.
// The full details open only for supplier_bank.read, one read at a time, and every read is audited.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Eye, Landmark, Lock, Plus } from "lucide-react";
import { supplierBankApi, type BankDecision, type BankDetails, type NewBankAccount, type PayoutLock, type SupplierBankAccount, type SupplierBankList } from "@/lib/api/supplierBank";
import { ContactChanges } from "./SupplierBankContactChanges";
import { Button, ConfirmDialog, DateText, Field, FormDrawer, Input, Modal, Pill, Section, type BookingTone, useToast } from "@/ui";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { supplierError } from "./shared";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./SupplierBankTab.messages";

const STATUS_TONE: Record<SupplierBankAccount["status"], BookingTone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
  withdrawn: "neutral",
  superseded: "neutral",
};
const STATUS_LABEL = {
  pending: "Waiting for approval",
  approved: "Approved",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
  superseded: "Replaced",
} as const;

type Decision = { account: SupplierBankAccount; action: BankDecision } | null;
const DONE: Partial<Record<SupplierBankAccount["status"], "Bank account approved" | "Bank account rejected" | "Bank account withdrawn">> = {
  approved: "Bank account approved",
  rejected: "Bank account rejected",
  withdrawn: "Bank account withdrawn",
};

export function SupplierBankTab({ supplierRef }: { supplierRef: string }) {
  const t = useT(MSG);
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["supplier", supplierRef, "bank"], queryFn: () => supplierBankApi.list(supplierRef) });
  const [entering, setEntering] = useState(false);
  const [decision, setDecision] = useState<Decision>(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["supplier", supplierRef, "bank"] });
    qc.invalidateQueries({ queryKey: ["supplier", supplierRef] });
  };
  const canEnter = !!q.data?.actions.enter;
  return (
    <Section
      title={t("Bank accounts")}
      description={t("Where AA pays this supplier. A change waits for a second person, who calls the supplier back on a number AA already knew; payouts pause while a change waits and for a while after it is approved.")}
      actions={
        canEnter && (
          <Button variant="primary" onClick={() => setEntering(true)}>
            <Plus size={15} /> {t("Enter bank account")}
          </Button>
        )
      }
    >
      <AccountList q={q} onDecide={setDecision} />
      {entering && <EnterDrawer supplierRef={supplierRef} onClose={() => setEntering(false)} onDone={refresh} />}
      <DecisionDialog decision={decision} onClose={() => setDecision(null)} onDone={refresh} />
    </Section>
  );
}

/** The list (masked), the payout pause and the details view of one account. */
function AccountList({ q, onDecide }: { q: UseQueryResult<SupplierBankList>; onDecide: (d: NonNullable<Decision>) => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const [revealed, setRevealed] = useState<BankDetails | null>(null);
  const reveal = useMutation({ mutationFn: (id: string) => supplierBankApi.reveal(id), onSuccess: setRevealed });
  if (q.isLoading) return <p className="text-[13px] text-muted">{t("Loading bank accounts")}</p>;
  if (q.isError || !q.data) return <p className="text-[13px] text-danger">{tx(supplierError(q.error))}</p>;
  const data = q.data;
  return (
    <>
      <LockNotice lock={data.payout_lock} />
      <ContactChanges changes={data.contact_changes} />
      {data.items.length === 0 && <p className="text-[13px] text-muted">{t("No bank account on file.")}</p>}
      <ul className="flex flex-col gap-3">
        {data.items.map((a) => (
          <AccountCard key={a.id} account={a} onDecide={(action) => onDecide({ account: a, action })} onReveal={() => reveal.mutate(a.id)} revealing={reveal.isPending} />
        ))}
      </ul>
      {reveal.isError && <p role="alert" className="mt-2 text-[13px] text-danger">{tx(supplierError(reveal.error))}</p>}
      <Modal open={revealed !== null} onClose={() => setRevealed(null)} title={t("Bank details")}>
        {revealed && <DetailsList d={revealed} />}
      </Modal>
    </>
  );
}

/** Approve (call-back note), reject or withdraw (reason) one account. */
function DecisionDialog({ decision, onClose, onDone }: { decision: Decision; onClose: () => void; onDone: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const decide = useMutation({
    mutationFn: ({ d, note }: { d: NonNullable<Decision>; note: string }) => supplierBankApi.decide(d.account.id, d.action, note),
    onSuccess: (a) => {
      toast.success(t(DONE[a.status] ?? "Bank account withdrawn"));
      onClose();
      onDone();
    },
  });
  const action = decision?.action ?? "withdraw";
  const ownApproval = action === "approve" && !!decision?.account.actions.approve_self;
  return (
    <ConfirmDialog
      open={decision !== null}
      onClose={() => {
        decide.reset();
        onClose();
      }}
      onConfirm={(note) => decision && decide.mutate({ d: decision, note })}
      busy={decide.isPending}
      error={tx(supplierError(decide.error))}
      danger={action === "reject"}
      title={t(DIALOG[action].title)}
      confirmLabel={t(DIALOG[action].confirm)}
      reason={{ label: t(DIALOG[action].note), required: true, maxLength: DIALOG[action].max }}
      message={ownApproval ? t("You entered this account. Approving it yourself is recorded as a business-owner self-approval.") : t(DIALOG[action].message)}
    />
  );
}

const DIALOG = {
  approve: {
    title: "Approve the bank account",
    confirm: "Approve",
    note: "Call-back note: who confirmed the details, on which number AA already knew",
    message: "Call the supplier on a number from the contract or earlier records, never one sent with this change, and read the details back.",
    max: 500,
  },
  reject: { title: "Reject the bank account", confirm: "Reject", note: "Reason", message: "The current account stays in use.", max: 300 },
  withdraw: { title: "Withdraw the bank account", confirm: "Withdraw", note: "Reason", message: "The current account stays in use.", max: 300 },
} as const;

function LockNotice({ lock }: { lock: PayoutLock }) {
  const t = useT(MSG);
  if (lock.state !== "pending" && lock.state !== "locked") return null;
  return (
    <div role="status" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-warning-border bg-warning-soft px-4 py-3 text-[13px] text-body">
      <Lock size={15} />
      {lock.state === "pending" ? (
        t("Payouts to this supplier are paused until the bank account change is decided.")
      ) : (
        <span>
          {t("Payouts to this supplier open at")} <DateText value={lock.until ?? ""} withTime />
        </span>
      )}
    </div>
  );
}

function AccountCard({
  account: a,
  onDecide,
  onReveal,
  revealing,
}: {
  account: SupplierBankAccount;
  onDecide: (action: BankDecision) => void;
  onReveal: () => void;
  revealing: boolean;
}) {
  const t = useT(MSG);
  return (
    <li className="flex flex-col gap-2 rounded-lg border border-line bg-card px-4 py-3 text-[13px] text-body">
      <AccountHead a={a} />
      <AccountHistory a={a} />
      {a.approve_blocked && <p className="text-warning">{t("Approval closed: the person who entered this account, or you, changed the supplier's contacts recently. Another person checks the contacts against the contract and approves.")}</p>}
      <AccountButtons a={a} onDecide={onDecide} onReveal={onReveal} revealing={revealing} />
    </li>
  );
}

function AccountHead({ a }: { a: SupplierBankAccount }) {
  const t = useT(MSG);
  const place = [a.currency, a.bank_country].filter(Boolean).join(" · ");
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Landmark size={15} className="text-muted" />
      <span className="font-semibold text-ink">{a.bank_name}</span>
      <span className="font-mono">{a.account_masked}</span>
      <span className="text-muted">{place}</span>
      <Pill tone={STATUS_TONE[a.status]}>{t(STATUS_LABEL[a.status])}</Pill>
      {a.owner_self_approved && <Pill tone="info">{t("Business-owner self-approval")}</Pill>}
    </div>
  );
}

function AccountHistory({ a }: { a: SupplierBankAccount }) {
  const t = useT(MSG);
  const name = (p: SupplierBankAccount["entered_by"]) => p?.name ?? "—";
  return (
    <>
      <p className="text-muted">
        {t("Entered by {0}", name(a.entered_by))} · <DateText value={a.entered_at} withTime /> · {a.reason}
      </p>
      {a.approved_by && (
        <p className="text-muted">
          {t("Approved by {0}", name(a.approved_by))} · <DateText value={a.approved_at ?? ""} withTime /> · {a.callback_note}
        </p>
      )}
      {a.closed_by && a.close_reason && (
        <p className="text-muted">
          {t("Closed by {0}", name(a.closed_by))} · {a.close_reason}
        </p>
      )}
    </>
  );
}

function AccountButtons({
  a,
  onDecide,
  onReveal,
  revealing,
}: {
  a: SupplierBankAccount;
  onDecide: (action: BankDecision) => void;
  onReveal: () => void;
  revealing: boolean;
}) {
  const t = useT(MSG);
  const buttons = (["approve", "reject", "withdraw"] as const).filter((k) => a.actions[k]);
  if (buttons.length === 0 && !a.actions.reveal) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {buttons.map((k) => (
        <Button key={k} size="sm" variant={k === "approve" ? "primary" : "ghost"} onClick={() => onDecide(k)}>
          {t(DIALOG[k].confirm)}
        </Button>
      ))}
      {a.actions.reveal && (
        <Button size="sm" variant="ghost" onClick={onReveal} disabled={revealing}>
          <Eye size={14} /> {t("Show details")}
        </Button>
      )}
    </div>
  );
}

function DetailsList({ d }: { d: BankDetails }) {
  const t = useT(MSG);
  const rows = [
    ["Account holder", d.account_holder],
    ["Account number / IBAN", d.account_number],
    ["SWIFT / BIC", d.swift_bic],
    ["Bank code", d.bank_code],
    ["Bank address", d.bank_address],
  ] as const;
  return (
    <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-3 gap-y-2 text-[13px]">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted">{t(label)}</dt>
          <dd className="break-all font-medium text-ink">{value ?? "—"}</dd>
        </div>
      ))}
      <p className="col-span-2 text-xs text-muted">{t("This view was recorded in the audit log.")}</p>
    </dl>
  );
}

const EMPTY: NewBankAccount = { account_holder: "", account_number: "", bank_name: "", currency: "", bank_country: "", swift_bic: "", bank_code: "", bank_address: "", reason: "" };
const FIELDS = [
  { key: "account_holder", label: "Account holder", required: true, max: 140 },
  { key: "account_number", label: "Account number / IBAN", required: true, max: 64 },
  { key: "bank_name", label: "Bank name", required: true, max: 120 },
  { key: "currency", label: "Currency (3 letters)", required: true, max: 3 },
  { key: "bank_country", label: "Bank country (2 letters)", max: 2 },
  { key: "swift_bic", label: "SWIFT / BIC", max: 64 },
  { key: "bank_code", label: "Bank code", max: 34 },
  { key: "bank_address", label: "Bank address", max: 300 },
  { key: "reason", label: "Why the account is entered or changed", required: true, max: 300 },
] as const satisfies readonly { key: keyof NewBankAccount; label: string; required?: boolean; max: number }[];

function EnterDrawer({ supplierRef, onClose, onDone }: { supplierRef: string; onClose: () => void; onDone: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const [form, setForm] = useState<NewBankAccount>(EMPTY);
  const send = useMutation({
    mutationFn: () => {
      const body = Object.fromEntries(Object.entries(form).map(([k, v]) => [k, (v ?? "").trim() || null])) as unknown as NewBankAccount;
      return supplierBankApi.enter(supplierRef, body);
    },
    onSuccess: () => {
      toast.success(t("Bank account sent for approval"));
      onDone();
      onClose();
    },
  });
  return (
    <FormDrawer open onClose={onClose} title={t("Enter bank account")} submitLabel={t("Send for approval")} onSubmit={() => send.mutate()} busy={send.isPending} dirty={form !== EMPTY} error={tx(supplierError(send.error))}>
      <div className="flex flex-col gap-3">
        <p className="text-[13px] text-muted">{t("The account is stored encrypted. It is used for payouts only after a second person approves it after a call-back, and once the payout pause has passed.")}</p>
        {FIELDS.map((f) => (
          <Field key={f.key} label={t(f.label)} required={"required" in f && f.required}>
            <Input className={inputCls} maxLength={f.max} autoComplete="off" value={form[f.key] ?? ""} onChange={(e) => setForm((cur) => ({ ...cur, [f.key]: e.target.value }))} />
          </Field>
        ))}
      </div>
    </FormDrawer>
  );
}
