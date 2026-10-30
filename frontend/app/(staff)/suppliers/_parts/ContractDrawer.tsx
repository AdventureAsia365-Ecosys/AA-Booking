"use client";
// app/(staff)/suppliers/_parts/ContractDrawer.tsx — New contract / Edit draft contract (AA-822, bk-008; ADR 0005
// decision 2). A contract holds a supplier's terms for a period: validity (open-ended when "valid to" is empty), the
// supplier currency (rates under it convert with the AA-810 FX rates), payment terms and cancellation policy. A new
// contract is a draft; only drafts are edited. Publishing is a separate step by a second person (maker-checker).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { suppliersApi, type ContractBody, type SupplierContract } from "@/lib/api/suppliers";
import { Input, Textarea, FormDrawer, useToast, DateField } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";
import { supplierError } from "./shared";

export function ContractDrawer({
  supplierRef,
  contract,
  onClose,
}: {
  supplierRef: string;
  /** A draft to edit; omitted for a new contract. */
  contract?: SupplierContract;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({
    title: contract?.title ?? "",
    reference: contract?.reference ?? "",
    valid_from: contract?.valid_from ?? "",
    valid_to: contract?.valid_to ?? "",
    currency: contract?.currency ?? "",
    payment_terms: contract?.payment_terms ?? "",
    cancellation_policy: contract?.cancellation_policy ?? "",
    notes: contract?.notes ?? "",
  });
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const dirty = contract
    ? Object.entries(form).some(([k, v]) => v !== ((contract as unknown as Record<string, string | null>)[k] ?? ""))
    : Object.values(form).some((v) => v.trim() !== "");

  const save = useMutation({
    mutationFn: (body: ContractBody) =>
      contract ? suppliersApi.updateContract(contract.id, body) : suppliersApi.createContract(supplierRef, body),
    onSuccess: (k) => {
      qc.invalidateQueries({ queryKey: ["supplier"] });
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      toast.success(contract ? `Draft ${k.title} saved.` : `Contract ${k.title} saved as a draft. A second person publishes it.`);
      onClose();
    },
  });

  function submit() {
    const currency = form.currency.trim().toUpperCase();
    if (!form.title.trim()) return setLocalError("Give the contract a title, for example “2027 rates”.");
    if (!form.valid_from) return setLocalError("Pick the first day the contract is valid.");
    if (form.valid_to && form.valid_to < form.valid_from) return setLocalError("“Valid to” is on or after “valid from”.");
    if (!/^[A-Z]{3}$/.test(currency)) return setLocalError("Currency is a 3-letter ISO code, e.g. VND, THB, USD.");
    setLocalError(null);
    const clean = (v: string) => v.trim() || null;
    save.mutate({
      title: form.title.trim(),
      reference: clean(form.reference),
      valid_from: form.valid_from,
      valid_to: form.valid_to || null,
      currency,
      payment_terms: clean(form.payment_terms),
      cancellation_policy: clean(form.cancellation_policy),
      notes: clean(form.notes),
    });
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={contract ? "Edit draft contract" : "New contract"}
      submitLabel={contract ? "Save draft" : "Save as draft"}
      onSubmit={submit}
      busy={save.isPending}
      dirty={dirty}
      error={localError ?? (fe.length ? null : supplierError(save.error))}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">
        The supplier&apos;s terms for a period. It stays a draft until a second person publishes it; once published it is
        fixed, so new terms are a new contract.
      </p>
      <FormField label="Title" required error={errorFor(fe, "title")}>
        <Input className={inputCls} maxLength={200} value={form.title} onChange={(e) => set("title")(e.target.value)} placeholder="e.g. 2027 rates" />
      </FormField>
      <FormField label="Supplier reference" hint="Their contract number or document name (no upload yet)." error={errorFor(fe, "reference")}>
        <Input className={inputCls} maxLength={120} value={form.reference} onChange={(e) => set("reference")(e.target.value)} />
      </FormField>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <FormField label="Valid from" required error={errorFor(fe, "valid_from")}>
          <DateField className={inputCls} value={form.valid_from} onChange={(v) => set("valid_from")(v)} />
        </FormField>
        <FormField label="Valid to" hint="Empty = open-ended." error={errorFor(fe, "valid_to")}>
          <DateField className={inputCls} value={form.valid_to} onChange={(v) => set("valid_to")(v)} />
        </FormField>
        <FormField label="Currency" required hint="The supplier's currency." error={errorFor(fe, "currency")}>
          <Input
            className={`${inputCls} font-mono uppercase`}
            maxLength={3}
            value={form.currency}
            onChange={(e) => set("currency")(e.target.value.toUpperCase())}
            placeholder="VND"
          />
        </FormField>
      </div>
      <FormField label="Payment terms" hint="Deposit, balance due, method." error={errorFor(fe, "payment_terms")}>
        <Textarea className={textareaCls} maxLength={2000} value={form.payment_terms} onChange={(e) => set("payment_terms")(e.target.value)} placeholder="e.g. 30% deposit on booking, balance 30 days before arrival" />
      </FormField>
      <FormField label="Cancellation policy" error={errorFor(fe, "cancellation_policy")}>
        <Textarea className={textareaCls} maxLength={5000} value={form.cancellation_policy} onChange={(e) => set("cancellation_policy")(e.target.value)} placeholder="e.g. free until 30 days, 50% until 7 days, then 100%" />
      </FormField>
      <FormField label="Internal notes" hint="Staff only." error={errorFor(fe, "notes")}>
        <Textarea className={textareaCls} maxLength={5000} value={form.notes} onChange={(e) => set("notes")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
