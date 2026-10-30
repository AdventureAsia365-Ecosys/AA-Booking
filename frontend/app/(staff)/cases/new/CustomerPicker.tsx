"use client";
// app/(staff)/cases/new/CustomerPicker.tsx
// AA-683 (fe-cases) — spec §5 CustomerCombobox with the inline "+ New lead" (spec §4.6, 03 decision 4):
// a debounced search on GET /booking/customers (name, e-mail, phone or CUS- code), or a new manual lead
// (name + e-mail or phone, consent checkbox) that the API creates unverified with the case.

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { api } from "@/lib/api/client";
import type { CustomerSummary } from "@/lib/api/types";
import { Checkbox, Input, CustomerStatusBadge, PersonCell } from "@/ui";
import { Field, inputCls } from "../_parts/fields";

export interface LeadInput {
  name: string;
  email: string;
  phone: string;
  contact_consent: boolean;
}

export const EMPTY_LEAD: LeadInput = { name: "", email: "", phone: "", contact_consent: false };

/** Same shape check as the API's Email field (no deliverability check). */
export const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function leadErrors(lead: LeadInput): Partial<Record<keyof LeadInput | "contact", string>> {
  const out: Partial<Record<keyof LeadInput | "contact", string>> = {};
  if (!lead.name.trim()) out.name = "Enter the lead's name.";
  if (!lead.email.trim() && !lead.phone.trim()) out.contact = "Enter an e-mail or a phone number.";
  if (lead.email.trim() && !EMAIL_RE.test(lead.email.trim())) out.email = "This e-mail does not look right.";
  if (lead.phone.trim() && lead.phone.trim().length < 3) out.phone = "A phone number needs at least 3 characters.";
  return out;
}

export function CustomerPicker({
  mode,
  onMode,
  customer,
  onCustomer,
  lead,
  onLead,
  showErrors,
}: {
  mode: "existing" | "new";
  onMode: (m: "existing" | "new") => void;
  customer: CustomerSummary | null;
  onCustomer: (c: CustomerSummary | null) => void;
  lead: LeadInput;
  onLead: (l: LeadInput) => void;
  showErrors: boolean;
}) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(search.trim()), 300);
    return () => window.clearTimeout(id);
  }, [search]);

  const results = useQuery({
    queryKey: ["customers", "picker", debounced],
    queryFn: () => api.customers.list({ q: debounced, limit: 8 }),
    enabled: mode === "existing" && debounced.length >= 2,
  });

  const errs = showErrors ? leadErrors(lead) : {};

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Customer">
        {(["existing", "new"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={mode === m}
            onClick={() => onMode(m)}
            className={`cursor-pointer rounded-full border px-4 py-1.5 text-[13px] font-medium ${
              mode === m ? "border-accent bg-accent-tint text-accent-deep" : "border-line text-muted hover:text-ink"
            }`}
          >
            {m === "existing" ? "Existing customer" : "+ New lead"}
          </button>
        ))}
      </div>

      {mode === "existing" ? (
        customer ? (
          <div className="flex items-center justify-between gap-3 rounded-md border border-accent-border bg-accent-tint px-3 py-2.5">
            <PersonCell name={customer.name} sub={[customer.code, customer.email ?? customer.phone].filter(Boolean).join(" · ")} chips={<CustomerStatusBadge status={customer.status} />} />
            <button type="button" onClick={() => onCustomer(null)} className="shrink-0 cursor-pointer text-[13px] font-semibold text-accent-deep hover:underline">
              Change
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <label className="relative">
              <span className="sr-only">Search customers</span>
              <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted2" />
              <Input
                className={inputCls} style={{ paddingLeft: 36 }}
                placeholder="Search by name, e-mail, phone or CUS- code"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                autoComplete="off"
              />
            </label>
            {showErrors && <p className="text-xs text-danger">Choose a customer, or add a new lead.</p>}
            {debounced.length >= 2 ? (
              <div className="overflow-hidden rounded-md border border-line" aria-live="polite">
                {results.isLoading ? (
                  <p className="px-3 py-3 text-[13px] text-muted">Searching…</p>
                ) : results.isError ? (
                  <p className="px-3 py-3 text-[13px] text-danger">{(results.error as Error).message}</p>
                ) : results.data?.items.length ? (
                  results.data.items.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => onCustomer(c)}
                      disabled={c.is_blocked}
                      title={c.is_blocked ? "This customer is blocked: a case cannot be opened for them." : undefined}
                      className="flex w-full cursor-pointer items-center justify-between gap-3 border-b border-line2 px-3 py-2 text-left last:border-b-0 hover:bg-bg disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <PersonCell name={c.name} sub={[c.code, c.email ?? c.phone].filter(Boolean).join(" · ")} />
                      <CustomerStatusBadge status={c.status} />
                    </button>
                  ))
                ) : (
                  <p className="px-3 py-3 text-[13px] text-muted">
                    No customer matches.{" "}
                    <button type="button" onClick={() => onMode("new")} className="cursor-pointer font-semibold text-accent-deep hover:underline">
                      Add a new lead
                    </button>
                  </p>
                )}
              </div>
            ) : (
              <p className="text-xs text-muted">Type at least 2 characters.</p>
            )}
          </div>
        )
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" required error={errs.name}>
            <Input className={inputCls} value={lead.name} maxLength={200} onChange={(e) => onLead({ ...lead, name: e.target.value })} autoComplete="off" />
          </Field>
          <Field label="E-mail" error={errs.email ?? errs.contact} hint="E-mail or phone is required.">
            <Input className={inputCls} type="email" value={lead.email} maxLength={254} onChange={(e) => onLead({ ...lead, email: e.target.value })} autoComplete="off" />
          </Field>
          <Field label="Phone" error={errs.phone}>
            <Input className={inputCls} type="tel" value={lead.phone} maxLength={40} onChange={(e) => onLead({ ...lead, phone: e.target.value })} autoComplete="off" />
          </Field>
          <label className="flex items-center gap-2 text-[13px] text-body sm:self-end sm:pb-2.5">
            <Checkbox
              checked={lead.contact_consent}
              onChange={(e) => onLead({ ...lead, contact_consent: e.target.checked })}
            />
            The customer agreed to be contacted
          </label>
          <p className="text-xs text-muted sm:col-span-2">
            The lead is saved unverified. If the e-mail or phone matches an existing customer, the lead is flagged for a merge review; nothing is merged automatically.
          </p>
        </div>
      )}
    </div>
  );
}
