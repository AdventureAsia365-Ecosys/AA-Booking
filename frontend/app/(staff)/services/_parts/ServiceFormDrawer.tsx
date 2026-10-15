"use client";
// app/(staff)/services/_parts/ServiceFormDrawer.tsx — Add service / Edit service (AA-823, bk-010; UI spec §6.3). One
// form for activity, transfer, guide, entrance and meal: supplier (fixed once created; it needs the matching category),
// type, internal title, pricing unit of its rates (fixed once it has rates), CIS place, an optional CIS catalog
// reference, max group size, duration, inclusions and internal notes. The title is a working name for staff: the
// customer-facing text is CIS content (O7).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { SERVICE_TYPES, supplyApi, type ServiceBody, type ServiceDetail, type ServiceType, type ServiceUnit } from "@/lib/api/supply";
import { FormDrawer, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { CATEGORY_FOR, SERVICE_TYPE_LABEL, SERVICE_UNIT_OPTIONS, SupplierSelect, joinList, splitList, supplyError } from "@/app/(staff)/_parts/supply";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { DestinationPicker, type PickedPlace } from "@/app/(staff)/suppliers/_parts/DestinationPicker";

const DEFAULT_UNIT: Record<ServiceType, ServiceUnit> = {
  activity: "person",
  transfer: "vehicle",
  guide: "day",
  entrance: "person",
  meal: "person",
};

export function ServiceFormDrawer({
  service,
  onClose,
  onSaved,
}: {
  service?: ServiceDetail;
  onClose: () => void;
  onSaved?: (s: ServiceDetail) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const countries = useCountries();
  const initial = {
    supplier_id: service?.supplier_id ?? "",
    service_type: (service?.service_type ?? "activity") as ServiceType,
    title: service?.title ?? "",
    unit: (service?.unit ?? "person") as ServiceUnit,
    country_code: service?.country_code ?? "",
    source_id: service?.source_id ?? "",
    max_group_size: service?.max_group_size ? String(service.max_group_size) : "",
    duration: service?.duration_minutes ? String(service.duration_minutes) : "",
    inclusions: joinList(service?.inclusions),
    internal_notes: service?.internal_notes ?? "",
  };
  const [form, setForm] = useState(initial);
  const [place, setPlace] = useState<PickedPlace | null>(service?.destination ?? null);
  const [placeId, setPlaceId] = useState<string | null>(service?.destination_id ?? null);
  const [localError, setLocalError] = useState<string | null>(null);
  const set = <K extends keyof typeof form>(k: K) => (v: (typeof form)[K]) => setForm((f) => ({ ...f, [k]: v }));
  const dirty = JSON.stringify(form) !== JSON.stringify(initial) || placeId !== (service?.destination_id ?? null);
  const unitLocked = !!service && service.rates_in_force > 0;

  const save = useMutation({
    mutationFn: (body: ServiceBody) => (service ? supplyApi.updateService(service.code, body) : supplyApi.createService(body)),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["services"] });
      qc.invalidateQueries({ queryKey: ["service"] });
      toast.success(service ? `${s.title} saved.` : `${s.title} added as ${s.code}.`);
      onSaved?.(s);
      onClose();
    },
  });

  function submit() {
    if (!service && !form.supplier_id) return setLocalError("Pick the supplier that delivers this service.");
    if (!form.title.trim()) return setLocalError("Give the service a working title.");
    const group = form.max_group_size ? Number(form.max_group_size) : null;
    if (group !== null && (!Number.isInteger(group) || group < 1 || group > 999)) return setLocalError("Max group size is a whole number from 1 to 999.");
    const minutes = form.duration ? Number(form.duration) : null;
    if (minutes !== null && (!Number.isInteger(minutes) || minutes < 1)) return setLocalError("Duration is a whole number of minutes.");
    setLocalError(null);
    const clean = (v: string) => v.trim() || null;
    const body: ServiceBody = {
      service_type: form.service_type,
      title: form.title.trim(),
      unit: form.unit,
      destination_id: placeId,
      country_code: clean(form.country_code),
      source_id: clean(form.source_id),
      max_group_size: group,
      duration_minutes: minutes,
      inclusions: splitList(form.inclusions),
      internal_notes: clean(form.internal_notes),
    };
    if (service && unitLocked) delete body.unit;
    save.mutate(service ? body : { ...body, supplier_id: form.supplier_id });
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={service ? `Edit ${service.title}` : "Add service"}
      submitLabel={service ? "Save service" : "Add service"}
      onSubmit={submit}
      busy={save.isPending}
      dirty={dirty}
      error={localError ?? (fe.length ? null : supplyError(save.error))}
      fieldErrors={fe}
      width={560}
    >
      <p className="text-[13px] text-muted">
        What Booking buys from the supplier. The title is for staff; the text customers read is written in CIS.
      </p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="Type" required error={errorFor(fe, "service_type")}>
          <select
            className={inputCls}
            value={form.service_type}
            onChange={(e) => {
              const t = e.target.value as ServiceType;
              setForm((f) => ({ ...f, service_type: t, unit: service ? f.unit : DEFAULT_UNIT[t], supplier_id: service ? f.supplier_id : "" }));
            }}
          >
            {SERVICE_TYPES.map((t) => (
              <option key={t} value={t}>
                {SERVICE_TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </FormField>
        <FormField
          label="Rates are"
          required
          locked={unitLocked}
          hint={unitLocked ? "Fixed: this service already has rates in this unit." : "The unit its net rates are entered in."}
          error={errorFor(fe, "unit")}
        >
          <select className={inputCls} value={form.unit} onChange={(e) => set("unit")(e.target.value as ServiceUnit)} disabled={unitLocked}>
            {SERVICE_UNIT_OPTIONS.map((u) => (
              <option key={u.value} value={u.value}>
                {u.label}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      {service ? (
        <FormField label="Supplier" locked hint="A service stays with the supplier it was created under (its rates live there).">
          <input className={inputCls} value={`${service.supplier_name} (${service.supplier_code})`} disabled />
        </FormField>
      ) : (
        <FormField label="Supplier" required hint={`Active suppliers with the matching category.`} error={errorFor(fe, "supplier_id")}>
          <SupplierSelect value={form.supplier_id} category={CATEGORY_FOR[form.service_type]} onChange={(v) => set("supplier_id")(v)} />
        </FormField>
      )}
      <FormField label="Working title" required error={errorFor(fe, "title")}>
        <input className={inputCls} maxLength={200} value={form.title} onChange={(e) => set("title")(e.target.value)} placeholder="e.g. Old Quarter street food walk" />
      </FormField>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-ink">City / place</span>
          <DestinationPicker
            value={place ?? (placeId ? { id: placeId, name: "Place not readable now", country: "CIS" } : null)}
            country={form.country_code}
            onChange={(p) => {
              setPlace(p ? { id: p.destination_id, name: p.name, country: p.country } : null);
              setPlaceId(p?.destination_id ?? null);
              if (p?.country_code) set("country_code")(p.country_code);
            }}
          />
          <span className="text-xs text-muted">A CIS place. Picking one fills the country.</span>
        </div>
        <FormField label="Country" error={errorFor(fe, "country_code")}>
          <select className={inputCls} value={form.country_code} onChange={(e) => set("country_code")(e.target.value)}>
            <option value="">—</option>
            {(countries.data?.items ?? []).map((c) => (
              <option key={c.iso2} value={c.iso2}>
                {c.name}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <FormField label="Max group size" error={errorFor(fe, "max_group_size")}>
          <input type="number" min={1} max={999} className={inputCls} value={form.max_group_size} onChange={(e) => set("max_group_size")(e.target.value)} />
        </FormField>
        <FormField label="Duration (minutes)" error={errorFor(fe, "duration_minutes")}>
          <input type="number" min={1} className={inputCls} value={form.duration} onChange={(e) => set("duration")(e.target.value)} />
        </FormField>
      </div>
      <FormField label="Inclusions" hint="Separate with commas, e.g. Guide, Tastings, Water." error={errorFor(fe, "inclusions")}>
        <input className={inputCls} value={form.inclusions} onChange={(e) => set("inclusions")(e.target.value)} />
      </FormField>
      <FormField label="CIS catalog reference" hint="Optional: the CIS component or tour id this service delivers." error={errorFor(fe, "source_id")}>
        <input className={`${inputCls} font-mono`} maxLength={120} value={form.source_id} onChange={(e) => set("source_id")(e.target.value)} />
      </FormField>
      <FormField label="Internal notes" hint="Staff only." error={errorFor(fe, "internal_notes")}>
        <textarea className={textareaCls} maxLength={5000} value={form.internal_notes} onChange={(e) => set("internal_notes")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
