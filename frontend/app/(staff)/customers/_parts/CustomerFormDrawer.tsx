"use client";
// app/(staff)/customers/_parts/CustomerFormDrawer.tsx
// AA-683 (fe-people) — "+ New lead" and "Edit profile" for a customer (UI spec §4.12; the Figma does not
// draw either form, module spec 02 "Not drawn").
//   New lead: POST /booking/customers {name, email?, phone?, address?, contact_consent}. The lead is
//     unverified, source "manual", and never merged (O6): a matching e-mail / phone only raises the
//     merge-review flag, which the answer reports.
//   Edit: PATCH /booking/customers/{ref} with only the changed fields. E-mail is locked once the customer
//     is verified (Q14): it then changes only through the customer's own sign-in.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type FieldError } from "@/lib/api/client";
import type { CustomerDetail, CustomerPatchBody, CustomerSummary } from "@/lib/api/types";
import { Checkbox, Input, FormDrawer, fieldName, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";

const FIELDS = ["name", "email", "phone", "address", "contact_consent"];
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

type Mode = { kind: "create" } | { kind: "edit"; customer: CustomerSummary };

interface Values {
  name: string;
  email: string;
  phone: string;
  address: string;
  contact_consent: boolean;
}

function initial(mode: Mode): Values {
  if (mode.kind === "create") return { name: "", email: "", phone: "", address: "", contact_consent: false };
  const c = mode.customer;
  return { name: c.name ?? "", email: c.email ?? "", phone: c.phone ?? "", address: c.address ?? "", contact_consent: c.contact_consent };
}

/** Client-side checks that mirror the API (NewCustomer / CustomerPatch); the API stays the authority. */
export function validateCustomer(v: Values): Partial<Record<keyof Values, string>> {
  const out: Partial<Record<keyof Values, string>> = {};
  if (!v.name.trim()) out.name = "Enter a name.";
  const email = v.email.trim();
  const phone = v.phone.trim();
  if (email && !EMAIL_RE.test(email)) out.email = "Enter a valid e-mail address.";
  if (phone && phone.length < 3) out.phone = "Enter at least 3 characters.";
  if (!email && !phone) out.phone = "Enter an e-mail or a phone number.";
  return out;
}

export function CustomerFormDrawer({
  open,
  onClose,
  mode,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  mode: Mode;
  onCreated?: (c: CustomerDetail) => void;
}) {
  // Remount the form per open so it starts from the current values.
  if (!open) return null;
  return <CustomerForm onClose={onClose} mode={mode} onCreated={onCreated} />;
}

function CustomerForm({ onClose, mode, onCreated }: { onClose: () => void; mode: Mode; onCreated?: (c: CustomerDetail) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [start] = useState(() => initial(mode));
  const [v, setV] = useState<Values>(start);
  const [touched, setTouched] = useState(false);
  const emailLocked = mode.kind === "edit" && mode.customer.verified;
  const local = touched ? validateCustomer(v) : {};
  const dirty = (Object.keys(start) as (keyof Values)[]).some((k) => start[k] !== v[k]);

  const save = useMutation({
    mutationFn: async () => {
      const clean = (s: string) => s.trim() || null;
      if (mode.kind === "create") {
        return api.customers.create({
          name: v.name.trim(),
          email: clean(v.email),
          phone: clean(v.phone),
          address: clean(v.address),
          contact_consent: v.contact_consent,
        });
      }
      const body: CustomerPatchBody = {};
      if (v.name.trim() !== start.name.trim()) body.name = v.name.trim();
      if (!emailLocked && v.email.trim() !== start.email.trim()) body.email = clean(v.email);
      if (v.phone.trim() !== start.phone.trim()) body.phone = clean(v.phone);
      if (v.address.trim() !== start.address.trim()) body.address = clean(v.address);
      if (Object.keys(body).length === 0) return null;
      return api.customers.update(mode.customer.code ?? mode.customer.id, body);
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["customers"] });
      qc.invalidateQueries({ queryKey: ["customer"] });
      if (mode.kind === "create" && res && "cases" in res) {
        toast.success(
          res.needs_merge_review
            ? `Lead ${res.code ?? ""} added. Its e-mail or phone matches another customer: check before merging.`
            : `Lead ${res.code ?? ""} added.`,
          6000,
        );
        onCreated?.(res);
      } else {
        toast.success(res ? "Customer updated." : "Nothing changed.");
      }
      onClose();
    },
  });

  const err = save.error instanceof ApiError ? save.error : null;
  const fieldErrors: FieldError[] = err?.fieldErrors ?? [];
  const unmapped = fieldErrors.filter((f) => !FIELDS.includes(fieldName(f)));
  const formError = err
    ? err.code === "email_locked"
      ? "This customer is verified: their e-mail changes only through their own sign-in."
      : err.code === "contact_taken"
        ? "Another verified customer already uses this e-mail or phone."
        : err.fieldErrors.length && !unmapped.length
          ? null
          : err.message
    : save.error
      ? (save.error as Error).message
      : null;

  function submit() {
    setTouched(true);
    if (Object.keys(validateCustomer(v)).length) return;
    save.mutate();
  }

  const set = <K extends keyof Values>(k: K, value: Values[K]) => setV((p) => ({ ...p, [k]: value }));
  const show = (k: keyof Values) => local[k] ?? errorFor(fieldErrors, k);

  return (
    <FormDrawer
      open
      onClose={onClose}
      title={mode.kind === "create" ? "New lead" : "Edit customer"}
      submitLabel={mode.kind === "create" ? "Add lead" : "Save changes"}
      onSubmit={submit}
      busy={save.isPending}
      dirty={dirty}
      error={formError}
      fieldErrors={unmapped}
    >
      {mode.kind === "create" && (
        <p className="rounded-md border border-accent-border bg-accent-tint px-3 py-2 text-[13px] text-body">
          A lead added here is unverified. If the e-mail or phone matches an existing customer, it is flagged for a merge
          review instead of being merged.
        </p>
      )}
      <FormField label="Full name" required error={show("name")}>
        <Input className={inputCls} value={v.name} maxLength={200} onChange={(e) => set("name", e.target.value)} autoComplete="off" autoFocus />
      </FormField>
      <FormField
        label="E-mail"
        locked={emailLocked}
        error={emailLocked ? null : show("email")}
        hint={emailLocked ? "Verified customers change their e-mail through their own sign-in." : "E-mail or phone is required."}
      >
        <Input className={inputCls} type="email" value={v.email} maxLength={254} disabled={emailLocked} onChange={(e) => set("email", e.target.value)} autoComplete="off" />
      </FormField>
      <FormField label="Phone" error={show("phone")} hint="With the country code, e.g. +44 7700 900077.">
        <Input className={inputCls} type="tel" value={v.phone} maxLength={40} onChange={(e) => set("phone", e.target.value)} autoComplete="off" />
      </FormField>
      <FormField label="Location" error={show("address")} hint="City and country, or a postal address.">
        <Input className={inputCls} value={v.address} maxLength={500} onChange={(e) => set("address", e.target.value)} autoComplete="off" />
      </FormField>
      {mode.kind === "create" && (
        <label className="flex items-start gap-2.5 text-[13px] text-body">
          <Checkbox checked={v.contact_consent} onChange={(e) => set("contact_consent", e.target.checked)} style={{ marginTop: 2 }} />
          <span>The customer agreed to be contacted about this trip.</span>
        </label>
      )}
    </FormDrawer>
  );
}
