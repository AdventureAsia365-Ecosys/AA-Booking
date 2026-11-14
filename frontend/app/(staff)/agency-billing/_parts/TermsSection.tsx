"use client";
// app/(staff)/agency-billing/_parts/TermsSection.tsx — Agency billing › Settings › Payment terms (AA-1037 R4, root ADR
// 0019 b16 / b17; moved out of BillingSettings.tsx in S280). The default terms of every agency, and an agency's own
// contract terms that replace them as a whole: deposit %, balance days before departure, software fee and settlement
// due days, warn / suspend days. billing_config.write = admin, finance.
//
// S280 (AA-1096 settings UI audit): an agency's terms are added and changed here too (PUT
// /booking/billing-terms/agencies/{tenant}, agency picker from the active B2B partners), not only removed; each
// agency's line shows every field in force for it. The deposit keeps the typed text (no float noise), every day count
// shows its allowed range, and an empty or out-of-range field is refused under it (lib/billingTermsForm.ts) instead of
// being sent as 0 or answered with a generic error.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { agencyBillingApi, type BillingTerms } from "@/lib/api/agencyBilling";
import { agencyOpsApi } from "@/lib/api/agencyOps";
import { DAY_RANGE, parseTerms, pctText, termsToText, type DayKey, type TermsErrors, type TermsText } from "@/lib/billingTermsForm";
import { Button, ConfirmDialog, Field, FormDrawer, Input, Section, Select, useCan, useToast } from "@/ui";
import { errorText } from "./shared";

const TERM_FIELDS: { key: DayKey; label: string; hint: string }[] = [
  { key: "balance_days_before_departure", label: "Balance due before departure (days)", hint: "Booked later than this: billed in full." },
  { key: "saas_due_days", label: "Software fee due after issue (days)", hint: "" },
  { key: "settlement_due_days", label: "Settlement due after issue (days)", hint: "" },
  { key: "warn_after_days", label: "Warn after (overdue days)", hint: "The agency's bell rings and its account enters its grace state." },
  { key: "suspend_after_days", label: "Suspend after (overdue days)", hint: "More than the warning days." },
];

function rangeHint(key: DayKey, hint: string): string {
  const { min, max } = DAY_RANGE[key];
  return `${hint ? `${hint} ` : ""}Allowed: ${min}–${max} days.`;
}

/** The terms fields, shared by the default terms and an agency's drawer. */
function TermsFields({ value, errors, disabled, onChange }: {
  value: TermsText;
  errors: TermsErrors;
  disabled: boolean;
  onChange: (next: TermsText) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <Field label="Deposit (%)" hint="Billed when an agency booking is confirmed. Allowed: 0–100." error={errors.deposit}>
        <Input inputMode="decimal" disabled={disabled} value={value.deposit} onChange={(e) => onChange({ ...value, deposit: e.target.value })} />
      </Field>
      {TERM_FIELDS.map((tf) => (
        <Field key={tf.key} label={tf.label} hint={rangeHint(tf.key, tf.hint)} error={errors[tf.key]}>
          <Input
            type="number"
            min={DAY_RANGE[tf.key].min}
            max={DAY_RANGE[tf.key].max}
            disabled={disabled}
            value={value[tf.key]}
            onChange={(e) => onChange({ ...value, [tf.key]: e.target.value })}
          />
        </Field>
      ))}
      <Field label="Software fee currency">
        <Input value={value.saas_currency} disabled />
      </Field>
    </div>
  );
}

function termsLine(t: BillingTerms): string {
  return [
    `deposit ${pctText(t.deposit_pct)} %`,
    `balance ${t.balance_days_before_departure} d before departure`,
    `software fee due ${t.saas_due_days} d`,
    `settlement due ${t.settlement_due_days} d`,
    `warn ${t.warn_after_days} d`,
    `suspend ${t.suspend_after_days} d`,
  ].join(" · ");
}

type Editing = { tenantId: string; name: string | null; text: TermsText; isNew: boolean };

function AgencyTermsDrawer({ editing, taken, onClose }: { editing: Editing; taken: Set<string>; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState(editing);
  const [errors, setErrors] = useState<TermsErrors>({});
  const [missing, setMissing] = useState(false);
  const agencies = useQuery({ queryKey: ["b2b-desk", "agencies"], queryFn: agencyOpsApi.agencies, enabled: editing.isNew, retry: false });
  const save = useMutation({
    mutationFn: (body: BillingTerms) => agencyBillingApi.putAgencyTerms(form.tenantId, body),
    onSuccess: () => {
      toast.success("Agency terms saved");
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
      onClose();
    },
  });
  function submit() {
    const { body, errors: found } = parseTerms(form.text);
    setErrors(found);
    setMissing(!form.tenantId);
    if (body && form.tenantId) save.mutate(body);
  }
  const options = (agencies.data?.items ?? []).filter((a) => !taken.has(a.tenant_id)).map((a) => ({ value: a.tenant_id, label: a.agency_name }));
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={editing.isNew ? "Add agency terms" : `Terms of ${editing.name ?? "this agency"}`}
      onSubmit={submit}
      submitLabel="Save terms"
      busy={save.isPending}
      error={save.isError ? errorText(save.error) : missing ? "Pick an agency." : null}
      width={640}
    >
      <div className="flex flex-col gap-4" data-testid="agency-terms-drawer">
        <p className="text-[13px] text-muted">These terms replace the default terms for this agency as a whole.</p>
        {editing.isNew && (
          <Field label="Agency" required hint={agencies.isError ? "The agency list could not be loaded." : "Active B2B agencies without their own terms."}>
            <Select
              value={form.tenantId}
              onChange={(e) => setForm({ ...form, tenantId: e.target.value })}
              options={[{ value: "", label: "Pick an agency" }, ...options]}
            />
          </Field>
        )}
        <TermsFields value={form.text} errors={errors} disabled={false} onChange={(text) => setForm({ ...form, text })} />
      </div>
    </FormDrawer>
  );
}

function Overrides({ overrides, defaults, canEdit }: { overrides: BillingTerms[]; defaults: BillingTerms | null; canEdit: boolean }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<Editing | null>(null);
  const [removing, setRemoving] = useState<BillingTerms | null>(null);
  const remove = useMutation({
    mutationFn: (tenantId: string) => agencyBillingApi.deleteAgencyTerms(tenantId),
    onSuccess: () => {
      toast.success("The agency follows the default terms again");
      setRemoving(null);
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const taken = new Set(overrides.map((o) => o.party_tenant_id ?? ""));
  return (
    <div className="mt-4" data-testid="agency-terms">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold">Agency terms</h3>
        {canEdit && defaults && (
          <Button size="sm" variant="secondary" onClick={() => setEditing({ tenantId: "", name: null, text: termsToText(defaults), isNew: true })}>
            Add agency terms
          </Button>
        )}
      </div>
      {overrides.length === 0 ? (
        <p className="text-[13px] text-muted">Every agency follows the default terms.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {overrides.map((o) => (
            <li key={o.party_tenant_id ?? ""} className="flex flex-wrap items-center gap-3 text-[13px]">
              <span className="font-semibold">{o.agency_name ?? o.party_tenant_id}</span>
              <span className="min-w-0 break-words text-body">{termsLine(o)}</span>
              {canEdit && (
                <span className="flex gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setEditing({ tenantId: o.party_tenant_id ?? "", name: o.agency_name ?? null, text: termsToText(o), isNew: false })}>
                    Edit
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setRemoving(o)}>Use the default</Button>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {editing && <AgencyTermsDrawer editing={editing} taken={taken} onClose={() => setEditing(null)} />}
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => removing?.party_tenant_id && remove.mutate(removing.party_tenant_id)}
        title="Use the default terms"
        message={`${removing?.agency_name ?? "This agency"} follows the default terms from now on; its own terms are removed.`}
        confirmLabel="Use the default"
        busy={remove.isPending}
      />
    </div>
  );
}

function SaveBar({ busy, onCancel, onSave }: { busy: boolean; onCancel: () => void; onSave: () => void }) {
  return (
    <span className="flex gap-2">
      <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      <Button variant="primary" disabled={busy} onClick={onSave}>Save terms</Button>
    </span>
  );
}

/** The terms shown: the edited draft, else the stored default row, else nothing yet. */
function shownTerms(draft: TermsText | null, current: BillingTerms | null): TermsText | null {
  return draft ?? (current ? termsToText(current) : null);
}

export function TermsSection() {
  const canEdit = useCan("billing_config.write");
  const toast = useToast();
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["agency-billing", "terms"], queryFn: agencyBillingApi.terms });
  const current = list.data?.items.find((t) => !t.party_tenant_id) ?? null;
  const [draft, setDraft] = useState<TermsText | null>(null);
  const [errors, setErrors] = useState<TermsErrors>({});
  const save = useMutation({
    mutationFn: (body: BillingTerms) => agencyBillingApi.putDefaultTerms(body),
    onSuccess: () => {
      toast.success("Default terms saved");
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  function submit() {
    if (!draft) return;
    const { body, errors: found } = parseTerms(draft);
    setErrors(found);
    if (body) save.mutate(body);
  }
  const shown = shownTerms(draft, current);
  return (
    <Section
      title="Payment terms"
      description="The default terms of every agency; an agency's own contract terms replace them as a whole."
      actions={canEdit && draft ? <SaveBar busy={save.isPending} onCancel={() => { setDraft(null); setErrors({}); }} onSave={submit} /> : undefined}
    >
      {shown ? (
        <TermsFields value={shown} errors={errors} disabled={!canEdit} onChange={setDraft} />
      ) : (
        <p className="text-[13px] text-muted">{list.isLoading ? "Loading…" : "No default terms row."}</p>
      )}
      <Overrides overrides={(list.data?.items ?? []).filter((t) => t.party_tenant_id)} defaults={current} canEdit={canEdit} />
    </Section>
  );
}
