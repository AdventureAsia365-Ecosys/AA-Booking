"use client";
// app/(staff)/catalog/_parts/GeoDrawers.tsx — AA-996: the Figma drawers of module 06 on the "CIS content + Booking
// state" pattern (UI spec §7):
//   DestinationDrawer  DESTINATION DETAILS (view) and ADD / EDIT DESTINATION (edit): name, country, coordinates and
//                      photos read-only "From CIS"; Booking status, reason and internal notes editable (admin).
//   CountryDrawer      ADD / EDIT COUNTRY: the country's CIS facts read-only; Booking status, reason, area and notes
//                      editable (admin). "Add" picks an ISO country Booking does not list yet (no CIS place there):
//                      countries are never created in Booking.
// Both forms show the record's CIS photos as a read-only thumbnail strip (PhotoStrip; Figma media rows, audit item 3).
// Places and country content are created and edited in CIS (Places / Master content). Writes: PATCH
// /booking/catalog/geo/… (catalog.admin), soft only.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, ExternalLink, MapPin, Pencil, Store, Truck } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { geoApi, type DestinationDetail, type GeoStatus } from "@/lib/api/geo";
import { Button, CisPhoto, DateText, Drawer, FormDrawer, Pill, useCan, useStaff, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { CisField, CisFieldset } from "@/app/(staff)/_parts/CisFields";
import { Fact, FactGrid } from "@/app/(staff)/_parts/DetailHeader";
import { CIS_EDITOR_ROLES } from "../trip-plans/_parts/shared";
import { useGeoStatus } from "./useGeoStatus";

/** CIS admin page where places (and the countries they sit in) are managed. CIS has no per-place deep link. */
export const CIS_PLACES_PATH = "/admin/places";

export function GeoStatusBadge({ status }: { status: GeoStatus }) {
  return status === "active" ? <Pill tone="success">Active</Pill> : <Pill tone="neutral">Inactive</Pill>;
}

export function coords(lat: number | null, lng: number | null): string | null {
  if (lat == null || lng == null) return null;
  return `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? "N" : "S"}, ${Math.abs(lng).toFixed(4)}° ${lng >= 0 ? "E" : "W"}`;
}

function apiMessage(err: unknown): string | null {
  if (!err) return null;
  if (err instanceof ApiError && err.status === 403) return "Only a Booking admin can change the Booking status.";
  return (err as Error).message;
}

/** A read-only strip of CIS photo thumbnails (first `max`, then a "+N" tile up to `total`). Empty = nothing. */
export function PhotoStrip({ paths, total, name, max = 6 }: { paths: string[]; total: number; name: string; max?: number }) {
  if (paths.length === 0) return null;
  const shown = paths.slice(0, max);
  const more = Math.max(total, paths.length) - shown.length;
  return (
    <ul className="grid grid-cols-6 gap-1.5" aria-label={`${name} photos from CIS`} data-testid="geo-photo-strip">
      {shown.map((path, i) => (
        <li key={`${path}-${i}`} className="relative min-w-0">
          <CisPhoto path={path} alt={`${name} photo ${i + 1}`} className="aspect-square w-full rounded-md" />
          {more > 0 && i === shown.length - 1 && (
            <span className="bk-tabular absolute inset-0 grid place-items-center rounded-md bg-black/55 text-[12px] font-semibold text-white">
              +{more}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

// ── Booking state fields (shared) ────────────────────────────────────────────

interface StateForm {
  status: GeoStatus;
  status_reason: string;
  internal_notes: string;
  area: string;
}

function StateFields({
  form,
  setForm,
  withArea,
  fe,
}: {
  form: StateForm;
  setForm: (f: StateForm) => void;
  withArea: boolean;
  fe: ApiError["fieldErrors"];
}) {
  return (
    <section className="flex flex-col gap-4" aria-label="Booking state">
      <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">Booking</span>
      <FormField label="Booking status" hint="Inactive = Booking does not sell trips here. Nothing is deleted." error={errorFor(fe, "status")}>
        <select className={inputCls} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value as GeoStatus })}>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
        </select>
      </FormField>
      {form.status === "inactive" && (
        <FormField label="Reason" hint="Shown to staff next to the status." error={errorFor(fe, "status_reason")}>
          <input className={inputCls} maxLength={500} value={form.status_reason} onChange={(e) => setForm({ ...form, status_reason: e.target.value })} />
        </FormField>
      )}
      {withArea && (
        <FormField label="Area" hint="Sales grouping, for example Indochina." error={errorFor(fe, "area")}>
          <input className={inputCls} maxLength={60} value={form.area} onChange={(e) => setForm({ ...form, area: e.target.value })} />
        </FormField>
      )}
      <FormField label="Internal notes" hint="Staff only, never shown to customers." error={errorFor(fe, "internal_notes")}>
        <textarea
          className={textareaCls}
          maxLength={5000}
          value={form.internal_notes}
          onChange={(e) => setForm({ ...form, internal_notes: e.target.value })}
        />
      </FormField>
    </section>
  );
}

function stateBody(form: StateForm, withArea: boolean) {
  return {
    status: form.status,
    status_reason: form.status === "inactive" ? form.status_reason.trim() || null : null,
    internal_notes: form.internal_notes.trim() || null,
    ...(withArea ? { area: form.area.trim() || null } : {}),
  };
}

// ── Destination ─────────────────────────────────────────────────────────────

export function DestinationDrawer({ id, startEditing = false, onClose }: { id: string; startEditing?: boolean; onClose: () => void }) {
  const canWrite = useCan("catalog.admin");
  const [editing, setEditing] = useState(startEditing && canWrite);
  const status = useGeoStatus();
  const q = useQuery({ queryKey: ["geo", "destination", id], queryFn: () => geoApi.destination(id) });
  const d = q.data;

  if (editing && d) return <DestinationForm d={d} onClose={() => (startEditing ? onClose() : setEditing(false))} />;
  return (
    <>
      <Drawer
        open
        onClose={onClose}
        title={d ? d.name : "Destination"}
        width={560}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            {canWrite && d && (
              <Button
                variant={d.status === "active" ? "danger" : "secondary"}
                onClick={() => status.ask("destination", { id: d.destination_id, name: d.name }, d.status !== "active")}
              >
                {d.status === "active" ? "Deactivate" : "Activate"}
              </Button>
            )}
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
            {canWrite && d && (
              <Button variant="primary" onClick={() => setEditing(true)}>
                <Pencil size={14} /> Edit destination
              </Button>
            )}
          </div>
        }
      >
        {q.isLoading ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : q.isError || !d ? (
          <p className="text-[13px] text-danger">{apiMessage(q.error) ?? "This destination could not be loaded."}</p>
        ) : (
          <DestinationView d={d} />
        )}
      </Drawer>
      {status.dialogs}
    </>
  );
}

function destinationPaths(d: DestinationDetail): string[] {
  return d.photos.length ? d.photos.map((p) => p.path) : d.cover_photo_path ? [d.cover_photo_path] : [];
}

function DestinationView({ d }: { d: DestinationDetail }) {
  const me = useStaff();
  const point = coords(d.lat, d.lng);
  const photos = destinationPaths(d);
  return (
    <div className="flex flex-col gap-5">
      <CisFieldset title="Media" editPath={CIS_EDITOR_ROLES.has(me.role) ? CIS_PLACES_PATH : null} editLabel="Open in CIS">
        {photos.length === 0 ? (
          <p className="text-[13px] text-muted2 sm:col-span-2">No photos in CIS for this place yet.</p>
        ) : (
          photos.slice(0, 6).map((path, i) => (
            <CisPhoto key={`${path}-${i}`} path={path} alt={`${d.name} photo ${i + 1}`} className="aspect-[4/3] w-full rounded-md" />
          ))
        )}
      </CisFieldset>
      <FactGrid>
        <Fact label="Name">{d.name}</Fact>
        <Fact label="Country">{d.country ?? "—"}</Fact>
        <Fact label="Booking status">
          <span className="inline-flex flex-wrap items-center gap-2">
            <GeoStatusBadge status={d.status} />
            {d.status_reason && <span className="text-xs text-muted">{d.status_reason}</span>}
          </span>
        </Fact>
        <Fact label="Photos in CIS">{d.photo_count}</Fact>
        <Fact label="Coordinates">
          {point ? (
            <a
              href={`https://www.openstreetmap.org/?mlat=${d.lat}&mlon=${d.lng}#map=12/${d.lat}/${d.lng}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-accent-deep hover:underline"
            >
              <MapPin size={13} /> {point} <ExternalLink size={12} />
            </a>
          ) : (
            "—"
          )}
        </Fact>
        <Fact label="Booking state updated">
          {d.profile_updated_at ? (
            <>
              <DateText value={d.profile_updated_at} withTime /> {d.profile_updated_by_name ? `by ${d.profile_updated_by_name}` : ""}
            </>
          ) : (
            "Never changed (active by default)"
          )}
        </Fact>
      </FactGrid>
      <section className="grid grid-cols-3 gap-3" aria-label="Booking supply at this place">
        <SupplyCount icon={<Store size={15} />} label="Suppliers" value={d.active_suppliers} />
        <SupplyCount icon={<Building2 size={15} />} label="Hotels" value={d.active_hotels} />
        <SupplyCount icon={<Truck size={15} />} label="Services" value={d.active_services} />
      </section>
      {d.internal_notes && (
        <section>
          <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">Internal notes</span>
          <p className="mt-1 whitespace-pre-line text-[13px] text-body">{d.internal_notes}</p>
        </section>
      )}
    </div>
  );
}

function SupplyCount({ icon, label, value }: { icon: React.ReactNode; label: string; value: number }) {
  return (
    <div className="rounded-md border border-line bg-panel px-3 py-2.5">
      <span className="flex items-center gap-1.5 text-xs text-muted">
        {icon} {label}
      </span>
      <span className="bk-tabular text-[17px] font-semibold text-ink">{value}</span>
      <span className="block text-[11px] text-muted2">active in Booking</span>
    </div>
  );
}

function DestinationForm({ d, onClose }: { d: DestinationDetail; onClose: () => void }) {
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const [start] = useState<StateForm>({
    status: d.status,
    status_reason: d.status_reason ?? "",
    internal_notes: d.internal_notes ?? "",
    area: "",
  });
  const [form, setForm] = useState(start);
  const save = useMutation({
    mutationFn: () => geoApi.updateDestination(d.destination_id, stateBody(form, false)),
    onSuccess: (out) => {
      qc.setQueryData(["geo", "destination", d.destination_id], out);
      qc.invalidateQueries({ queryKey: ["geo"] });
      toast.success(out.changed?.length ? `${d.name} saved.` : "Nothing changed");
      onClose();
    },
  });
  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={`Edit ${d.name}`}
      submitLabel="Save changes"
      onSubmit={() => save.mutate()}
      busy={save.isPending}
      dirty={JSON.stringify(form) !== JSON.stringify(start)}
      error={fe.length ? null : apiMessage(save.error)}
      fieldErrors={fe}
      width={560}
    >
      <CisFieldset
        title="Destination"
        editPath={CIS_EDITOR_ROLES.has(me.role) ? CIS_PLACES_PATH : null}
        description="Name, country, coordinates and photos come from CIS places. New places are created in CIS."
      >
        <CisField label="Destination name">{d.name}</CisField>
        <CisField label="Country">{d.country}</CisField>
        <CisField label="Latitude">{d.lat != null ? d.lat.toFixed(4) : null}</CisField>
        <CisField label="Longitude">{d.lng != null ? d.lng.toFixed(4) : null}</CisField>
        <CisField label="Destination media" wide>
          {d.photo_count ? `${d.photo_count} photo${d.photo_count === 1 ? "" : "s"} in CIS` : null}
        </CisField>
        <div className="sm:col-span-2">
          <PhotoStrip paths={destinationPaths(d)} total={d.photo_count} name={d.name} />
        </div>
      </CisFieldset>
      <StateFields form={form} setForm={setForm} withArea={false} fe={fe} />
    </FormDrawer>
  );
}

// ── Country ─────────────────────────────────────────────────────────────────

/** Edit a listed country (`iso`), or add Booking state for an ISO country not listed yet (`iso` = null). */
export function CountryDrawer({ iso, listed = [], onClose }: { iso: string | null; listed?: string[]; onClose: () => void }) {
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const countries = useCountries();
  const [picked, setPicked] = useState<string>(iso ?? "");
  const detail = useQuery({
    queryKey: ["geo", "country", picked],
    queryFn: () => geoApi.country(picked),
    enabled: !!picked,
  });
  const c = detail.data;
  const [form, setForm] = useState<StateForm | null>(null);
  const start: StateForm | null = c
    ? { status: c.status, status_reason: c.status_reason ?? "", internal_notes: c.internal_notes ?? "", area: c.area ?? "" }
    : null;
  const current = form ?? start;

  const save = useMutation({
    mutationFn: () => geoApi.updateCountry(picked, stateBody(current!, true)),
    onSuccess: (out) => {
      qc.setQueryData(["geo", "country", picked], out);
      qc.invalidateQueries({ queryKey: ["geo"] });
      // Add mode: the API stores the country even when the defaults are kept (it then becomes listed).
      toast.success(!iso ? `${out.name} added.` : out.changed?.length ? `${out.name} saved.` : "Nothing changed");
      onClose();
    },
  });
  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  const options = (countries.data?.items ?? []).filter((x) => !listed.includes(x.iso2));
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={iso ? `Edit ${c?.name ?? iso}` : "Add country"}
      submitLabel={iso ? "Save changes" : "Save country"}
      onSubmit={() => current && picked && save.mutate()}
      busy={save.isPending}
      dirty={!!form && JSON.stringify(form) !== JSON.stringify(start)}
      error={!picked && save.isError ? "Pick a country first." : fe.length ? null : apiMessage(save.error)}
      fieldErrors={fe}
      width={560}
    >
      {!iso && (
        <FormField label="Country name" required hint="Countries with a CIS place or tour are listed already; add Booking state for another ISO country here.">
          <select
            className={inputCls}
            value={picked}
            onChange={(e) => {
              setPicked(e.target.value);
              setForm(null);
            }}
          >
            <option value="">Select a country</option>
            {options.map((x) => (
              <option key={x.iso2} value={x.iso2}>
                {x.name}
              </option>
            ))}
          </select>
        </FormField>
      )}
      {picked && (
        <CisFieldset
          title="Country"
          editPath={CIS_EDITOR_ROLES.has(me.role) ? CIS_PLACES_PATH : null}
          description="Derived from CIS places and tours. Country descriptions, FAQs and festivals have no CIS source yet (UI spec Q18), so Booking does not author them."
        >
          <CisField label="Country">{c ? `${c.name} (${c.country_code})` : detail.isLoading ? "…" : null}</CisField>
          <CisField label="Destinations">{c ? `${c.destinations} places` : null}</CisField>
          <CisField label="Tours">{c ? `${c.tours} CIS tours` : null}</CisField>
          <CisField label="Images">{c ? `${c.photos} CIS photo${c.photos === 1 ? "" : "s"}` : null}</CisField>
          {c && c.photo_items.length > 0 && (
            <div className="sm:col-span-2">
              <PhotoStrip paths={c.photo_items.map((p) => p.path)} total={c.photos} name={c.name} />
            </div>
          )}
        </CisFieldset>
      )}
      {current && <StateFields form={current} setForm={setForm} withArea fe={fe} />}
    </FormDrawer>
  );
}
