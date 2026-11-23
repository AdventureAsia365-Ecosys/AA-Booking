"use client";
// app/(staff)/suppliers/_parts/SupplierLegalTerms.tsx — AA-1158 part 2 (bk-067): the supplier's legal entity, tax code,
// standard payment terms and contacts by role. `LegalTermsFacts` shows them on the Profile tab; `useLegalTermsFields`
// is the matching part of the add / edit form (the API checks every field again: contacts need a role and a name,
// e-mail or phone; a deposit percentage only with the deposit + balance basis).

import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import {
  CONTACT_ROLES,
  TERMS_BASES,
  TERMS_METHODS,
  type ContactRole,
  type PaymentTerms,
  type RoleContact,
  type SupplierBody,
  type SupplierDetail,
} from "@/lib/api/suppliers";
import { Button, Field, Input, Section, Select } from "@/ui";
import { Fact, FactGrid } from "@/app/(staff)/_parts/DetailHeader";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./SupplierLegalTerms.messages";

const ROLE_LABEL = {
  reservations: "Reservations",
  accounts: "Accounts",
  sales: "Sales",
  operations: "Operations",
  emergency: "Emergency",
  management: "Management",
} as const;
const BASIS_LABEL = {
  prepayment: "Prepayment before the service",
  deposit_balance: "Deposit, then the balance",
  on_arrival: "On arrival",
  after_service: "After the service",
  monthly_statement: "Monthly statement",
} as const;
const METHOD_LABEL = { bank_transfer: "Bank transfer", card: "Card", cash: "Cash", other: "Other" } as const;

/** One line of the standard terms ("Deposit, then the balance · 30% · 14 days · Bank transfer"). */
function useTermsText(): (p: PaymentTerms | null | undefined) => string {
  const t = useT(MSG);
  return (p) => {
    if (!p) return "—";
    const parts: string[] = [t(BASIS_LABEL[p.basis])];
    if (p.deposit_percent != null) parts.push(`${p.deposit_percent}%`);
    if (p.due_days != null) parts.push(t("{0} days", p.due_days));
    if (p.method) parts.push(t(METHOD_LABEL[p.method]));
    if (p.note) parts.push(p.note);
    return parts.join(" · ");
  };
}

export function LegalTermsFacts({ s }: { s: SupplierDetail }) {
  const t = useT(MSG);
  const termsText = useTermsText();
  const contacts = s.role_contacts ?? [];
  return (
    <Section title={t("Legal and payment")}>
      <FactGrid>
        <Fact label={t("Legal entity")}>{s.legal_entity ?? "—"}</Fact>
        <Fact label={t("Tax code")}>{s.tax_code ?? "—"}</Fact>
        <Fact label={t("Standard payment terms")} wide>
          {termsText(s.payment_terms)}
        </Fact>
        <Fact label={t("Contacts by role")} wide>
          {contacts.length === 0 ? (
            "—"
          ) : (
            <ul className="flex flex-col gap-1">
              {contacts.map((c, i) => (
                <li key={i}>
                  <span className="font-medium text-ink">{t(ROLE_LABEL[c.role])}</span>: {[c.name, c.email, c.phone].filter(Boolean).join(" · ")}
                </li>
              ))}
            </ul>
          )}
        </Fact>
      </FactGrid>
    </Section>
  );
}

type TermsDraft = { basis: string; deposit_percent: string; due_days: string; method: string; note: string };
type ContactDraft = { role: ContactRole; name: string; email: string; phone: string };

function termsDraft(p: PaymentTerms | null | undefined): TermsDraft {
  return {
    basis: p?.basis ?? "",
    deposit_percent: p?.deposit_percent != null ? String(p.deposit_percent) : "",
    due_days: p?.due_days != null ? String(p.due_days) : "",
    method: p?.method ?? "",
    note: p?.note ?? "",
  };
}

function termsBody(d: TermsDraft): PaymentTerms | null {
  if (!d.basis) return null;
  return {
    basis: d.basis as PaymentTerms["basis"],
    deposit_percent: d.basis === "deposit_balance" && d.deposit_percent.trim() ? Number(d.deposit_percent) : null,
    due_days: d.due_days.trim() ? Number(d.due_days) : null,
    method: (d.method || null) as PaymentTerms["method"],
    note: d.note.trim() || null,
  };
}

function contactsBody(list: ContactDraft[]): RoleContact[] {
  return list
    .map((c) => ({ role: c.role, name: c.name.trim() || null, email: c.email.trim() || null, phone: c.phone.trim() || null }))
    .filter((c) => c.name || c.email || c.phone);
}

/** The form part: state, the section to render and the body fields to send. */
export function useLegalTermsFields(s?: SupplierDetail): { section: React.ReactNode; body: () => SupplierBody } {
  const t = useT(MSG);
  const [legal, setLegal] = useState(s?.legal_entity ?? "");
  const [tax, setTax] = useState(s?.tax_code ?? "");
  const [terms, setTerms] = useState<TermsDraft>(() => termsDraft(s?.payment_terms));
  const [contacts, setContacts] = useState<ContactDraft[]>(() =>
    (s?.role_contacts ?? []).map((c) => ({ role: c.role, name: c.name ?? "", email: c.email ?? "", phone: c.phone ?? "" })),
  );
  const setTerm = (k: keyof TermsDraft) => (v: string) => setTerms((cur) => ({ ...cur, [k]: v }));
  const setContact = (i: number, k: keyof ContactDraft, v: string) =>
    setContacts((cur) => cur.map((c, j) => (j === i ? { ...c, [k]: v } : c)));

  const section = (
    <Section title={t("Legal and payment")} description={t("Who AA contracts with and pays, and how this supplier is usually paid. A contract may state its own terms.")}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("Legal entity")} hint={t("The registered company name.")}>
          <Input className={inputCls} maxLength={300} value={legal} onChange={(e) => setLegal(e.target.value)} />
        </Field>
        <Field label={t("Tax code")}>
          <Input className={inputCls} maxLength={64} value={tax} onChange={(e) => setTax(e.target.value)} />
        </Field>
        <Field label={t("Payment basis")}>
          <Select className={inputCls} value={terms.basis} onChange={(e) => setTerm("basis")(e.target.value)}>
            <option value="">{t("Not set")}</option>
            {TERMS_BASES.map((b) => (
              <option key={b} value={b}>
                {t(BASIS_LABEL[b])}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("Method")}>
          <Select className={inputCls} value={terms.method} onChange={(e) => setTerm("method")(e.target.value)} disabled={!terms.basis}>
            <option value="">{t("Not set")}</option>
            {TERMS_METHODS.map((m) => (
              <option key={m} value={m}>
                {t(METHOD_LABEL[m])}
              </option>
            ))}
          </Select>
        </Field>
        {terms.basis === "deposit_balance" && (
          <Field label={t("Deposit (%)")}>
            <Input className={inputCls} type="number" min={1} max={100} value={terms.deposit_percent} onChange={(e) => setTerm("deposit_percent")(e.target.value)} />
          </Field>
        )}
        <Field label={t("Due days")} hint={t("Before the service for a prepayment or deposit, after it otherwise.")}>
          <Input className={inputCls} type="number" min={0} max={365} value={terms.due_days} onChange={(e) => setTerm("due_days")(e.target.value)} disabled={!terms.basis} />
        </Field>
        <Field label={t("Terms note")} className="sm:col-span-2">
          <Input className={inputCls} maxLength={300} value={terms.note} onChange={(e) => setTerm("note")(e.target.value)} disabled={!terms.basis} />
        </Field>
      </div>
      <div className="mt-4 flex flex-col gap-3">
        <span className="text-[13px] font-medium text-ink">{t("Contacts by role")}</span>
        {contacts.map((c, i) => (
          <div key={i} className="grid gap-2 sm:grid-cols-[150px_minmax(0,1fr)_minmax(0,1fr)_140px_auto]">
            <Select className={inputCls} aria-label={t("Role")} value={c.role} onChange={(e) => setContact(i, "role", e.target.value)}>
              {CONTACT_ROLES.map((r) => (
                <option key={r} value={r}>
                  {t(ROLE_LABEL[r])}
                </option>
              ))}
            </Select>
            <Input className={inputCls} aria-label={t("Name")} placeholder={t("Name")} maxLength={200} value={c.name} onChange={(e) => setContact(i, "name", e.target.value)} />
            <Input className={inputCls} aria-label="E-mail" placeholder="E-mail" type="email" maxLength={254} value={c.email} onChange={(e) => setContact(i, "email", e.target.value)} />
            <Input className={inputCls} aria-label={t("Phone")} placeholder={t("Phone")} type="tel" maxLength={40} value={c.phone} onChange={(e) => setContact(i, "phone", e.target.value)} />
            <Button variant="ghost" aria-label={t("Remove contact")} onClick={() => setContacts((cur) => cur.filter((_, j) => j !== i))}>
              <Trash2 size={14} />
            </Button>
          </div>
        ))}
        {contacts.length < 12 && (
          <span>
            <Button variant="secondary" size="sm" onClick={() => setContacts((cur) => [...cur, { role: "reservations", name: "", email: "", phone: "" }])}>
              <Plus size={14} /> {t("Add contact")}
            </Button>
          </span>
        )}
      </div>
    </Section>
  );

  const body = (): SupplierBody => ({
    legal_entity: legal.trim() || null,
    tax_code: tax.trim() || null,
    payment_terms: termsBody(terms),
    role_contacts: contactsBody(contacts),
  });
  return { section, body };
}
