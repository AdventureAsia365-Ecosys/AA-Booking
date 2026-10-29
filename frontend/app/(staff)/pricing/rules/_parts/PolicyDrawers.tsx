"use client";
// app/(staff)/pricing/rules/_parts/PolicyDrawers.tsx — "New rule" and "New FX rate" drawers (AA-810, bk-007).
// Staff type percentages ("30" for 30%); the API takes fractions ("0.3000", NUMERIC(5,4)). Both POSTs create a
// PENDING row; the toast says a second person has to approve it. The API owns every check (ranges, floor <=
// target, margin + uplift + fee < 100%, one row per channel / currency and date): its 422 / 409 show inline.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { CHANNELS, type Channel } from "@/lib/api/proposals";
import { pricingPolicyApi } from "@/lib/api/pricingPolicy";
import { FormDrawer, useToast, DateField } from "@/ui";
import { FormField, errorFor, inputCls } from "../../../_parts/FormField";

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** "30" → "0.3000"; "" → "0.0000" when optional; null when not a percentage in [0, 100) with ≤ 2 decimals. */
export function percentToFraction(value: string, optional = false): string | null {
  const v = value.trim();
  if (!v) return optional ? "0.0000" : null;
  if (!/^\d{1,2}(\.\d{1,2})?$/.test(v)) return null;
  return (Number(v) / 100).toFixed(4);
}

function errorText(err: unknown): string | null {
  if (!err) return null;
  if (err instanceof ApiError) {
    if (err.fieldErrors.length) return null; // shown per field
    return err.message;
  }
  return err instanceof Error ? err.message : "Could not save.";
}

function PercentInput({ value, onChange, id }: { value: string; onChange: (v: string) => void; id?: string }) {
  return (
    <span className="relative block">
      <input
        id={id}
        inputMode="decimal"
        className={`${inputCls} pr-8`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0"
      />
      <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted">%</span>
    </span>
  );
}

export function NewRuleDrawer({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({
    channel: "b2c_tailor_made" as Channel,
    target: "",
    floor: "",
    peak: "3",
    card: "3",
    valid_from: todayUtc(),
    note: "",
  });
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const dirty = !!(form.target || form.floor || form.note);

  const create = useMutation({
    mutationFn: pricingPolicyApi.createRule,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pricing"] });
      toast.success("Rule saved as Pending approval. A second person has to approve it before quotes use it.");
      onClose();
    },
  });

  function submit() {
    const target = percentToFraction(form.target);
    const floor = percentToFraction(form.floor);
    const peak = percentToFraction(form.peak, true);
    const card = percentToFraction(form.card, true);
    if (!target || !floor || !peak || !card) {
      setLocalError("Enter each percentage as a number from 0 to 99.99, for example 30 or 2.5.");
      return;
    }
    if (!form.valid_from) {
      setLocalError("Pick the day the rule takes effect.");
      return;
    }
    setLocalError(null);
    create.mutate({
      channel: form.channel,
      target_margin: target,
      floor_margin: floor,
      peak_uplift_pts: peak,
      card_fee_pct: card,
      valid_from: form.valid_from,
      note: form.note.trim() || undefined,
    });
  }

  const fe = create.error instanceof ApiError ? create.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="New pricing rule"
      submitLabel="Save as pending"
      onSubmit={submit}
      busy={create.isPending}
      dirty={dirty}
      error={localError ?? errorText(create.error)}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">
        A rule is never edited. This adds a new row for the channel; once approved by a second person it is in force
        from its date (the newest approved rule on or before the pricing day wins).
      </p>
      <FormField label="Channel" required>
        <select className={inputCls} value={form.channel} onChange={(e) => set("channel")(e.target.value)}>
          {CHANNELS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </FormField>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="Target margin" required hint="Gross margin on the selling price." error={errorFor(fe, "target_margin")}>
          <PercentInput value={form.target} onChange={set("target")} />
        </FormField>
        <FormField label="Floor margin" required hint="Below this a sales override needs a reason." error={errorFor(fe, "floor_margin")}>
          <PercentInput value={form.floor} onChange={set("floor")} />
        </FormField>
        <FormField label="Peak uplift (points)" hint="Added to the target margin in peak season." error={errorFor(fe, "peak_uplift_pts")}>
          <PercentInput value={form.peak} onChange={set("peak")} />
        </FormField>
        <FormField label="Card fee" hint="Built into the price, not a surcharge." error={errorFor(fe, "card_fee_pct")}>
          <PercentInput value={form.card} onChange={set("card")} />
        </FormField>
      </div>
      <FormField label="Valid from" required error={errorFor(fe, "valid_from")}>
        <DateField className={inputCls} value={form.valid_from} onChange={(v) => set("valid_from")(v)} />
      </FormField>
      <FormField label="Note" hint="Why the change, or who confirmed it (for example Ms. Thư, 14/10)." error={errorFor(fe, "note")}>
        <input className={inputCls} maxLength={500} value={form.note} onChange={(e) => set("note")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}

export function NewFxRateDrawer({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ currency: "", rate: "", buffer: "2", as_of: todayUtc(), source: "" });
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const dirty = !!(form.currency || form.rate || form.source);

  const create = useMutation({
    mutationFn: pricingPolicyApi.createFxRate,
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["pricing"] });
      toast.success(`${row.currency} rate saved as Pending approval. A second person has to approve it before quotes use it.`);
      onClose();
    },
  });

  function submit() {
    const currency = form.currency.trim().toUpperCase();
    const rateValue = form.rate.trim();
    const buffer = percentToFraction(form.buffer, true);
    if (!/^[A-Z]{3}$/.test(currency) || currency === "USD") {
      setLocalError("Currency is a 3-letter ISO code other than USD (USD is the base).");
      return;
    }
    if (!/^\d{1,10}(\.\d{1,8})?$/.test(rateValue) || Number(rateValue) <= 0) {
      setLocalError("Rate is the number of units of the currency per 1 USD, for example 0.92 for EUR or 25400 for VND.");
      return;
    }
    if (!buffer) {
      setLocalError("Enter the buffer as a percentage from 0 to 99.99, for example 2.");
      return;
    }
    if (!form.source.trim()) {
      setLocalError("Say where the rate comes from (bank, ECB, manual-dev, …).");
      return;
    }
    setLocalError(null);
    create.mutate({ currency, rate_per_usd: rateValue, buffer_pct: buffer, as_of: form.as_of, source: form.source.trim() });
  }

  const fe = create.error instanceof ApiError ? create.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="New FX rate"
      submitLabel="Save as pending"
      onSubmit={submit}
      busy={create.isPending}
      dirty={dirty}
      error={localError ?? errorText(create.error)}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">
        Rates are dated and never edited. Supplier costs convert to USD at the plain rate; a quote sold in this currency
        adds the buffer. A second person approves the row before any quote uses it.
      </p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="Currency" required hint="ISO code, e.g. EUR, GBP, VND." error={errorFor(fe, "currency")}>
          <input
            className={`${inputCls} font-mono uppercase`}
            maxLength={3}
            value={form.currency}
            onChange={(e) => set("currency")(e.target.value.toUpperCase())}
            placeholder="EUR"
          />
        </FormField>
        <FormField label="Units per 1 USD" required hint="EUR 0.92 · VND 25400" error={errorFor(fe, "rate_per_usd")}>
          <input inputMode="decimal" className={`${inputCls} tabular-nums`} value={form.rate} onChange={(e) => set("rate")(e.target.value)} />
        </FormField>
        <FormField label="Selling buffer" hint="ADR 0005: 2–3% on non-USD selling prices." error={errorFor(fe, "buffer_pct")}>
          <PercentInput value={form.buffer} onChange={set("buffer")} />
        </FormField>
        <FormField label="As of" required hint="The market date of the rate." error={errorFor(fe, "as_of")}>
          <DateField className={inputCls} value={form.as_of} onChange={(v) => set("as_of")(v)} />
        </FormField>
      </div>
      <FormField label="Source" required hint="Where the rate comes from: bank, ECB, or manual-dev for a Dev placeholder." error={errorFor(fe, "source")}>
        <input className={inputCls} maxLength={200} value={form.source} onChange={(e) => set("source")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
