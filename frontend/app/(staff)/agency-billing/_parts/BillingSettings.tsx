"use client";
// app/(staff)/agency-billing/_parts/BillingSettings.tsx — Agency billing › Settings (AA-1037 R4; root ADR 0019 b10,
// b16, b17). Three data sets finance edits, never code: the AA legal entity that issues the invoices (one default),
// the tax rules (rate + wording per kind and recipient country, with an effective window) and the payment terms (the
// default set and per-agency overrides: deposit %, balance days before departure, due days, warn / suspend days).
// Every change is audited in Booking. billing_config.write = admin, finance.
// AA-1037 extras G6: the terms clauses agencies see (e.g. price errors), as data: finance edits a clause (a draft
// again), the business owner (terms_clause.approve = admin) approves it; agencies only see approved clauses.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  KIND_LABEL,
  agencyBillingApi,
  type BillingEntity,
  type BillingTerms,
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
  const canEdit = useCan("billing_config.write");
  const toast = useToast();
  const invalidate = useInvalidate();
  const list = useQuery({ queryKey: ["agency-billing", "entities"], queryFn: agencyBillingApi.entities });
  const [form, setForm] = useState<typeof EMPTY_ENTITY | null>(null);
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
    onSuccess: () => {
      toast.success("Billing entity saved");
      setForm(null);
      invalidate();
    },
  });
  const setDefault = useMutation({
    mutationFn: (e: BillingEntity) => agencyBillingApi.updateEntity(e.id, { is_default: true }),
    onSuccess: () => {
      toast.success("Default billing entity changed");
      invalidate();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const columns: ColumnDef<BillingEntity, unknown>[] = [
    { id: "code", header: "Code", cell: ({ row }) => <span className="font-semibold">{row.original.code}</span> },
    { id: "legal", header: "Legal entity", cell: ({ row }) => row.original.legal_entity },
    { id: "tax", header: "Tax code", cell: ({ row }) => row.original.tax_code ?? "—" },
    { id: "country", header: "Country", cell: ({ row }) => row.original.country_iso2 ?? "—" },
    {
      id: "state",
      header: "",
      cell: ({ row }) =>
        row.original.is_default ? (
          <Pill tone="success">Default</Pill>
        ) : canEdit && row.original.active ? (
          <Button size="sm" variant="ghost" onClick={() => setDefault.mutate(row.original)}>Make default</Button>
        ) : !row.original.active ? (
          <Pill>Inactive</Pill>
        ) : null,
    },
  ];
  const f = form;
  return (
    <Section
      title="Issuing entity"
      description="The AA legal entity printed on every invoice. The invoice keeps a copy taken when it is issued."
      actions={canEdit ? <Button onClick={() => setForm({ ...EMPTY_ENTITY })}>Add entity</Button> : undefined}
    >
      <DataTable<BillingEntity>
        searchable={false}
        tableId="billing-entities"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        emptyTitle="No billing entity yet"
        emptyDescription="Add the AA legal entity that issues invoices before issuing the first one."
      />
      <FormDrawer
        open={f !== null}
        onClose={() => setForm(null)}
        title="Add billing entity"
        onSubmit={() => f && create.mutate(f)}
        busy={create.isPending}
        error={create.isError ? errorText(create.error) : null}
      >
        {f && (
          <div className="flex flex-col gap-3">
            <Field label="Code" required hint="Capital letters, digits, - or _ (e.g. AA-VN)">
              <Input value={f.code} onChange={(e) => setForm({ ...f, code: e.target.value.toUpperCase() })} />
            </Field>
            <Field label="Legal entity" required>
              <Input value={f.legal_entity} onChange={(e) => setForm({ ...f, legal_entity: e.target.value })} />
            </Field>
            <Field label="Tax code">
              <Input value={f.tax_code} onChange={(e) => setForm({ ...f, tax_code: e.target.value })} />
            </Field>
            <Field label="Registered office">
              <Textarea value={f.registered_office} onChange={(e) => setForm({ ...f, registered_office: e.target.value })} />
            </Field>
            <Field label="Country" hint="Two letters (VN)">
              <Input value={f.country_iso2} maxLength={2} onChange={(e) => setForm({ ...f, country_iso2: e.target.value.toUpperCase() })} />
            </Field>
            <Field label="Invoice inbox">
              <Input type="email" value={f.invoice_inbox} onChange={(e) => setForm({ ...f, invoice_inbox: e.target.value })} />
            </Field>
            <Field label="Payment instructions" hint="Printed on the invoice: where and how the agency pays">
              <Textarea value={f.payment_instructions} onChange={(e) => setForm({ ...f, payment_instructions: e.target.value })} />
            </Field>
            <Checkbox label="Default issuing entity" checked={f.is_default} onChange={(e) => setForm({ ...f, is_default: e.target.checked })} />
          </div>
        )}
      </FormDrawer>
    </Section>
  );
}

// ── Tax rules ────────────────────────────────────────────────────────────────

const EMPTY_RULE = { code: "", invoice_kind: "", recipient_country: "", rate: "0", line_wording: "", footer_wording: "", effective_from: new Date().toISOString().slice(0, 10) };

function TaxRulesSection() {
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
      toast.success("Tax rule saved");
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
    { id: "code", header: "Code", cell: ({ row }) => <span className="font-semibold">{row.original.code}</span> },
    { id: "kind", header: "Invoice kind", cell: ({ row }) => (row.original.invoice_kind ? KIND_LABEL[row.original.invoice_kind] : "Any") },
    { id: "country", header: "Recipient country", cell: ({ row }) => row.original.recipient_country ?? "Any" },
    { id: "rate", header: "Rate", cell: ({ row }) => `${(Number(row.original.rate) * 100).toFixed(2)} %` },
    { id: "wording", header: "Line", cell: ({ row }) => row.original.line_wording },
    { id: "from", header: "From", cell: ({ row }) => <DateText value={row.original.effective_from} /> },
    { id: "to", header: "Until", cell: ({ row }) => (row.original.effective_to ? <DateText value={row.original.effective_to} /> : "—") },
    {
      id: "active",
      header: "",
      cell: ({ row }) =>
        canEdit ? (
          <Button size="sm" variant="ghost" onClick={() => toggle.mutate(row.original)}>
            {row.original.active ? "Deactivate" : "Activate"}
          </Button>
        ) : (
          <Pill tone={row.original.active ? "success" : "neutral"}>{row.original.active ? "Active" : "Inactive"}</Pill>
        ),
    },
  ];
  const f = form;
  return (
    <Section
      title="Tax rules"
      description="The issue picks the active rule in force for the invoice kind and the agency's country, the most specific first. The footer wording is printed on the invoice."
      actions={canEdit ? <Button onClick={() => setForm({ ...EMPTY_RULE })}>Add tax rule</Button> : undefined}
    >
      <DataTable<TaxRule>
        searchable={false}
        tableId="tax-rules"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        emptyTitle="No tax rule"
        emptyDescription="Without a rule an invoice carries no tax line."
        tableMinWidth={900}
      />
      <FormDrawer
        open={f !== null}
        onClose={() => setForm(null)}
        title="Add tax rule"
        onSubmit={() => f && create.mutate(f)}
        busy={create.isPending}
        error={create.isError ? errorText(create.error) : null}
      >
        {f && (
          <div className="flex flex-col gap-3">
            <Field label="Code" required>
              <Input value={f.code} onChange={(e) => setForm({ ...f, code: e.target.value.toUpperCase() })} />
            </Field>
            <Field label="Invoice kind">
              <Select
                value={f.invoice_kind}
                onChange={(e) => setForm({ ...f, invoice_kind: e.target.value })}
                options={[{ value: "", label: "Any" }, { value: "saas", label: KIND_LABEL.saas }, { value: "settlement", label: KIND_LABEL.settlement }]}
              />
            </Field>
            <Field label="Recipient country" hint="Two letters; empty = any country">
              <Input value={f.recipient_country} maxLength={2} onChange={(e) => setForm({ ...f, recipient_country: e.target.value.toUpperCase() })} />
            </Field>
            <Field label="Rate (%)" required>
              <Input type="number" min={0} max={99.99} step="0.01" value={f.rate} onChange={(e) => setForm({ ...f, rate: e.target.value })} />
            </Field>
            <Field label="Tax line wording" required hint="For example: VAT 10%">
              <Input value={f.line_wording} onChange={(e) => setForm({ ...f, line_wording: e.target.value })} />
            </Field>
            <Field label="Footer wording">
              <Textarea value={f.footer_wording} onChange={(e) => setForm({ ...f, footer_wording: e.target.value })} />
            </Field>
            <Field label="In force from" required>
              <Input type="date" value={f.effective_from} onChange={(e) => setForm({ ...f, effective_from: e.target.value })} />
            </Field>
          </div>
        )}
      </FormDrawer>
    </Section>
  );
}

// ── Terms ────────────────────────────────────────────────────────────────────

const TERM_FIELDS: { key: keyof BillingTerms; label: string; hint: string }[] = [
  { key: "balance_days_before_departure", label: "Balance due before departure (days)", hint: "Booked later than this: billed in full" },
  { key: "saas_due_days", label: "Software fee due after issue (days)", hint: "" },
  { key: "settlement_due_days", label: "Settlement due after issue (days)", hint: "" },
  { key: "warn_after_days", label: "Warn after (overdue days)", hint: "The agency's bell rings and its account enters its grace state" },
  { key: "suspend_after_days", label: "Suspend after (overdue days)", hint: "Must be more than the warning days" },
];

function TermsSection() {
  const canEdit = useCan("billing_config.write");
  const toast = useToast();
  const invalidate = useInvalidate();
  const list = useQuery({ queryKey: ["agency-billing", "terms"], queryFn: agencyBillingApi.terms });
  const current = list.data?.items.find((t) => !t.party_tenant_id) ?? null;
  const [draft, setDraft] = useState<BillingTerms | null>(null);
  const shown = draft ?? current;
  const save = useMutation({
    mutationFn: (t: BillingTerms) =>
      agencyBillingApi.putDefaultTerms({
        deposit_pct: t.deposit_pct,
        balance_days_before_departure: Number(t.balance_days_before_departure),
        saas_due_days: Number(t.saas_due_days),
        settlement_due_days: Number(t.settlement_due_days),
        warn_after_days: Number(t.warn_after_days),
        suspend_after_days: Number(t.suspend_after_days),
        saas_currency: t.saas_currency,
      }),
    onSuccess: () => {
      toast.success("Default terms saved");
      setDraft(null);
      invalidate();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const removeOverride = useMutation({
    mutationFn: (tenantId: string) => agencyBillingApi.deleteAgencyTerms(tenantId),
    onSuccess: () => {
      toast.success("The agency follows the default terms again");
      invalidate();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const overrides = (list.data?.items ?? []).filter((t) => t.party_tenant_id);
  return (
    <Section
      title="Payment terms"
      description="The default terms of every agency; an agency's contract terms override them as a whole."
      actions={
        canEdit && draft ? (
          <span className="flex gap-2">
            <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
            <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate(draft)}>Save terms</Button>
          </span>
        ) : undefined
      }
    >
      {shown ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <Field label="Deposit (%)" hint="Billed when an agency booking is confirmed">
            <Input
              type="number"
              min={0}
              max={100}
              step="0.01"
              disabled={!canEdit}
              value={draft ? String(Number(draft.deposit_pct) * 100) : String(Number(shown.deposit_pct) * 100)}
              onChange={(e) => setDraft({ ...shown, ...draft, deposit_pct: (Number(e.target.value || "0") / 100).toFixed(4) })}
            />
          </Field>
          {TERM_FIELDS.map((tf) => (
            <Field key={tf.key} label={tf.label} hint={tf.hint || undefined}>
              <Input
                type="number"
                min={0}
                disabled={!canEdit}
                value={String((draft ?? shown)[tf.key] ?? "")}
                onChange={(e) => setDraft({ ...shown, ...draft, [tf.key]: e.target.value } as BillingTerms)}
              />
            </Field>
          ))}
          <Field label="Software fee currency">
            <Input value={shown.saas_currency} disabled />
          </Field>
        </div>
      ) : (
        <p className="text-[13px] text-muted">{list.isLoading ? "Loading…" : "No default terms row."}</p>
      )}
      {overrides.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-[13px] font-semibold">Agency overrides</h3>
          <ul className="flex flex-col gap-2">
            {overrides.map((o) => (
              <li key={o.party_tenant_id ?? ""} className="flex flex-wrap items-center gap-3 text-[13px]">
                <span className="font-semibold">{o.agency_name}</span>
                <span>{`deposit ${(Number(o.deposit_pct) * 100).toFixed(0)} %, warn ${o.warn_after_days} d, suspend ${o.suspend_after_days} d`}</span>
                {canEdit && (
                  <Button size="sm" variant="ghost" onClick={() => o.party_tenant_id && removeOverride.mutate(o.party_tenant_id)}>
                    Use the default
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Section>
  );
}

// ── Terms clauses (AA-1037 extras G6) ────────────────────────────────────────

type ClauseForm = { code: string; title: string; body: string; free_cancel_days: string; isNew: boolean };

function TermsClausesSection() {
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
      toast.success("Clause saved as a draft; it reaches agencies once approved");
      setForm(null);
      invalidate();
    },
  });
  const approve = useMutation({
    mutationFn: (c: TermsClause) => agencyBillingApi.approveClause(c.code, c.version),
    onSuccess: () => {
      toast.success("Clause approved; agencies now see it");
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
      header: "Clause",
      cell: ({ row }) => (
        <div className="flex max-w-[640px] flex-col gap-1">
          <span className="font-semibold">{row.original.title}</span>
          <span className="whitespace-pre-line text-[13px] text-body">{row.original.body}</span>
        </div>
      ),
    },
    {
      id: "window",
      header: "Free-cancel window",
      cell: ({ row }) => (row.original.free_cancel_days == null ? "—" : `${row.original.free_cancel_days} days`),
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) =>
        row.original.status === "approved" ? (
          <Pill tone="success">{`Approved v${row.original.version}`}</Pill>
        ) : (
          <Pill tone="warning">{`Draft v${row.original.version}, not shown to agencies`}</Pill>
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
                  title: row.original.title,
                  body: row.original.body,
                  free_cancel_days: row.original.free_cancel_days == null ? "" : String(row.original.free_cancel_days),
                  isNew: false,
                })
              }
            >
              Edit
            </Button>
          )}
          {canApprove && row.original.status === "draft" && row.original.updated_by !== me.cis_user_id && (
            <Button size="sm" variant="primary" onClick={() => setApproving(row.original)}>Approve</Button>
          )}
        </span>
      ),
    },
  ];
  const f = form;
  return (
    <Section
      title="Terms clauses"
      description="Clauses of AA's terms that agencies read with their payment terms (for example how AA corrects a clearly wrong price). A clause is a draft until the business owner approves it; any edit makes it a draft again."
      actions={canEdit ? <Button onClick={() => setForm({ code: "", title: "", body: "", free_cancel_days: "", isNew: true })}>Add clause</Button> : undefined}
    >
      <DataTable<TermsClause>
        searchable={false}
        tableId="terms-clauses"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        emptyTitle="No terms clause"
        emptyDescription="Add a clause; agencies see it once it is approved."
        tableMinWidth={900}
      />
      <FormDrawer
        open={f !== null}
        onClose={() => setForm(null)}
        title={f?.isNew ? "Add terms clause" : "Edit terms clause"}
        onSubmit={() => f && save.mutate(f)}
        busy={save.isPending}
        error={save.isError ? errorText(save.error) : null}
      >
        {f && (
          <div className="flex flex-col gap-3">
            <Field label="Code" required hint="Lower-case letters, digits or _ (for example price_error)">
              <Input value={f.code} disabled={!f.isNew} onChange={(e) => setForm({ ...f, code: e.target.value.toLowerCase() })} />
            </Field>
            <Field label="Title" required>
              <Input value={f.title} maxLength={120} onChange={(e) => setForm({ ...f, title: e.target.value })} />
            </Field>
            <Field label="Wording" required hint="Saved as a draft; the business owner approves the wording before agencies see it">
              <Textarea value={f.body} rows={8} maxLength={4000} onChange={(e) => setForm({ ...f, body: e.target.value })} />
            </Field>
            <Field label="Free-cancel window (days)" hint="Days the agency may cancel free of charge after AA's notice; empty when the clause has none">
              <Input type="number" min={0} max={60} value={f.free_cancel_days} onChange={(e) => setForm({ ...f, free_cancel_days: e.target.value })} />
            </Field>
          </div>
        )}
      </FormDrawer>
      <ConfirmDialog
        open={approving !== null}
        onClose={() => setApproving(null)}
        onConfirm={() => approving && approve.mutate(approving)}
        title={`Approve "${approving?.title ?? ""}"?`}
        message="Agencies see this wording in their terms from now on. A later edit makes it a draft again."
        confirmLabel="Approve"
        busy={approve.isPending}
      />
    </Section>
  );
}
