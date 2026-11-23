"use client";
// app/(staff)/suppliers/_parts/SupplierLegalTerms.tsx — AA-1158 part 2 (bk-067): the supplier's legal entity, tax code,
// standard payment terms and contacts by role. `LegalTermsFacts` shows them on the Profile tab; `useLegalTermsFields`
// is the matching part of the add / edit form (the API checks every field again: contacts need a role and a name,
// e-mail or phone; a deposit percentage only with the deposit + balance basis). The contact roles, payment bases and
// methods are an administrator's data (GET /booking/supplier-profile-options, review S289): labels in the UI language,
// active ones offered, a retired one kept (and marked) where this supplier already holds it.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import {
  DEPOSIT_BASIS,
  profileOptionsApi,
  type PaymentTerms,
  type ProfileOption,
  type ProfileOptionKind,
  type RoleContact,
  type SupplierBody,
  type SupplierDetail,
} from "@/lib/api/suppliers";
import { Button, Field, Input, Section, Select } from "@/ui";
import { Fact, FactGrid } from "@/app/(staff)/_parts/DetailHeader";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { useLang, useT } from "@/ui/core/useI18n";
import { MSG } from "./SupplierLegalTerms.messages";

export const PROFILE_OPTIONS_KEY = ["suppliers", "profile-options"] as const;

/** The choices by kind and a label reader in the UI language (the code itself while loading or unknown). */
export function useProfileOptions(): {
  options: (kind: ProfileOptionKind, keep?: string | null) => ProfileOption[];
  label: (kind: ProfileOptionKind, code: string | null | undefined) => string;
  loading: boolean;
} {
  const lang = useLang();
  const q = useQuery({ queryKey: PROFILE_OPTIONS_KEY, queryFn: profileOptionsApi.list, staleTime: 60_000 });
  const kinds = q.data?.kinds;
  const find = (kind: ProfileOptionKind, code: string) => kinds?.[kind]?.find((o) => o.code === code);
  return {
    loading: q.isLoading,
    options: (kind, keep) => (kinds?.[kind] ?? []).filter((o) => o.active || o.code === keep),
    label: (kind, code) => {
      if (!code) return "—";
      const o = find(kind, code);
      return o ? (lang === "vi" ? o.label_vi : o.label_en) : code;
    },
  };
}

/** One line of the standard terms ("Deposit, then the balance · 30% · 14 days · Bank transfer"). */
function useTermsText(): (p: PaymentTerms | null | undefined) => string {
  const t = useT(MSG);
  const { label } = useProfileOptions();
  return (p) => {
    if (!p) return "—";
    const parts: string[] = [label("terms_basis", p.basis)];
    if (p.deposit_percent != null) parts.push(`${p.deposit_percent}%`);
    if (p.due_days != null) parts.push(t("{0} days", p.due_days));
    if (p.method) parts.push(label("terms_method", p.method));
    if (p.note) parts.push(p.note);
    return parts.join(" · ");
  };
}

export function LegalTermsFacts({ s }: { s: SupplierDetail }) {
  const t = useT(MSG);
  const termsText = useTermsText();
  const { label } = useProfileOptions();
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
                  <span className="font-medium text-ink">{label("contact_role", c.role)}</span>: {[c.name, c.email, c.phone].filter(Boolean).join(" · ")}
                </li>
              ))}
            </ul>
          )}
        </Fact>
      </FactGrid>
    </Section>
  );
}

/** The `<option>`s of one kind: the active choices, plus `value` when it is a retired one this supplier holds. */
function OptionItems({ kind, value }: { kind: ProfileOptionKind; value: string }) {
  const t = useT(MSG);
  const lang = useLang();
  const { options, loading } = useProfileOptions();
  const list = options(kind, value);
  return (
    <>
      {loading && value && <option value={value}>{t("Loading the choices")}</option>}
      {list.map((o) => (
        <option key={o.code} value={o.code}>
          {(lang === "vi" ? o.label_vi : o.label_en) + (o.active ? "" : ` ${t("(retired)")}`)}
        </option>
      ))}
    </>
  );
}

type TermsDraft = { basis: string; deposit_percent: string; due_days: string; method: string; note: string };
type ContactDraft = { role: string; name: string; email: string; phone: string };

const text = (v: unknown): string => (v == null ? "" : String(v));

function termsDraft(p: PaymentTerms | null | undefined): TermsDraft {
  const d = p ?? ({} as Partial<PaymentTerms>);
  return {
    basis: text(d.basis),
    deposit_percent: text(d.deposit_percent),
    due_days: text(d.due_days),
    method: text(d.method),
    note: text(d.note),
  };
}

function termsBody(d: TermsDraft): PaymentTerms | null {
  if (!d.basis) return null;
  return {
    basis: d.basis,
    deposit_percent: d.basis === DEPOSIT_BASIS && d.deposit_percent.trim() ? Number(d.deposit_percent) : null,
    due_days: d.due_days.trim() ? Number(d.due_days) : null,
    method: d.method || null,
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
  const firstRole = useProfileOptions().options("contact_role")[0]?.code ?? "";
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
            <OptionItems kind="terms_basis" value={terms.basis} />
          </Select>
        </Field>
        <Field label={t("Method")}>
          <Select className={inputCls} value={terms.method} onChange={(e) => setTerm("method")(e.target.value)} disabled={!terms.basis}>
            <option value="">{t("Not set")}</option>
            <OptionItems kind="terms_method" value={terms.method} />
          </Select>
        </Field>
        {terms.basis === DEPOSIT_BASIS && (
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
              <OptionItems kind="contact_role" value={c.role} />
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
            <Button variant="secondary" size="sm" disabled={!firstRole} onClick={() => setContacts((cur) => [...cur, { role: firstRole, name: "", email: "", phone: "" }])}>
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
