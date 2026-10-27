"use client";
// app/(staff)/catalog/trip-plans/_parts/CommercialDrawer.tsx — commercial edit of one trip plan (AA-679, bk-009;
// UI spec §3 /commercial, Figma CREATE / EDIT TEMPLATE, commercial parts only). PUT
// /booking/catalog/trip-plans/{source_id}/commercial with EVERY field (a cleared field is saved as empty). The
// price hint is a guide for sales, never a quote: proposals are priced from their cost lines (ADR 0005). Supplier
// and the season × pax band price matrix come in a later slice.
// AA-996: the Figma CREATE / EDIT TEMPLATE form shows the tour content too; here it is the read-only "From CIS" block
// (title, SKU, country, duration, days, summary) with "Edit in CIS ↗" for the roles that edit content in CIS.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { CHANNELS, SELLING_CURRENCIES, type Channel } from "@/lib/api/proposals";
import { tripPlansApi, type TripPlanDetail } from "@/lib/api/tripPlans";
import { FormDrawer, useStaff, useToast } from "@/ui";
import { CisField, CisFieldset } from "../../../_parts/CisFields";
import { CIS_EDITOR_ROLES } from "./shared";
import { FormField, errorFor, inputCls } from "../../../_parts/FormField";

const textareaCls =
  "min-h-[96px] w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border";

interface FormState {
  sellable: boolean;
  trip_style: string;
  default_channel: Channel | "";
  hint_amount: string;
  hint_currency: string;
  hint_basis: "per_person" | "per_group";
  notes: string;
}

function initial(plan: TripPlanDetail): FormState {
  return {
    sellable: plan.sellable,
    trip_style: plan.trip_style ?? "",
    default_channel: plan.default_channel ?? "",
    hint_amount: plan.price_hint?.amount ?? "",
    hint_currency: plan.price_hint?.currency ?? "USD",
    hint_basis: plan.price_hint?.basis ?? "per_person",
    notes: plan.commercial_notes ?? "",
  };
}

const AMOUNT_RE = /^\d{1,10}(\.\d{1,2})?$/;

export function CommercialDrawer({ plan, onClose }: { plan: TripPlanDetail; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const me = useStaff();
  const [start] = useState(() => initial(plan));
  const [form, setForm] = useState<FormState>(start);
  const [localError, setLocalError] = useState<string | null>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(start);

  const save = useMutation({
    mutationFn: () => {
      const amount = form.hint_amount.trim();
      return tripPlansApi.saveCommercial(plan.source_id, {
        sellable: form.sellable,
        trip_style: form.trip_style.trim() || null,
        default_channel: form.default_channel || null,
        price_hint: amount ? { amount, currency: form.hint_currency, basis: form.hint_basis } : null,
        commercial_notes: form.notes.trim() || null,
      });
    },
    onSuccess: (out) => {
      qc.setQueryData(["trip-plans", "detail", plan.source_id], out);
      qc.invalidateQueries({ queryKey: ["trip-plans"] });
      toast.success(out.changed?.length ? "Commercial details saved" : "Nothing changed");
      onClose();
    },
  });

  function submit() {
    const amount = form.hint_amount.trim();
    if (amount && (!AMOUNT_RE.test(amount) || Number(amount) <= 0)) {
      setLocalError("Enter the price hint as an amount above 0 with up to 2 decimals, for example 2450 or 2450.50.");
      return;
    }
    setLocalError(null);
    save.mutate();
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  const apiMessage = save.error instanceof ApiError && !fe.length ? save.error.message : null;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Commercial details"
      onSubmit={submit}
      busy={save.isPending}
      dirty={dirty}
      error={localError ?? apiMessage}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">
        Booking-owned fields of <span className="font-medium text-ink">{plan.title}</span>. The tour itself (title, days,
        photos) is edited in CIS.
      </p>
      <CisFieldset
        title="Tour content"
        editPath={CIS_EDITOR_ROLES.has(me.role) ? `/admin/master-content?tour=${encodeURIComponent(plan.tour_id)}` : null}
        description={CIS_EDITOR_ROLES.has(me.role) ? undefined : "Ask the content team to change it in CIS."}
      >
        <CisField label="Title" wide>
          {plan.title}
        </CisField>
        <CisField label="SKU">{plan.sku}</CisField>
        <CisField label="Country">{plan.country}</CisField>
        <CisField label="Duration">{plan.duration_raw || (plan.duration_days ? `${plan.duration_days} days` : null)}</CisField>
        <CisField label="Days in CIS">{plan.days.length ? `${plan.days.length} days, ${plan.photos.length} photos` : null}</CisField>
        <CisField label="Summary" multiline wide>
          {plan.summary || plan.description}
        </CisField>
      </CisFieldset>
      <label className="flex items-start gap-3 rounded-md border border-line bg-panel px-3 py-3">
        <input
          type="checkbox"
          className="mt-0.5 h-4 w-4 accent-accent"
          checked={form.sellable}
          onChange={(e) => setForm({ ...form, sellable: e.target.checked })}
        />
        <span>
          <span className="block text-[13px] font-medium text-ink">Sellable</span>
          <span className="block text-xs text-muted">Sales can start trip cases from this plan. Untick to mark it not for sale.</span>
        </span>
      </label>
      <FormField label="Trip style" hint="Free text for now, for example Private or Small group." error={errorFor(fe, "trip_style")}>
        <input className={inputCls} maxLength={60} value={form.trip_style} onChange={(e) => setForm({ ...form, trip_style: e.target.value })} />
      </FormField>
      <FormField label="Default channel" hint="Suggested for proposals started from this plan." error={errorFor(fe, "default_channel")}>
        <select
          className={inputCls}
          value={form.default_channel}
          onChange={(e) => setForm({ ...form, default_channel: e.target.value as Channel | "" })}
        >
          <option value="">No default</option>
          {CHANNELS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </FormField>
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1.5 text-[13px] font-medium text-ink">Price hint</legend>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_96px_132px]">
          <input
            aria-label="Price hint amount"
            inputMode="decimal"
            className={inputCls}
            placeholder="from amount"
            value={form.hint_amount}
            onChange={(e) => setForm({ ...form, hint_amount: e.target.value })}
          />
          <select aria-label="Price hint currency" className={inputCls} value={form.hint_currency} onChange={(e) => setForm({ ...form, hint_currency: e.target.value })}>
            {SELLING_CURRENCIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <select
            aria-label="Price hint basis"
            className={inputCls}
            value={form.hint_basis}
            onChange={(e) => setForm({ ...form, hint_basis: e.target.value as FormState["hint_basis"] })}
          >
            <option value="per_person">per person</option>
            <option value="per_group">per group</option>
          </select>
        </div>
        <span className="text-xs text-muted">
          A guide for sales, never a quote. Leave the amount empty for no hint. Proposals are priced from their cost lines.
        </span>
      </fieldset>
      <FormField label="Commercial notes" hint="Staff only, never shown to customers." error={errorFor(fe, "commercial_notes")}>
        <textarea className={textareaCls} maxLength={5000} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
      </FormField>
    </FormDrawer>
  );
}
