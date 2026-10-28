"use client";
// app/(staff)/catalog/trip-plans/_parts/CommercialDrawer.tsx — commercial edit of one trip plan (AA-679, bk-009;
// UI spec §3 /commercial "sellable, supplier, price matrix, trip style"; Figma CREATE / EDIT TEMPLATE, commercial
// parts only). PUT /booking/catalog/trip-plans/{source_id}/commercial with EVERY field (a cleared field is saved as
// empty), then, when the matrix was edited, PUT …/prices with the whole matrix (bk-032).
// AA-996: the Figma CREATE / EDIT TEMPLATE form shows the tour content too; here it is the read-only "From CIS" block
// (title, SKU, country, duration, days, summary, inclusions, exclusions) with "Edit in CIS ↗" for the roles that edit
// content in CIS. Slice 2 adds the Supplier select (Figma "Supplier" card: active suppliers) and the price matrix
// (Figma "Pricing" card: per-person prices by channel, season and pax band, PriceMatrixEditor). The price hint stays
// as a free "from" guide for plans without a matrix. The supplier select lists the first 100 active suppliers (the
// API page cap); a search box above it (debounced `q`) finds the others, and the picked supplier stays listed.

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { CHANNELS, SELLING_CURRENCIES, type Channel } from "@/lib/api/proposals";
import { suppliersApi } from "@/lib/api/suppliers";
import { tripPlansApi, type PlanSupplier, type TripPlanDetail } from "@/lib/api/tripPlans";
import { FormDrawer, useCan, useStaff, useToast } from "@/ui";
import { draftsFrom, draftsToBody, type DraftError, type PriceDraft } from "@/ui/booking/tripPlans";
import { CisField, CisFieldset } from "../../../_parts/CisFields";
import { CIS_EDITOR_ROLES } from "./shared";
import { FormField, errorFor, inputCls } from "../../../_parts/FormField";
import { PriceMatrixEditor } from "./PriceMatrixEditor";

const textareaCls =
  "min-h-[96px] w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border";

interface FormState {
  sellable: boolean;
  trip_style: string;
  default_channel: Channel | "";
  supplier_id: string;
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
    supplier_id: plan.supplier_id ?? "",
    hint_amount: plan.price_hint?.amount ?? "",
    hint_currency: plan.price_hint?.currency ?? "USD",
    hint_basis: plan.price_hint?.basis ?? "per_person",
    notes: plan.commercial_notes ?? "",
  };
}

const AMOUNT_RE = /^\d{1,10}(\.\d{1,2})?$/;
const todayIso = () => new Date().toISOString().slice(0, 10);

export function CommercialDrawer({ plan, onClose }: { plan: TripPlanDetail; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const me = useStaff();
  const canPrice = useCan("pricing.read");
  const canPickSupplier = useCan("supply.read");
  const [start] = useState(() => initial(plan));
  const [form, setForm] = useState<FormState>(start);
  const [localError, setLocalError] = useState<string | null>(null);
  // null = the matrix as loaded (untouched): only an edited matrix is sent, so a slow load can never wipe it.
  const [drafts, setDrafts] = useState<PriceDraft[] | null>(null);
  const [rowErrors, setRowErrors] = useState<DraftError[]>([]);
  const [needReason, setNeedReason] = useState(false);
  const [reason, setReason] = useState("");
  const [today] = useState(todayIso);
  const [supplierSearch, setSupplierSearch] = useState("");
  const [supplierQ, setSupplierQ] = useState("");
  // The supplier shown as picked, kept as an option even when a search no longer lists it.
  const [pickedSupplier, setPickedSupplier] = useState<PlanSupplier | null>(plan.supplier);

  useEffect(() => {
    const id = window.setTimeout(() => setSupplierQ(supplierSearch.trim()), 300);
    return () => window.clearTimeout(id);
  }, [supplierSearch]);

  const prices = useQuery({
    queryKey: ["trip-plans", "prices", plan.source_id],
    queryFn: () => tripPlansApi.prices(plan.source_id),
    enabled: canPrice,
  });
  const suppliers = useQuery({
    queryKey: ["suppliers", "active-options", supplierQ],
    queryFn: () => suppliersApi.list({ status: "active", sort: "company_name", limit: 100, q: supplierQ || undefined }),
    enabled: canPickSupplier,
    staleTime: 60_000,
  });
  const loadedDrafts = draftsFrom(prices.data?.items ?? []);
  const rows = drafts ?? loadedDrafts;
  const pricesDirty = drafts !== null && JSON.stringify(drafts) !== JSON.stringify(loadedDrafts);
  const dirty = JSON.stringify(form) !== JSON.stringify(start) || pricesDirty;

  const save = useMutation({
    mutationFn: async () => {
      const amount = form.hint_amount.trim();
      const out = await tripPlansApi.saveCommercial(plan.source_id, {
        sellable: form.sellable,
        trip_style: form.trip_style.trim() || null,
        default_channel: form.default_channel || null,
        price_hint: amount ? { amount, currency: form.hint_currency, basis: form.hint_basis } : null,
        commercial_notes: form.notes.trim() || null,
        ...(canPickSupplier ? { supplier_id: form.supplier_id || null } : {}),
      });
      qc.setQueryData(["trip-plans", "detail", plan.source_id], out);
      let matrix = null;
      if (pricesDirty && drafts) {
        matrix = await tripPlansApi.savePrices(plan.source_id, {
          items: draftsToBody(drafts).items,
          below_floor_reason: reason.trim() || null,
        });
        qc.setQueryData(["trip-plans", "prices", plan.source_id], matrix);
      }
      return { out, matrix };
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["trip-plans"] });
    },
    onSuccess: ({ out, matrix }) => {
      toast.success(out.changed?.length || matrix ? "Commercial details saved" : "Nothing changed");
      onClose();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "override_reason_required") setNeedReason(true);
    },
  });

  function submit() {
    const amount = form.hint_amount.trim();
    if (amount && (!AMOUNT_RE.test(amount) || Number(amount) <= 0)) {
      setLocalError("Enter the price hint as an amount above 0 with up to 2 decimals, for example 2450 or 2450.50.");
      return;
    }
    if (pricesDirty && drafts) {
      const { errors } = draftsToBody(drafts);
      setRowErrors(errors);
      if (errors.length) {
        setLocalError(`Check the price matrix: ${errors.length === 1 ? "one row needs" : `${errors.length} fields need`} attention.`);
        return;
      }
    }
    if (needReason && !reason.trim()) {
      setLocalError("Give a reason for the prices below the channel floor.");
      return;
    }
    setLocalError(null);
    save.mutate();
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  const apiMessage =
    save.error instanceof ApiError ? (fe.length ? null : save.error.message) : save.error instanceof Error ? save.error.message : null;
  const supplierOptions = suppliers.data?.items ?? [];
  const currentSupplier = form.supplier_id && pickedSupplier?.id === form.supplier_id ? pickedSupplier : null;
  const showCurrent = currentSupplier && !supplierOptions.some((s) => s.id === currentSupplier.id);
  const supplierTotal = suppliers.data?.total ?? 0;
  const supplierHint = suppliers.isError
    ? "The supplier list could not be loaded."
    : supplierTotal > supplierOptions.length
      ? `Showing the first ${supplierOptions.length} of ${supplierTotal} active suppliers; search to find the others.`
      : supplierQ && !supplierOptions.length
        ? "No active supplier matches the search."
        : "The main supplier (ground operator) of this plan. Active suppliers only.";
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
      width={640}
    >
      <p className="text-[13px] text-muted">
        Booking-owned fields of <span className="font-medium text-ink">{plan.title}</span>. The tour itself (title, days,
        photos, inclusions) is edited in CIS.
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
        <CisField label="Included" multiline wide>
          {plan.inclusions}
        </CisField>
        <CisField label="Not included" multiline wide>
          {plan.exclusions}
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
      {canPickSupplier && (
        <FormField label="Supplier" hint={supplierHint} error={errorFor(fe, "supplier_id")}>
          <input
            aria-label="Search active suppliers"
            className={inputCls}
            placeholder="Search by name, code or contact"
            maxLength={200}
            value={supplierSearch}
            onChange={(e) => setSupplierSearch(e.target.value)}
            autoComplete="off"
          />
          <select
            aria-label="Supplier"
            className={inputCls}
            value={form.supplier_id}
            onChange={(e) => {
              const id = e.target.value;
              const hit = supplierOptions.find((o) => o.id === id);
              if (hit) setPickedSupplier({ id: hit.id, code: hit.code, company_name: hit.company_name, status: "active" });
              setForm({ ...form, supplier_id: id });
            }}
            disabled={suppliers.isLoading}
          >
            <option value="">{suppliers.isLoading ? "Loading suppliers…" : "No supplier"}</option>
            {showCurrent && (
              <option value={currentSupplier.id}>
                {currentSupplier.company_name ?? currentSupplier.code} {currentSupplier.status === "inactive" ? "(inactive)" : ""}
              </option>
            )}
            {supplierOptions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.company_name} · {s.code}
              </option>
            ))}
          </select>
        </FormField>
      )}
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
      {canPrice &&
        (prices.isLoading ? (
          <p className="text-[13px] text-muted">Loading the price matrix…</p>
        ) : prices.isError ? (
          <p className="text-[13px] text-danger">The price matrix could not be loaded. Save the other fields, then reopen.</p>
        ) : (
          <PriceMatrixEditor
            rows={rows}
            onChange={(next) => {
              setDrafts(next);
              setRowErrors([]);
              // The API asks for the reason again if a row is still below the floor after the edit.
              setNeedReason(false);
            }}
            errors={rowErrors}
            saved={prices.data?.items ?? []}
            today={today}
          />
        ))}
      {needReason && (
        <FormField label="Reason for prices below the floor" required hint="Kept in the audit log with the price matrix.">
          <input className={inputCls} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        </FormField>
      )}
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
          A free guide for sales when the plan has no price matrix; the matrix wins where it has a price. Leave the amount empty for no hint.
        </span>
      </fieldset>
      <FormField label="Commercial notes" hint="Staff only, never shown to customers." error={errorFor(fe, "commercial_notes")}>
        <textarea className={textareaCls} maxLength={5000} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
      </FormField>
    </FormDrawer>
  );
}
