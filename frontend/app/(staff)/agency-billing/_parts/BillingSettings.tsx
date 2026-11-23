"use client";
// app/(staff)/agency-billing/_parts/BillingSettings.tsx — Agency billing › Settings (AA-1037 R4; root ADR 0019 b10,
// b16, b17). Three data sets finance edits, never code: the AA legal entity that issues the invoices (one default),
// the tax rules (rate + wording per kind and recipient country, with an effective window) and the payment terms (the
// default set and per-agency terms: deposit %, balance days before departure, due days, warn / suspend days; S280:
// TermsSection.tsx, where an agency's terms are added and changed too).
// Every change is audited in Booking. billing_config.write = admin, finance. AA-1158: the payment details and the
// default flag are pending changes a second person approves (EntityChanges.tsx; billing_config.approve).
// AA-1037 extras G6: the terms clauses agencies see (e.g. price errors), as data: finance edits a clause (a draft
// again), the business owner (terms_clause.approve = admin) approves it; agencies only see approved clauses.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  KIND_LABEL,
  agencyBillingApi,
  type BillingEntity,
  type TaxRule,
  type TermsClause,
} from "@/lib/api/agencyBilling";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  DateText,
  Field,
  FormDrawer,
  Input,
  Pill,
  Section,
  Select,
  Textarea,
  useCan,
  useStaff,
  useToast,
} from "@/ui";
import { errorText } from "./shared";
import { ChangeDetailsDrawer, PendingEntityChanges } from "./EntityChanges";
import { TermsSection } from "./TermsSection";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./BillingSettings.messages";

export function BillingSettings() {
  return (
    <div className="flex flex-col gap-5">
      <EntitiesSection />
      <TaxRulesSection />
      <TermsSection />
      <TermsClausesSection />
    </div>
  );
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["agency-billing"] });
}

// ── Billing entity ───────────────────────────────────────────────────────────

const EMPTY_ENTITY = { code: "", legal_entity: "", tax_code: "", registered_office: "", country_iso2: "", invoice_inbox: "", payment_instructions: "", is_default: true };

function EntitiesSection() {
  const t = useT(MSG);
  const canEdit = useCan("billing_config.write");
  const toast = useToast();
  const invalidate = useInvalidate();
  const list = useQuery({ queryKey: ["agency-billing", "entities"], queryFn: agencyBillingApi.entities });
  const [form, setForm] = useState<typeof EMPTY_ENTITY | null>(null);
  const [changing, setChanging] = useState<BillingEntity | null>(null);
  const create = useMutation({
    mutationFn: (f: typeof EMPTY_ENTITY) =>
      agencyBillingApi.createEntity({
        code: f.code.trim(),
        legal_entity: f.legal_entity.trim(),
        tax_code: f.tax_code.trim() || null,
        registered_office: f.registered_office.trim() || null,
        country_iso2: f.country_iso2.trim() || null,
        invoice_inbox: f.invoice_inbox.trim() || null,
        payment_instructions: f.payment_instructions.trim() || null,
        is_default: f.is_default,
      }),
    onSuccess: (e) => {
      toast.success(e.pending_change ? t("Billing entity saved; its payment details and default flag wait for approval") : t("Billing entity saved"));
      setForm(null);
      invalidate();
    },
  });
  const setDefault = useMutation({
    mutationFn: (e: BillingEntity) =>
      agencyBillingApi.requestEntityChange(e.id, { set_payment_instructions: false, make_default: true, reason: t("Make it the default issuing entity") }),
    onSuccess: () => {
      toast.success(t("Change sent for approval"));
      invalidate();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const columns: ColumnDef<BillingEntity, unknown>[] = [
    { id: "code", header: t("Code"), cell: ({ row }) => <span className="font-semibold">{row.original.code}</span> },
    { id: "legal", header: t("Legal entity"), cell: ({ row }) => row.original.legal_entity },
    { id: "tax", header: t("Tax code"), cell: ({ row }) => row.original.tax_code ?? "—" },
    { id: "country", header: t("Country"), cell: ({ row }) => row.original.country_iso2 ?? "—" },
    {
      id: "state",
      header: "",
      cell: ({ row }) =>
        row.original.pending_change ? (
          <Pill tone="warning">{t("Change pending")}</Pill>
        ) : row.original.is_default ? (
          <Pill tone="success">{t("Default")}</Pill>
        ) : canEdit && row.original.active ? (
          <Button size="sm" variant="ghost" onClick={() => setDefault.mutate(row.original)}>{t("Make default")}</Button>
        ) : !row.original.active ? (
          <Pill>{t("Inactive")}</Pill>
        ) : null,
    },
    {
      id: "details",
      header: "",
      cell: ({ row }) =>
        canEdit && !row.original.pending_change ? (
          <Button size="sm" variant="ghost" onClick={() => setChanging(row.original)}>{t("Change payment details")}</Button>
        ) : null,
    },
  ];
  const f = form;
  return (
    <Section
      title={t("Issuing entity")}
      description={t("The AA legal entity printed on every invoice. The invoice keeps a copy taken when it is issued.")}
      actions={canEdit ? <Button onClick={() => setForm({ ...EMPTY_ENTITY })}>{t("Add entity")}</Button> : undefined}
    >
      <DataTable<BillingEntity>
        searchable={false}
        tableId="billing-entities"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        emptyTitle={t("No billing entity yet")}
        emptyDescription={t("Add the AA legal entity that issues invoices before issuing the first one.")}
      />
      <PendingEntityChanges entities={list.data?.items ?? []} onChanged={invalidate} />
      <ChangeDetailsDrawer entity={changing} onClose={() => setChanging(null)} onChanged={invalidate} />
      <FormDrawer
        open={f !== null}
        onClose={() => setForm(null)}
        title={t("Add billing entity")}
        onSubmit={() => f && create.mutate(f)}
        busy={create.isPending}
        error={create.isError ? errorText(create.error) : null}
      >
        {f && (
          <div className="flex flex-col gap-3">
            <Field label={t("Code")} required hint={t("Capital letters, digits, - or _ (e.g. AA-VN)")}>
              <Input value={f.code} onChange={(e) => setForm({ ...f, code: e.target.value.toUpperCase() })} />
            </Field>
            <Field label={t("Legal entity")} required>
              <Input value={f.legal_entity} onChange={(e) => setForm({ ...f, legal_entity: e.target.value })} />
            </Field>
            <Field label={t("Tax code")}>
              <Input value={f.tax_code} onChange={(e) => setForm({ ...f, tax_code: e.target.value })} />
            </Field>
            <Field label={t("Registered office")}>
              <Textarea value={f.registered_office} onChange={(e) => setForm({ ...f, registered_office: e.target.value })} />
            </Field>
            <Field label={t("Country")} hint={t("Two letters (VN)")}>
              <Input value={f.country_iso2} maxLength={2} onChange={(e) => setForm({ ...f, country_iso2: e.target.value.toUpperCase() })} />
            </Field>
            <Field label={t("Invoice inbox")}>
              <Input type="email" value={f.invoice_inbox} onChange={(e) => setForm({ ...f, invoice_inbox: e.target.value })} />
            </Field>
            <Field label={t("Payment instructions")} hint={t("Printed on the invoice; applies after a second person approves it")}>
              <Textarea value={f.payment_instructions} onChange={(e) => setForm({ ...f, payment_instructions: e.target.value })} />
            </Field>
            <Checkbox label={t("Default issuing entity")} checked={f.is_default} onChange={(e) => setForm({ ...f, is_default: e.target.checked })} />
          </div>
        )}
      </FormDrawer>
    </Section>
  );
}

// ── Tax rules ────────────────────────────────────────────────────────────────

const EMPTY_RULE = { code: "", invoice_kind: "", recipient_country: "", rate: "0", line_wording: "", footer_wording: "", effective_from: new Date().toISOString().slice(0, 10) };

function TaxRulesSection() {
  const t = useT(MSG);
  const tx = useTx();
  const canEdit = useCan("billing_config.write");
  const toast = useToast();
  const invalidate = useInvalidate();
  const list = useQuery({ queryKey: ["agency-billing", "tax-rules"], queryFn: agencyBillingApi.taxRules });
  const [form, setForm] = useState<typeof EMPTY_RULE | null>(null);
  const create = useMutation({
    mutationFn: (f: typeof EMPTY_RULE) =>
      agencyBillingApi.createTaxRule({
        code: f.code.trim(),
        invoice_kind: (f.invoice_kind || null) as TaxRule["invoice_kind"],
        recipient_country: f.recipient_country.trim() || null,
        rate: (Number(f.rate || "0") / 100).toFixed(4),
        line_wording: f.line_wording.trim(),
        footer_wording: f.footer_wording.trim() || null,
        effective_from: f.effective_from,
      }),
    onSuccess: () => {
      toast.success(t("Tax rule saved"));
      setForm(null);
      invalidate();
    },
  });
  const toggle = useMutation({
    mutationFn: (r: TaxRule) => agencyBillingApi.updateTaxRule(r.id, { active: !r.active }),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(errorText(e)),
  });
  const columns: ColumnDef<TaxRule, unknown>[] = [
    { id: "code", header: t("Code"), cell: ({ row }) => <span className="font-semibold">{row.original.code}</span> },
    { id: "kind", header: t("Invoice kind"), cell: ({ row }) => (row.original.invoice_kind ? tx(KIND_LABEL[row.original.invoice_kind]) : t("Any")) },
    { id: "country", header: t("Recipient country"), cell: ({ row }) => row.original.recipient_country ?? t("Any") },
    { id: "rate", header: t("Rate"), cell: ({ row }) => `${(Number(row.original.rate) * 100).toFixed(2)} %` },
    { id: "wording", header: t("Line"), cell: ({ row }) => row.original.line_wording },
    { id: "from", header: t("From"), cell: ({ row }) => <DateText value={row.original.effective_from} /> },
    { id: "to", header: t("Until"), cell: ({ row }) => (row.original.effective_to ? <DateText value={row.original.effective_to} /> : "—") },
    {
      id: "active",
      header: "",
      cell: ({ row }) =>
        canEdit ? (
          <Button size="sm" variant="ghost" onClick={() => toggle.mutate(row.original)}>
            {row.original.active ? t("Deactivate") : t("Activate")}
          </Button>
        ) : (
          <Pill tone={row.original.active ? "success" : "neutral"}>{row.original.active ? t("Active") : t("Inactive")}</Pill>
        ),
    },
  ];
  const f = form;
  return (
    <Section
      title={t("Tax rules")}
      description={t("The issue picks the active rule in force for the invoice kind and the agency's country, the most specific first. The footer wording is printed on the invoice.")}
      actions={canEdit ? <Button onClick={() => setForm({ ...EMPTY_RULE })}>{t("Add tax rule")}</Button> : undefined}
    >
      <DataTable<TaxRule>
        searchable={false}
        tableId="tax-rules"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        emptyTitle={t("No tax rule")}
        emptyDescription={t("Without a rule an invoice carries no tax line.")}
        tableMinWidth={900}
      />
      <FormDrawer
        open={f !== null}
        onClose={() => setForm(null)}
        title={t("Add tax rule")}
        onSubmit={() => f && create.mutate(f)}
        busy={create.isPending}
        error={create.isError ? errorText(create.error) : null}
      >
        {f && (
          <div className="flex flex-col gap-3">
            <Field label={t("Code")} required>
              <Input value={f.code} onChange={(e) => setForm({ ...f, code: e.target.value.toUpperCase() })} />
            </Field>
            <Field label={t("Invoice kind")}>
              <Select
                value={f.invoice_kind}
                onChange={(e) => setForm({ ...f, invoice_kind: e.target.value })}
                options={[{ value: "", label: t("Any") }, { value: "saas", label: tx(KIND_LABEL.saas) }, { value: "settlement", label: tx(KIND_LABEL.settlement) }]}
              />
            </Field>
            <Field label={t("Recipient country")} hint={t("Two letters; empty = any country")}>
              <Input value={f.recipient_country} maxLength={2} onChange={(e) => setForm({ ...f, recipient_country: e.target.value.toUpperCase() })} />
            </Field>
            <Field label={t("Rate (%)")} required>
              <Input type="number" min={0} max={99.99} step="0.01" value={f.rate} onChange={(e) => setForm({ ...f, rate: e.target.value })} />
            </Field>
            <Field label={t("Tax line wording")} required hint={t("For example: VAT 10%")}>
              <Input value={f.line_wording} onChange={(e) => setForm({ ...f, line_wording: e.target.value })} />
            </Field>
            <Field label={t("Footer wording")}>
              <Textarea value={f.footer_wording} onChange={(e) => setForm({ ...f, footer_wording: e.target.value })} />
            </Field>
            <Field label={t("In force from")} required>
              <Input type="date" value={f.effective_from} onChange={(e) => setForm({ ...f, effective_from: e.target.value })} />
            </Field>
          </div>
        )}
      </FormDrawer>
    </Section>
  );
}

// ── Terms clauses (AA-1037 extras G6) ────────────────────────────────────────

type ClauseForm = { code: string; title: string; body: string; free_cancel_days: string; isNew: boolean };

function TermsClausesSection() {
  const t = useT(MSG);
  const canEdit = useCan("billing_config.write");
  const canApprove = useCan("terms_clause.approve");
  const me = useStaff();
  const toast = useToast();
  const invalidate = useInvalidate();
  const list = useQuery({ queryKey: ["agency-billing", "clauses"], queryFn: agencyBillingApi.clauses });
  const [form, setForm] = useState<ClauseForm | null>(null);
  const [approving, setApproving] = useState<TermsClause | null>(null);
  const save = useMutation({
    mutationFn: (f: ClauseForm) =>
      agencyBillingApi.putClause(f.code.trim(), {
        title: f.title.trim(),
        body: f.body.trim(),
        free_cancel_days: f.free_cancel_days.trim() === "" ? null : Number(f.free_cancel_days),
      }),
    onSuccess: () => {
      toast.success(t("Clause saved as a draft; it reaches agencies once approved"));
      setForm(null);
      invalidate();
    },
  });
  const approve = useMutation({
    mutationFn: (c: TermsClause) => agencyBillingApi.approveClause(c.code, c.version),
    onSuccess: () => {
      toast.success(t("Clause approved; agencies now see it"));
      setApproving(null);
      invalidate();
    },
    onError: (e) => {
      toast.error(errorText(e));
      setApproving(null);
    },
  });
  const columns: ColumnDef<TermsClause, unknown>[] = [
    {
      id: "title",
      header: t("Clause"),
      cell: ({ row }) => (
        <div className="flex max-w-[640px] flex-col gap-1">
          {/* Clause text is data (the wording agencies read), shown exactly as stored, never through the phrasebook. */}
          <span className="font-semibold">{row.original.title}</span>
          <span className="whitespace-pre-line text-[13px] text-body">{row.original.body}</span>
        </div>
      ),
    },
    {
      id: "window",
      header: t("Free-cancel window"),
      cell: ({ row }) => (row.original.free_cancel_days == null ? "—" : t("{0} days", row.original.free_cancel_days)),
    },
    {
      id: "status",
      header: t("Status"),
      cell: ({ row }) =>
        row.original.status === "approved" ? (
          <Pill tone="success">{t("Approved v{0}", row.original.version)}</Pill>
        ) : (
          <Pill tone="warning">{t("Draft v{0}, not shown to agencies", row.original.version)}</Pill>
        ),
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <span className="flex flex-wrap gap-2">
          {canEdit && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                setForm({
                  code: row.original.code,
                  // The stored wording, never a translation of it: saving must not change the clause text.
                  title: row.original.title,
                  body: row.original.body,
                  free_cancel_days: row.original.free_cancel_days == null ? "" : String(row.original.free_cancel_days),
                  isNew: false,
                })
              }
            >
              {t("Edit")}
            </Button>
          )}
          {canApprove && row.original.status === "draft" && row.original.updated_by !== me.cis_user_id && (
            <Button size="sm" variant="primary" onClick={() => setApproving(row.original)}>{t("Approve")}</Button>
          )}
        </span>
      ),
    },
  ];
  const f = form;
  return (
    <Section
      title={t("Terms clauses")}
      description={t("Clauses of AA's terms that agencies read with their payment terms (for example how AA corrects a clearly wrong price). A clause is a draft until the business owner approves it; any edit makes it a draft again.")}
      actions={canEdit ? <Button onClick={() => setForm({ code: "", title: "", body: "", free_cancel_days: "", isNew: true })}>{t("Add clause")}</Button> : undefined}
    >
      <DataTable<TermsClause>
        searchable={false}
        tableId="terms-clauses"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        emptyTitle={t("No terms clause")}
        emptyDescription={t("Add a clause; agencies see it once it is approved.")}
        tableMinWidth={900}
      />
      <FormDrawer
        open={f !== null}
        onClose={() => setForm(null)}
        title={f?.isNew ? t("Add terms clause") : t("Edit terms clause")}
        onSubmit={() => f && save.mutate(f)}
        busy={save.isPending}
        error={save.isError ? errorText(save.error) : null}
      >
        {f && (
          <div className="flex flex-col gap-3">
            <Field label={t("Code")} required hint={t("Lower-case letters, digits or _ (for example price_error)")}>
              <Input value={f.code} disabled={!f.isNew} onChange={(e) => setForm({ ...f, code: e.target.value.toLowerCase() })} />
            </Field>
            <Field label={t("Title")} required>
              <Input value={f.title} maxLength={120} onChange={(e) => setForm({ ...f, title: e.target.value })} />
            </Field>
            <Field label={t("Wording")} required hint={t("Saved as a draft; the business owner approves the wording before agencies see it")}>
              <Textarea value={f.body} rows={8} maxLength={4000} onChange={(e) => setForm({ ...f, body: e.target.value })} />
            </Field>
            <Field label={t("Free-cancel window (days)")} hint={t("Days the agency may cancel free of charge after AA's notice; empty when the clause has none")}>
              <Input type="number" min={0} max={60} value={f.free_cancel_days} onChange={(e) => setForm({ ...f, free_cancel_days: e.target.value })} />
            </Field>
          </div>
        )}
      </FormDrawer>
      <ConfirmDialog
        open={approving !== null}
        onClose={() => setApproving(null)}
        onConfirm={() => approving && approve.mutate(approving)}
        title={t("Approve \"{0}\"?", approving?.title ?? "")}
        message={t("Agencies see this wording in their terms from now on. A later edit makes it a draft again.")}
        confirmLabel={t("Approve")}
        busy={approve.isPending}
      />
    </Section>
  );
}
