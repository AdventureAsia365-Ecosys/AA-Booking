"use client";
// app/(staff)/pricing/rules/_parts/PolicyDrawers.tsx — "New rule" and "New FX rate" drawers (AA-810, bk-007).
// Staff type percentages ("30" for 30%); the API takes fractions ("0.3000", NUMERIC(5,4)). Both POSTs create a
// PENDING row; the toast says a second person has to approve it. The API owns every check (ranges, floor <=
// target, margin + uplift + fee < 100%, one row per channel / currency and date): its 422 / 409 show inline.
// AA-1073 rest (bk-048, bk-17): a b2b or fixed_departure rule may carry an early-booking discount (days before start +
// margin points off, both or neither); the 422 codes invalid_early_booking / early_booking_channel show under its inputs.
// S280 (AA-1096 settings UI audit): the text is in the page's language (messages.ts); "Valid from" / "In force from"
// take today or a later day (min on the date field; the API refuses a past day with 422 effective_in_past).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { CHANNELS, type Channel } from "@/lib/api/proposals";
import { pricingPolicyApi } from "@/lib/api/pricingPolicy";
import { earlyBookingAllowed, earlyBookingBody, earlyBookingError, type EarlyField } from "@/ui/booking/earlyBooking";
import { Input, Select, FormDrawer, useT, useToast, DateField } from "@/ui";
import { FormField, errorFor, inputCls } from "../../../_parts/FormField";
import { R, type RKey } from "./messages";
import { apiErrorText, channelLabel, type RT } from "./rulesText";

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** "30" → "0.3000"; "" → "0.0000" when optional; null when not a percentage in [0, 100) with ≤ 2 decimals. */
/** "50" -> "0.5000": a child share from 0 to 100 % (null when not a number in range). */
function childFraction(text: string): string | null {
  const n = Number(text.trim());
  return text.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= 100 ? (n / 100).toFixed(4) : null;
}

/** "12.5" -> "12.50": a USD amount from 0 to 9,999.99 (null when not one). */
function usdAmount(text: string): string | null {
  const n = Number(text.trim());
  return text.trim() !== "" && Number.isFinite(n) && n >= 0 && n < 10000 ? n.toFixed(2) : null;
}

export function percentToFraction(value: string, optional = false): string | null {
  const v = value.trim();
  if (!v) return optional ? "0.0000" : null;
  if (!/^\d{1,2}(\.\d{1,2})?$/.test(v)) return null;
  return (Number(v) / 100).toFixed(4);
}

function errorText(t: RT, err: unknown): string | null {
  if (!err) return null;
  if (err instanceof ApiError) {
    if (err.fieldErrors.length) return null; // shown per field
    if (earlyBookingError(err.code)) return null; // shown under the early-booking inputs
  }
  return apiErrorText(t, err);
}

/** The text of an early-booking helper message (English, ui/booking/earlyBooking.ts) in the page's language. */
function byEnglish(t: RT, text: string): string {
  return text in R.en ? (t(text as RKey) as string) : text;
}

export function PercentInput({ value, onChange, id }: { value: string; onChange: (v: string) => void; id?: string }) {
  return (
    <span className="relative block">
      <Input
        id={id}
        inputMode="decimal"
        className={inputCls} style={{ paddingRight: 32 }}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0"
      />
      <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted">%</span>
    </span>
  );
}

export function NewRuleDrawer({ onClose }: { onClose: () => void }) {
  const t = useT(R);
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({
    channel: "b2b" as Channel,
    target: "",
    floor: "",
    peak: "3",
    card: "3",
    valid_from: todayUtc(),
    note: "",
    rrp: "25",
    child: "",
    infant: "",
    early_days: "",
    early_pts: "",
  });
  const [localError, setLocalError] = useState<string | null>(null);
  const [earlyError, setEarlyError] = useState<{ field: EarlyField; message: string } | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const dirty = !!(form.target || form.floor || form.note || form.early_days || form.early_pts);

  const create = useMutation({
    mutationFn: pricingPolicyApi.createRule,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pricing"] });
      toast.success(t("ruleSaved") as string);
      onClose();
    },
  });

  function submit() {
    const target = percentToFraction(form.target);
    const floor = percentToFraction(form.floor);
    const peak = percentToFraction(form.peak, true);
    const card = percentToFraction(form.card, true);
    // AA-1029 (root ADR 0019 b15): the suggested retail price markup, b2b rules only; empty = no RRP published.
    const rrp = form.channel === "b2b" && form.rrp.trim() ? percentToFraction(form.rrp) : undefined;
    // AA-1075 (bk-15): the child share of the adult price (100 allowed) and the infant net in USD; empty = not set.
    const child = form.child.trim() ? childFraction(form.child) : undefined;
    const infant = form.infant.trim() ? usdAmount(form.infant) : undefined;
    if (!target || !floor || !peak || !card || rrp === null || child === null || infant === null) {
      setLocalError(t("errPercent") as string);
      return;
    }
    if (!(form.valid_from >= todayUtc())) {  // empty or a past day
      setLocalError(t("errDay") as string);
      return;
    }
    const early = earlyBookingBody(form.channel, form.early_days, form.early_pts);
    if (!early.ok) {
      setLocalError(null);
      setEarlyError({ field: early.field, message: early.message });
      return;
    }
    setLocalError(null);
    setEarlyError(null);
    create.mutate({
      channel: form.channel,
      target_margin: target,
      floor_margin: floor,
      peak_uplift_pts: peak,
      card_fee_pct: card,
      valid_from: form.valid_from,
      note: form.note.trim() || undefined,
      ...(rrp ? { rrp_markup_pct: rrp } : {}),
      ...(child ? { child_pct: child } : {}),
      ...(infant ? { infant_flat_usd: infant } : {}),
      ...early.body,
    });
  }

  const fe = create.error instanceof ApiError ? create.error.fieldErrors : [];
  const apiEarly = create.error instanceof ApiError ? earlyBookingError(create.error.code) : null;
  const shownEarly = earlyError ?? apiEarly;
  const earlyFor = (field: EarlyField) => errorFor(fe, field) ?? (shownEarly?.field === field ? byEnglish(t, shownEarly.message) : null);
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("ruleTitle") as string}
      submitLabel={t("saveAsPending") as string}
      onSubmit={submit}
      busy={create.isPending}
      dirty={dirty}
      error={localError ?? errorText(t, create.error)}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">{t("ruleIntro")}</p>
      <FormField label={t("fieldChannel") as string} required>
        <Select className={inputCls} value={form.channel} onChange={(e) => set("channel")(e.target.value)}>
          {CHANNELS.map((c) => (
            <option key={c.value} value={c.value}>
              {channelLabel(t, c.value)}
            </option>
          ))}
        </Select>
      </FormField>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label={t("fieldTarget") as string} required hint={t("fieldTargetHint")} error={errorFor(fe, "target_margin")}>
          <PercentInput value={form.target} onChange={set("target")} />
        </FormField>
        <FormField label={t("fieldFloor") as string} required hint={t("fieldFloorHint")} error={errorFor(fe, "floor_margin")}>
          <PercentInput value={form.floor} onChange={set("floor")} />
        </FormField>
        <FormField label={t("fieldPeak") as string} hint={t("fieldPeakHint")} error={errorFor(fe, "peak_uplift_pts")}>
          <PercentInput value={form.peak} onChange={set("peak")} />
        </FormField>
        <FormField label={t("fieldCard") as string} hint={t("fieldCardHint")} error={errorFor(fe, "card_fee_pct")}>
          <PercentInput value={form.card} onChange={set("card")} />
        </FormField>
      </div>
      {form.channel === "b2b" && (
        <FormField
          label={t("fieldRrp") as string}
          hint={t("fieldRrpHint")}
          error={errorFor(fe, "rrp_markup_pct")}
        >
          <PercentInput value={form.rrp} onChange={set("rrp")} />
        </FormField>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField
          label={t("fieldChild") as string}
          hint={t("fieldChildHint")}
          error={errorFor(fe, "child_pct")}
        >
          <PercentInput value={form.child} onChange={set("child")} />
        </FormField>
        <FormField
          label={t("fieldInfant") as string}
          hint={t("fieldInfantHint")}
          error={errorFor(fe, "infant_flat_usd")}
        >
          <Input className={inputCls} inputMode="decimal" value={form.infant} onChange={(e) => set("infant")(e.target.value)} />
        </FormField>
      </div>
      {earlyBookingAllowed(form.channel) && (
        // AA-1073 rest (bk-17): fixed-departure prices only (b2b agencies book them too); both or neither.
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2" data-testid="rule-early-booking">
          <FormField
            label={t("fieldEarlyDays") as string}
            hint={t("fieldEarlyDaysHint")}
            error={earlyFor("early_days")}
          >
            <Input
              className={`${inputCls} tabular-nums`}
              inputMode="numeric"
              maxLength={3}
              value={form.early_days}
              onChange={(e) => set("early_days")(e.target.value)}
            />
          </FormField>
          <FormField
            label={t("fieldEarlyPts") as string}
            hint={t("fieldEarlyPtsHint")}
            error={earlyFor("early_discount_pts")}
          >
            <PercentInput value={form.early_pts} onChange={set("early_pts")} />
          </FormField>
        </div>
      )}
      <FormField label={t("fieldValidFrom") as string} required hint={t("fieldValidFromHint")} error={errorFor(fe, "valid_from")}>
        <DateField className={inputCls} value={form.valid_from} onChange={(v) => set("valid_from")(v)} min={todayUtc()} />
      </FormField>
      <FormField label={t("fieldNote") as string} hint={t("fieldNoteHint")} error={errorFor(fe, "note")}>
        <Input className={inputCls} maxLength={500} value={form.note} onChange={(e) => set("note")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}

export function NewFxRateDrawer({ onClose }: { onClose: () => void }) {
  const t = useT(R);
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
      toast.success(t("fxSaved", row.currency) as string);
      onClose();
    },
  });

  function submit() {
    const currency = form.currency.trim().toUpperCase();
    const rateValue = form.rate.trim();
    const buffer = percentToFraction(form.buffer, true);
    if (!/^[A-Z]{3}$/.test(currency) || currency === "USD") {
      setLocalError(t("errCurrency") as string);
      return;
    }
    if (!/^\d{1,10}(\.\d{1,8})?$/.test(rateValue) || Number(rateValue) <= 0) {
      setLocalError(t("errRate") as string);
      return;
    }
    if (!buffer) {
      setLocalError(t("errBuffer") as string);
      return;
    }
    if (!form.source.trim()) {
      setLocalError(t("errSource") as string);
      return;
    }
    if (!(form.as_of >= todayUtc())) {  // empty or a past day
      setLocalError(t("errAsOf") as string);
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
      title={t("fxTitle") as string}
      submitLabel={t("saveAsPending") as string}
      onSubmit={submit}
      busy={create.isPending}
      dirty={dirty}
      error={localError ?? errorText(t, create.error)}
      fieldErrors={fe}
    >
      <p className="text-[13px] text-muted">{t("fxIntro")}</p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label={t("fieldCurrency") as string} required hint={t("fieldCurrencyHint")} error={errorFor(fe, "currency")}>
          <Input
            className={`${inputCls} font-mono uppercase`}
            maxLength={3}
            value={form.currency}
            onChange={(e) => set("currency")(e.target.value.toUpperCase())}
            placeholder="EUR"
          />
        </FormField>
        <FormField label={t("fieldRate") as string} required hint="EUR 0.92 · VND 25400" error={errorFor(fe, "rate_per_usd")}>
          <Input inputMode="decimal" className={`${inputCls} tabular-nums`} value={form.rate} onChange={(e) => set("rate")(e.target.value)} />
        </FormField>
        <FormField label={t("fieldBuffer") as string} hint={t("fieldBufferHint")} error={errorFor(fe, "buffer_pct")}>
          <PercentInput value={form.buffer} onChange={set("buffer")} />
        </FormField>
        <FormField label={t("fieldAsOf") as string} required hint={t("fieldAsOfHint")} error={errorFor(fe, "as_of")}>
          <DateField className={inputCls} value={form.as_of} onChange={(v) => set("as_of")(v)} min={todayUtc()} />
        </FormField>
      </div>
      <FormField label={t("fieldSource") as string} required hint={t("fieldSourceHint")} error={errorFor(fe, "source")}>
        <Input className={inputCls} maxLength={200} value={form.source} onChange={(e) => set("source")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
