"use client";
// app/(staff)/cases/[code]/EditDetailsDrawer.tsx
// AA-683 (fe-cases) — "Edit details" drawer (spec §4.7; Figma ADD NEW ACTIVITY-2 "Edit Trip Case", customer
// locked). Edits only the commercial case fields the API accepts (CasePatch): start date, adults / children,
// currency, countries and internal notes. Only changed fields are sent. Content (tour text, destinations)
// is CIS-owned and never edited here (O7); the sales rep has its own Assign action.

import { useState } from "react";
import { ApiError } from "@/lib/api/client";
import type { TripCaseDetail } from "@/lib/api/types";
import { FormDrawer } from "@/ui";
import { CURRENCIES, Field, LockedValue, inputCls, paxErrors, textareaCls, toInt } from "../_parts/fields";
import { CountryPicker } from "../_parts/CountryPicker";
import { friendlyError } from "../_parts/QueryError";
import { useUpdateCase } from "./useCaseMutations";
import { DateField } from "@/ui/booking/DateField";

interface Form {
  start_date: string;
  pax_adults: string;
  pax_children: string;
  currency: string;
  countries: string[];
  notes: string;
}

function fromCase(c: TripCaseDetail): Form {
  return {
    start_date: c.start_date ?? "",
    pax_adults: c.pax_adults != null ? String(c.pax_adults) : c.pax != null ? String(c.pax) : "",
    pax_children: c.pax_children != null ? String(c.pax_children) : "0",
    currency: c.currency ?? "USD",
    countries: [...c.countries],
    notes: c.notes ?? "",
  };
}

/** The PATCH body: only what differs from the case. */
export function diffCase(c: TripCaseDetail, f: Form): Record<string, unknown> {
  const base = fromCase(c);
  const body: Record<string, unknown> = {};
  if (f.start_date !== base.start_date) body.start_date = f.start_date || null;
  if (f.pax_adults !== base.pax_adults || f.pax_children !== base.pax_children) {
    body.pax_adults = toInt(f.pax_adults);
    body.pax_children = toInt(f.pax_children) ?? 0;
  }
  if (f.currency !== base.currency) body.currency = f.currency;
  if (f.countries.join(",") !== base.countries.join(",")) body.countries = f.countries;
  if (f.notes.trim() !== base.notes.trim()) body.notes = f.notes.trim() || null;
  return body;
}

export function EditDetailsDrawer({ c, open, onClose }: { c: TripCaseDetail; open: boolean; onClose: () => void }) {
  // Remount per open so the form starts from the latest case.
  return open ? <EditBody c={c} onClose={onClose} /> : null;
}

function EditBody({ c, onClose }: { c: TripCaseDetail; onClose: () => void }) {
  const [f, setF] = useState<Form>(() => fromCase(c));
  const [tried, setTried] = useState(false);
  const update = useUpdateCase(c.code);
  const body = diffCase(c, f);
  const dirty = Object.keys(body).length > 0;
  const pax = paxErrors(f.pax_adults, f.pax_children);
  const invalid = !!pax.adults || !!pax.children;
  const currencies = CURRENCIES.includes(f.currency as (typeof CURRENCIES)[number]) ? CURRENCIES : [f.currency, ...CURRENCIES];

  function submit() {
    setTried(true);
    if (invalid) return;
    if (!dirty) return onClose();
    update.mutate(body, { onSuccess: onClose });
  }

  return (
    <FormDrawer
      open
      onClose={onClose}
      title={`Edit trip case ${c.code}`}
      onSubmit={submit}
      submitLabel="Save changes"
      busy={update.isPending}
      dirty={dirty}
      error={update.isError ? friendlyError(update.error) : tried && invalid ? "Some fields need attention." : null}
      fieldErrors={update.error instanceof ApiError ? update.error.fieldErrors : []}
      width={520}
    >
      <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">Customer</p>
      <LockedValue value={c.customer_name ?? c.customer_email ?? "Customer"} hint="A case keeps its customer. Open a new case for someone else." />

      <p className="mt-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">Trip details</p>
      <Field label="Start date" hint="Clear it if the dates are not fixed yet.">
        <DateField className={inputCls} value={f.start_date} onChange={(v) => setF({ ...f, start_date: v })} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Adults" required error={tried ? pax.adults : null}>
          <input className={inputCls} type="number" inputMode="numeric" min={1} max={999} value={f.pax_adults} onChange={(e) => setF({ ...f, pax_adults: e.target.value })} />
        </Field>
        <Field label="Children" error={tried ? pax.children : null}>
          <input className={inputCls} type="number" inputMode="numeric" min={0} max={998} value={f.pax_children} onChange={(e) => setF({ ...f, pax_children: e.target.value })} />
        </Field>
      </div>
      <Field label="Currency" hint="Proposals already sent keep the currency they were sent in.">
        <select className={inputCls} value={f.currency} onChange={(e) => setF({ ...f, currency: e.target.value })}>
          {currencies.map((cur) => (
            <option key={cur}>{cur}</option>
          ))}
        </select>
      </Field>
      <Field label="Countries">
        <CountryPicker value={f.countries} onChange={(countries) => setF({ ...f, countries })} />
      </Field>

      <p className="mt-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">Notes</p>
      <Field label="Internal notes" hint="Staff only. The customer never sees these.">
        <textarea className={textareaCls} value={f.notes} maxLength={10000} onChange={(e) => setF({ ...f, notes: e.target.value })} />
      </Field>
    </FormDrawer>
  );
}
