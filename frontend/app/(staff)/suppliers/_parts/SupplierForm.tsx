"use client";
// app/(staff)/suppliers/_parts/SupplierForm.tsx — Add / Edit supplier (W2, AA-822; module spec 08 screens 2–3, with
// its conflicts applied): sections Basic information (name, categories — one or more of the six ADR 0005 types, Q20
// — website, internal notes), Contact, Location (street, CIS place typeahead, country, state / province, postal
// code). Only name and one category are required (08: postal code / state are unrealistic for BT / MN). Description
// is labelled "Internal notes" (08 conflict 2: never customer-facing). The API normalises and checks every field;
// its 409 / 422 show in the error band. Needs supply.write.

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { SUPPLIER_CATEGORIES, suppliersApi, type Supplier, type SupplierBody, type SupplierCategory, type SupplierDetail } from "@/lib/api/suppliers";
import { Button, Section, fieldName, useCan, useToast } from "@/ui";
import { Field, textareaCls } from "@/app/(staff)/cases/_parts/fields";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { QueryError } from "@/app/(staff)/cases/_parts/QueryError";
import { inputCls } from "@/app/(staff)/_parts/FormField";
import { DestinationPicker, type PickedPlace } from "./DestinationPicker";
import { CATEGORY_LABEL, supplierError, supplierHref } from "./shared";

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const WEBSITE = /^https?:\/\/\S+$/i;

type Text = Record<
  | "company_name"
  | "website"
  | "internal_notes"
  | "contact_name"
  | "contact_title"
  | "contact_email"
  | "contact_phone"
  | "address"
  | "region"
  | "postal_code"
  | "country_code",
  string
>;

function initialText(s?: SupplierDetail): Text {
  return {
    company_name: s?.company_name ?? "",
    website: s?.website ?? "",
    internal_notes: s?.internal_notes ?? "",
    contact_name: s?.contact_name ?? "",
    contact_title: s?.contact_title ?? "",
    contact_email: s?.contact_email ?? "",
    contact_phone: s?.contact_phone ?? "",
    address: s?.address ?? "",
    region: s?.region ?? "",
    postal_code: s?.postal_code ?? "",
    country_code: s?.country_code ?? "",
  };
}

function errorsOf(t: Text, categories: SupplierCategory[]): Partial<Record<keyof Text | "categories", string>> {
  const e: Partial<Record<keyof Text | "categories", string>> = {};
  if (!t.company_name.trim()) e.company_name = "Enter the company name.";
  if (categories.length === 0) e.categories = "Pick at least one category.";
  if (t.website.trim() && !WEBSITE.test(t.website.trim())) e.website = "Start with https:// (or http://).";
  if (t.contact_email.trim() && !EMAIL.test(t.contact_email.trim())) e.contact_email = "Enter a valid e-mail address.";
  return e;
}

export function SupplierForm({ supplier }: { supplier?: SupplierDetail }) {
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const canWrite = useCan("supply.write");
  const countries = useCountries();
  const editing = !!supplier;

  const [text, setText] = useState<Text>(() => initialText(supplier));
  const [categories, setCategories] = useState<SupplierCategory[]>(supplier?.categories ?? []);
  const [place, setPlace] = useState<PickedPlace | null>(() =>
    supplier?.destination ? { ...supplier.destination } : null,
  );
  // The place id is kept even when the catalog could not resolve its name on load (destination null, id set).
  const [placeId, setPlaceId] = useState<string | null>(supplier?.destination_id ?? null);
  const [tried, setTried] = useState(false);
  const set = (k: keyof Text) => (v: string) => setText((t) => ({ ...t, [k]: v }));
  const errors = errorsOf(text, categories);
  const invalid = Object.keys(errors).length > 0;

  const save = useMutation({
    mutationFn: (): Promise<Supplier> => {
      const clean = (v: string) => v.trim() || null;
      const body: SupplierBody = {
        company_name: text.company_name.trim(),
        categories,
        website: clean(text.website),
        internal_notes: clean(text.internal_notes),
        contact_name: clean(text.contact_name),
        contact_title: clean(text.contact_title),
        contact_email: clean(text.contact_email),
        contact_phone: clean(text.contact_phone),
        address: clean(text.address),
        region: clean(text.region),
        postal_code: clean(text.postal_code),
        destination_id: placeId,
        country_code: clean(text.country_code),
      };
      return editing ? suppliersApi.update(supplier.code, body) : suppliersApi.create(body);
    },
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      qc.invalidateQueries({ queryKey: ["supplier"] });
      toast.success(editing ? `Changes to ${s.company_name} saved.` : `Supplier ${s.company_name} added (${s.code}).`);
      router.push(supplierHref(s));
    },
  });

  function submit() {
    setTried(true);
    if (invalid || save.isPending) return;
    save.mutate();
  }

  const back = editing ? supplierHref(supplier) : "/suppliers";
  const title = editing ? `Edit ${supplier.company_name}` : "Add new supplier";
  const submitLabel = save.isPending ? "Saving…" : editing ? "Save changes" : "Save supplier";
  const apiError = save.error instanceof ApiError ? save.error : null;
  const show = (k: keyof typeof errors) => (tried ? (errors[k] ?? null) : null);

  if (!canWrite) {
    return (
      <main className="aa-admin-main flex-1 overflow-y-auto px-4 py-6 sm:px-8">
        <QueryError error={new ApiError("forbidden", 403)} what="editing suppliers" />
      </main>
    );
  }

  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <div className="border-b border-line bg-bg px-4 py-4 sm:px-8">
        <div className="mx-auto flex max-w-[880px] flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <Link href={back} aria-label="Back" className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-line bg-card text-muted hover:text-ink">
              <ArrowLeft size={16} />
            </Link>
            <h1 className="truncate font-display text-xl font-semibold text-ink">{title}</h1>
          </div>
          <div className="flex items-center gap-2">
            <Link href={back} className="px-3 text-[13px] font-medium text-muted hover:text-ink">
              Cancel
            </Link>
            <Button variant="primary" onClick={submit} disabled={save.isPending}>
              {submitLabel}
            </Button>
          </div>
        </div>
      </div>

      <form
        className="mx-auto flex max-w-[880px] flex-col gap-5 px-4 py-6 sm:px-8"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        noValidate
      >
        <Section title="Basic information">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Supplier name" required error={show("company_name")} className="sm:col-span-2">
              <input className={inputCls} maxLength={200} value={text.company_name} onChange={(e) => set("company_name")(e.target.value)} placeholder="e.g. Hanoi Emerald Hotel" />
            </Field>
            <fieldset className="flex min-w-0 flex-col gap-1.5 sm:col-span-2">
              <legend className="mb-1.5 text-[13px] font-medium text-ink">
                Categories<span className="text-danger"> *</span>
              </legend>
              <div className="flex flex-wrap gap-2">
                {SUPPLIER_CATEGORIES.map((c) => {
                  const on = categories.includes(c);
                  return (
                    <button
                      key={c}
                      type="button"
                      role="checkbox"
                      aria-checked={on}
                      onClick={() => setCategories((cur) => (on ? cur.filter((x) => x !== c) : SUPPLIER_CATEGORIES.filter((x) => x === c || cur.includes(x))))}
                      className={`cursor-pointer rounded-full border px-3 py-1.5 text-[13px] font-medium ${
                        on ? "border-accent-border bg-accent-tint text-accent-deep" : "border-line bg-card text-body hover:border-accent-border"
                      }`}
                    >
                      {CATEGORY_LABEL[c]}
                    </button>
                  );
                })}
              </div>
              {show("categories") ? (
                <span className="text-xs text-danger">{show("categories")}</span>
              ) : (
                <span className="text-xs text-muted">One or more: what Booking buys from this supplier (rate types).</span>
              )}
            </fieldset>
            <Field label="Website" hint="Optional." error={show("website")} className="sm:col-span-2">
              <input className={inputCls} type="url" maxLength={500} value={text.website} onChange={(e) => set("website")(e.target.value)} placeholder="https://" />
            </Field>
            <Field label="Internal notes" hint="Staff only. Never shown to customers." className="sm:col-span-2">
              <textarea
                className={textareaCls}
                maxLength={5000}
                value={text.internal_notes}
                onChange={(e) => set("internal_notes")(e.target.value)}
                placeholder="What they are good at, who to call for what, anything the next person should know."
              />
            </Field>
          </div>
        </Section>

        <Section title="Contact details">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Primary contact person">
              <input className={inputCls} maxLength={200} value={text.contact_name} onChange={(e) => set("contact_name")(e.target.value)} placeholder="Full name" />
            </Field>
            <Field label="Job title">
              <input className={inputCls} maxLength={120} value={text.contact_title} onChange={(e) => set("contact_title")(e.target.value)} placeholder="e.g. Sales Manager" />
            </Field>
            <Field label="E-mail address" error={show("contact_email")}>
              <input className={inputCls} type="email" maxLength={254} value={text.contact_email} onChange={(e) => set("contact_email")(e.target.value)} placeholder="contact@supplier.com" />
            </Field>
            <Field label="Phone number">
              <input className={inputCls} type="tel" maxLength={40} value={text.contact_phone} onChange={(e) => set("contact_phone")(e.target.value)} placeholder="e.g. +84 24 1234 5678" />
            </Field>
          </div>
        </Section>

        <Section title="Location" description="The city is a CIS place, so it matches tours and hotels; the address lines are free text.">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Street address" className="sm:col-span-2">
              <input className={inputCls} maxLength={500} value={text.address} onChange={(e) => set("address")(e.target.value)} placeholder="Full street address" />
            </Field>
            <div className="flex min-w-0 flex-col gap-1.5">
              <span className="text-[13px] font-medium text-ink">City / place</span>
              <DestinationPicker
                value={place ?? (placeId ? { id: placeId, name: "Place not readable now", country: "CIS" } : null)}
                country={text.country_code}
                onChange={(p) => {
                  setPlace(p ? { id: p.destination_id, name: p.name, country: p.country } : null);
                  setPlaceId(p ? p.destination_id : null);
                  if (p?.country_code) set("country_code")(p.country_code);
                }}
              />
              <span className="text-xs text-muted">Picking a place fills the country.</span>
            </div>
            <Field label="Country">
              <select className={inputCls} value={text.country_code} onChange={(e) => set("country_code")(e.target.value)}>
                <option value="">Not set</option>
                {(countries.data?.items ?? []).map((c) => (
                  <option key={c.iso2} value={c.iso2}>
                    {c.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="State / province" hint="Optional.">
              <input className={inputCls} maxLength={120} value={text.region} onChange={(e) => set("region")(e.target.value)} />
            </Field>
            <Field label="Postal code" hint="Optional.">
              <input className={inputCls} maxLength={20} value={text.postal_code} onChange={(e) => set("postal_code")(e.target.value)} />
            </Field>
          </div>
        </Section>

        {(save.isError || (tried && invalid)) && (
          <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-4 py-3 text-[13px] text-danger">
            {save.isError ? (
              <>
                <p>{supplierError(save.error)}</p>
                {apiError?.fieldErrors.map((f, i) => (
                  <p key={i}>
                    {fieldName(f) || "form"}: {f.msg}
                  </p>
                ))}
              </>
            ) : (
              <p>Some fields need attention before the supplier can be saved.</p>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 sm:hidden">
          <Button variant="primary" type="submit" disabled={save.isPending}>
            {submitLabel}
          </Button>
        </div>
      </form>
    </main>
  );
}
