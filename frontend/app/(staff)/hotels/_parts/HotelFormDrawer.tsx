"use client";
// app/(staff)/hotels/_parts/HotelFormDrawer.tsx — Add hotel / Edit hotel (AA-823, bk-010; ADR 0004a decision 1). The
// hotel's supply facts only: supplier (fixed once created), name, style, stars, CIS place (typeahead over
// catalog.v_destination; the country follows it), address, geo point, check-in/out, amenities, staff links and internal
// notes. No description or photo: CIS writes the customer-facing content from these facts (O7, Q17).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { supplyApi, type HotelBody, type HotelDetail } from "@/lib/api/supply";
import { FormDrawer, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { SupplierSelect, joinList, splitList, supplyError } from "@/app/(staff)/_parts/supply";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { DestinationPicker, type PickedPlace } from "@/app/(staff)/suppliers/_parts/DestinationPicker";
import { TimeField } from "@/ui/booking/DateField";

const hhmm = (t: string | null) => (t ? t.slice(0, 5) : "");
const linksText = (h?: HotelDetail) => (h?.links ?? []).map((l) => (l.label && l.label !== l.url ? `${l.label} | ${l.url}` : l.url)).join("\n");

function parseLinks(text: string): { label: string | null; url: string }[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [a, b] = line.split("|").map((s) => s.trim());
      return b ? { label: a || null, url: b } : { label: null, url: a };
    });
}

export function HotelFormDrawer({ hotel, onClose, onSaved }: { hotel?: HotelDetail; onClose: () => void; onSaved?: (h: HotelDetail) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const countries = useCountries();
  const initial = {
    supplier_id: hotel?.supplier_id ?? "",
    name: hotel?.name ?? "",
    hotel_style: hotel?.hotel_style ?? "",
    star_rating: hotel?.star_rating ? String(hotel.star_rating) : "",
    address: hotel?.address ?? "",
    country_code: hotel?.country_code ?? "",
    latitude: hotel?.latitude != null ? String(hotel.latitude) : "",
    longitude: hotel?.longitude != null ? String(hotel.longitude) : "",
    check_in_time: hhmm(hotel?.check_in_time ?? null),
    check_out_time: hhmm(hotel?.check_out_time ?? null),
    amenities: joinList(hotel?.amenities),
    links: linksText(hotel),
    internal_notes: hotel?.internal_notes ?? "",
  };
  const [form, setForm] = useState(initial);
  const [place, setPlace] = useState<PickedPlace | null>(hotel?.destination ?? null);
  const [placeId, setPlaceId] = useState<string | null>(hotel?.destination_id ?? null);
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const dirty = JSON.stringify(form) !== JSON.stringify(initial) || placeId !== (hotel?.destination_id ?? null);

  const save = useMutation({
    mutationFn: (body: HotelBody) => (hotel ? supplyApi.updateHotel(hotel.code, body) : supplyApi.createHotel(body)),
    onSuccess: (h) => {
      qc.invalidateQueries({ queryKey: ["hotels"] });
      qc.invalidateQueries({ queryKey: ["hotel"] });
      toast.success(hotel ? `${h.name} saved.` : `${h.name} added as ${h.code}. Add its room types next.`);
      onSaved?.(h);
      onClose();
    },
  });

  function submit() {
    if (!hotel && !form.supplier_id) return setLocalError("Pick the supplier this hotel is contracted through.");
    if (!form.name.trim()) return setLocalError("Give the hotel its name.");
    if (!form.latitude.trim() !== !form.longitude.trim()) return setLocalError("Give both latitude and longitude, or neither.");
    setLocalError(null);
    const clean = (v: string) => v.trim() || null;
    const body: HotelBody = {
      name: form.name.trim(),
      hotel_style: clean(form.hotel_style),
      star_rating: form.star_rating ? Number(form.star_rating) : null,
      address: clean(form.address),
      destination_id: placeId,
      country_code: clean(form.country_code),
      latitude: clean(form.latitude),
      longitude: clean(form.longitude),
      check_in_time: form.check_in_time || null,
      check_out_time: form.check_out_time || null,
      amenities: splitList(form.amenities),
      links: parseLinks(form.links),
      internal_notes: clean(form.internal_notes),
    };
    save.mutate(hotel ? body : { ...body, supplier_id: form.supplier_id });
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={hotel ? `Edit ${hotel.name}` : "Add hotel"}
      submitLabel={hotel ? "Save hotel" : "Add hotel"}
      onSubmit={submit}
      busy={save.isPending}
      dirty={dirty}
      error={localError ?? (fe.length ? null : supplyError(save.error))}
      fieldErrors={fe}
      width={560}
    >
      <p className="text-[13px] text-muted">
        The hotel&apos;s facts as contracted. Descriptions and photos shown to customers are written in CIS from these facts.
      </p>
      {hotel ? (
        <FormField label="Supplier" locked hint="A hotel stays with the supplier it was created under (its rates live there).">
          <input className={inputCls} value={`${hotel.supplier_name} (${hotel.supplier_code})`} disabled />
        </FormField>
      ) : (
        <FormField label="Supplier" required hint="Active suppliers with the Hotel category." error={errorFor(fe, "supplier_id")}>
          <SupplierSelect value={form.supplier_id} category="hotel" onChange={set("supplier_id")} />
        </FormField>
      )}
      <FormField label="Hotel name" required error={errorFor(fe, "name")}>
        <input className={inputCls} maxLength={200} value={form.name} onChange={(e) => set("name")(e.target.value)} placeholder="e.g. Hanoi Emerald Hotel" />
      </FormField>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="Style" hint="e.g. Boutique, Resort, Homestay." error={errorFor(fe, "hotel_style")}>
          <input className={inputCls} maxLength={60} value={form.hotel_style} onChange={(e) => set("hotel_style")(e.target.value)} />
        </FormField>
        <FormField label="Star rating" error={errorFor(fe, "star_rating")}>
          <select className={inputCls} value={form.star_rating} onChange={(e) => set("star_rating")(e.target.value)}>
            <option value="">Not rated</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n} {n === 1 ? "star" : "stars"}
              </option>
            ))}
          </select>
        </FormField>
      </div>
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
      <FormField label="Street address" error={errorFor(fe, "address")}>
        <input className={inputCls} maxLength={500} value={form.address} onChange={(e) => set("address")(e.target.value)} />
      </FormField>
      <div className="grid grid-cols-2 gap-4">
        <FormField label="Latitude" error={errorFor(fe, "latitude")}>
          <input className={inputCls} inputMode="decimal" value={form.latitude} onChange={(e) => set("latitude")(e.target.value)} placeholder="21.0285" />
        </FormField>
        <FormField label="Longitude" error={errorFor(fe, "longitude")}>
          <input className={inputCls} inputMode="decimal" value={form.longitude} onChange={(e) => set("longitude")(e.target.value)} placeholder="105.8542" />
        </FormField>
        <FormField label="Check-in from" error={errorFor(fe, "check_in_time")}>
          <TimeField className={inputCls} value={form.check_in_time} onChange={(v) => set("check_in_time")(v)} />
        </FormField>
        <FormField label="Check-out by" error={errorFor(fe, "check_out_time")}>
          <TimeField className={inputCls} value={form.check_out_time} onChange={(v) => set("check_out_time")(v)} />
        </FormField>
      </div>
      <FormField label="Amenities" hint="Separate with commas, e.g. Pool, Spa, Airport shuttle." error={errorFor(fe, "amenities")}>
        <input className={inputCls} value={form.amenities} onChange={(e) => set("amenities")(e.target.value)} />
      </FormField>
      <FormField label="Staff links" hint="One per line: a URL, or “Label | URL” (extranet, supplier site, map). Not shown to customers." error={errorFor(fe, "links")}>
        <textarea className={textareaCls} value={form.links} onChange={(e) => set("links")(e.target.value)} placeholder="Extranet | https://…" />
      </FormField>
      <FormField label="Internal notes" hint="Staff only." error={errorFor(fe, "internal_notes")}>
        <textarea className={textareaCls} maxLength={5000} value={form.internal_notes} onChange={(e) => set("internal_notes")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
