"use client";
// app/(staff)/pricing/agency-tiers/_parts/AgencyDrawers.tsx — "New tier", "New tier discount" and "New rate card"
// drawers (AA-1030, bk-043). Staff type percentages ("3" for 3 %); the API takes fractions ("0.0300"). A discount or a
// rate card is saved PENDING; the toast says a second person has to approve it. A rate card is either a discount or an
// END card (no discount: from its first day it ends the contract rate of its scope, back to the tier); the Rate cards
// tab opens this drawer pre-filled as an end card from a card's "End" button. The API owns every check (discount under
// 50 %, valid_to on or after valid_from, one row per tier / partner scope, kind and date): its 422 / 409 show inline.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { agencyPricingApi, type PartnerTerms, type PartnerTier } from "@/lib/api/agencyPricing";
import { DateField, FormDrawer, Input, Select, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "../../../_parts/FormField";
import { PercentInput, percentToFraction } from "../../rules/_parts/PolicyDrawers";

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

function errorText(err: unknown): string | null {
  if (!err) return null;
  if (err instanceof ApiError) return err.fieldErrors.length ? null : err.message;
  return err instanceof Error ? err.message : "Could not save.";
}

/** A discount: a percentage from 0 to under 50 with at most 2 decimals; null otherwise. */
export function discountFraction(value: string): string | null {
  const fraction = percentToFraction(value);
  return fraction !== null && Number(fraction) < 0.5 ? fraction : null;
}

export function NewTierDrawer({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ code: "", label: "", sort: "40" });
  const [localError, setLocalError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: agencyPricingApi.createTier,
    onSuccess: (tier) => {
      qc.invalidateQueries({ queryKey: ["agency-pricing"] });
      toast.success(`Tier ${tier.label} added. Add its discount; until one is approved it gives 0 %.`);
      onClose();
    },
  });
  function submit() {
    const code = form.code.trim().toLowerCase();
    if (!/^[a-z][a-z0-9_]{1,31}$/.test(code) || !form.label.trim()) {
      setLocalError("Code: 2 to 32 lowercase letters, digits or _ starting with a letter. Label: the name staff see.");
      return;
    }
    setLocalError(null);
    create.mutate({ code, label: form.label.trim(), sort_order: Number(form.sort) || 0 });
  }
  const fe = create.error instanceof ApiError ? create.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="New tier"
      submitLabel="Add tier"
      onSubmit={submit}
      busy={create.isPending}
      dirty={!!(form.code || form.label)}
      error={localError ?? errorText(create.error)}
      fieldErrors={fe}
    >
      <FormField label="Code" required hint="Stable key, never changed (for example platinum)." error={errorFor(fe, "code")}>
        <Input className={`${inputCls} font-mono`} maxLength={32} value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
      </FormField>
      <FormField label="Label" required hint="The name used in contracts." error={errorFor(fe, "label")}>
        <Input className={inputCls} maxLength={60} value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
      </FormField>
      <FormField label="Order" hint="Lower first." error={errorFor(fe, "sort_order")}>
        <Input className={inputCls} inputMode="numeric" value={form.sort} onChange={(e) => setForm({ ...form, sort: e.target.value })} />
      </FormField>
    </FormDrawer>
  );
}

export function NewTierDiscountDrawer({ tiers, initialTier, onClose }: { tiers: PartnerTier[]; initialTier?: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ tier: initialTier ?? tiers[0]?.code ?? "", pct: "", valid_from: todayUtc(), note: "" });
  const [localError, setLocalError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: agencyPricingApi.createDiscount,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agency-pricing"] });
      toast.success("Discount saved as Pending approval. A second person has to approve it before quotes use it.");
      onClose();
    },
  });
  function submit() {
    const pct = discountFraction(form.pct);
    if (!form.tier || pct === null) {
      setLocalError("Enter the discount as a percentage from 0 to 49.99, for example 3 or 4.5.");
      return;
    }
    setLocalError(null);
    create.mutate({ tier_code: form.tier, discount_pct: pct, valid_from: form.valid_from, note: form.note.trim() || undefined });
  }
  const fe = create.error instanceof ApiError ? create.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="New tier discount"
      submitLabel="Save as pending"
      onSubmit={submit}
      busy={create.isPending}
      dirty={!!(form.pct || form.note)}
      error={localError ?? errorText(create.error)}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">
        A discount is never edited. This adds a dated row for the tier; once a second person approves it, it is taken
        off the agency price of every quote from its date.
      </p>
      <FormField label="Tier" required>
        <Select className={inputCls} value={form.tier} onChange={(e) => setForm({ ...form, tier: e.target.value })}>
          {tiers.map((t) => (
            <option key={t.code} value={t.code}>
              {t.label}
            </option>
          ))}
        </Select>
      </FormField>
      <FormField label="Discount on the agency price" required error={errorFor(fe, "discount_pct")}>
        <PercentInput value={form.pct} onChange={(v) => setForm({ ...form, pct: v })} />
      </FormField>
      <FormField label="Valid from" required error={errorFor(fe, "valid_from")}>
        <DateField className={inputCls} value={form.valid_from} onChange={(v) => setForm({ ...form, valid_from: v })} />
      </FormField>
      <FormField label="Note" hint="Why the change, or who confirmed it." error={errorFor(fe, "note")}>
        <Input className={inputCls} maxLength={500} value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
      </FormField>
    </FormDrawer>
  );
}

export type RateCardKind = "discount" | "end";

export function NewRateCardDrawer({
  partners,
  initialPartner,
  initialTour,
  initialKind = "discount",
  onClose,
}: {
  partners: PartnerTerms[];
  initialPartner?: string;
  initialTour?: string;
  initialKind?: RateCardKind;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({
    partner: initialPartner ?? partners[0]?.id ?? "",
    kind: initialKind,
    tour: initialTour ?? "",
    pct: "",
    valid_from: todayUtc(),
    valid_to: "",
    ref: "",
    note: "",
  });
  const [localError, setLocalError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: agencyPricingApi.createRateCard,
    onSuccess: (_row, body) => {
      qc.invalidateQueries({ queryKey: ["agency-pricing"] });
      toast.success(
        body.discount_pct === null
          ? "End card saved as Pending approval. Once a second person approves it, the agency is back on its tier from that day."
          : "Rate card saved as Pending approval. A second person has to approve it before quotes use it.",
      );
      onClose();
    },
  });
  const ending = form.kind === "end";
  function submit() {
    const pct = ending ? null : discountFraction(form.pct);
    if (!form.partner || (!ending && pct === null)) {
      setLocalError(ending ? "Pick the agency." : "Pick the agency and enter the discount as a percentage from 0 to 49.99.");
      return;
    }
    if (form.valid_to && form.valid_to < form.valid_from) {
      setLocalError("The last day is on or after the first day.");
      return;
    }
    setLocalError(null);
    create.mutate({
      partner_id: form.partner,
      source_id: form.tour.trim() || undefined,
      discount_pct: pct,
      valid_from: form.valid_from,
      valid_to: form.valid_to || undefined,
      contract_ref: form.ref.trim() || undefined,
      note: form.note.trim() || undefined,
    });
  }
  const fe = create.error instanceof ApiError ? create.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={ending ? "End a contract rate" : "New contract rate card"}
      submitLabel="Save as pending"
      onSubmit={submit}
      busy={create.isPending}
      dirty={!!(form.pct || form.tour || form.ref || form.note) || form.kind !== initialKind}
      error={localError ?? errorText(create.error)}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">
        A rate card gives one agency its contract discount instead of the tier discount, for every tour or for one tour. A card
        for one tour wins over a card for every tour. An end card stops the contract rate of the same agency and tours from
        its first day, and the agency is back on its tier. A second person approves either before quotes use it.
      </p>
      <FormField label="Agency" required error={errorFor(fe, "partner_id")}>
        <Select className={inputCls} value={form.partner} onChange={(e) => setForm({ ...form, partner: e.target.value })}>
          {partners.map((p) => (
            <option key={p.id} value={p.id}>
              {p.agency_name}
            </option>
          ))}
        </Select>
      </FormField>
      <FormField label="Tour key" hint="Empty = every tour. Otherwise the catalog key of one tour (Trip Plans)." error={errorFor(fe, "source_id")}>
        <Input className={`${inputCls} font-mono`} maxLength={128} value={form.tour} onChange={(e) => setForm({ ...form, tour: e.target.value })} />
      </FormField>
      <FormField label="Card" required>
        <Select className={inputCls} value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as RateCardKind })}>
          <option value="discount">Contract discount</option>
          <option value="end">End the contract rate (back to the tier)</option>
        </Select>
      </FormField>
      {!ending && (
        <FormField label="Discount on the agency price" required error={errorFor(fe, "discount_pct")}>
          <PercentInput value={form.pct} onChange={(v) => setForm({ ...form, pct: v })} />
        </FormField>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label={ending ? "Ends from" : "Valid from"} required error={errorFor(fe, "valid_from")}>
          <DateField className={inputCls} value={form.valid_from} onChange={(v) => setForm({ ...form, valid_from: v })} />
        </FormField>
        <FormField
          label="Valid to"
          hint={ending ? "Empty = for good. A date brings back the earlier card after it." : "Empty = no end date."}
          error={errorFor(fe, "valid_to")}
        >
          <DateField className={inputCls} value={form.valid_to} onChange={(v) => setForm({ ...form, valid_to: v })} />
        </FormField>
      </div>
      <FormField label="Contract reference" error={errorFor(fe, "contract_ref")}>
        <Input className={inputCls} maxLength={120} value={form.ref} onChange={(e) => setForm({ ...form, ref: e.target.value })} />
      </FormField>
      <FormField label="Note" error={errorFor(fe, "note")}>
        <Input className={inputCls} maxLength={500} value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
      </FormField>
    </FormDrawer>
  );
}
