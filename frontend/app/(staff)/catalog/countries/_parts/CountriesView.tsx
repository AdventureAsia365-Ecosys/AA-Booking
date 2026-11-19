"use client";
// app/(staff)/catalog/countries/_parts/CountriesView.tsx — Catalog → Countries (AA-996; UI spec §3 / §7; Figma COUNTRY
// MANAGAMENT, -1, ADD / EDIT / DELETE / UNPUBLISHED COUNTRY, module 06). The countries the CIS catalog has places or
// tours in (plus any country Booking keeps state for), as the Figma card grid: CIS cover photo with an image-count
// chip (a neutral tile with the ISO code when CIS has no photo there), name, ISO code, Booking status, CIS counts
// (places, tours, photos) and active suppliers. Filters (search, status, area) run on the server; the list
// is short, so it is not paged. Row menu: View details, Edit country, Deactivate / Activate (admin). "Add country"
// stores Booking state for an ISO country not listed yet: countries and their content are never created in Booking.

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Eye, Globe, ImageIcon, MapPin, Pencil, Plus, Power, Store } from "lucide-react";
import { geoApi, type CountryItem, type GeoStatus } from "@/lib/api/geo";
import { Button, CisPhoto, EmptyState, ErrorState, ExportCsvButton, FilterBar, FromCisTag, KpiTile, ManagedInCisLink, PageFrame, Section, SkeletonBar, useCan, useUrlFilters, RowMenu } from "@/ui";
import { CIS_PLACES_PATH, CountryDrawer, GeoStatusBadge } from "../../_parts/GeoDrawers";
import { useGeoStatus } from "../../_parts/useGeoStatus";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./CountriesView.messages";

export function CountriesView() {
  const t = useT(MSG);
  const tx = useTx();
  const router = useRouter();
  const { get, setMany } = useUrlFilters();
  const canWrite = useCan("catalog.admin");
  const status = useGeoStatus();
  const [adding, setAdding] = useState(false);
  const query = {
    q: get("q") || undefined,
    status: (get("status") || undefined) as GeoStatus | undefined,
    area: get("area") || undefined,
  };
  const list = useQuery({ queryKey: ["geo", "countries", query], queryFn: () => geoApi.countries(query) });
  const all = useQuery({ queryKey: ["geo", "countries", {}], queryFn: () => geoApi.countries() });
  const items = list.data?.items ?? [];
  const everything = all.data?.items ?? [];
  const editing = get("edit");
  const filtered = !!(query.q || query.status || query.area);

  const active = everything.filter((c) => c.status === "active").length;
  return (
    <PageFrame
      title={t("Countries")}
      description={t("Countries of the CIS catalog and whether Booking sells trips there. Places, photos and country content come from CIS.")}
      actions={
        <>
          <ManagedInCisLink path={CIS_PLACES_PATH} label={t("Open CIS places")} />
          {canWrite && (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus size={15} /> {t("Add country")}
            </Button>
          )}
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile label={t("Countries")} value={all.data ? everything.length : undefined} icon={<Globe size={16} />} hint={<FromCisTag />} />
        <KpiTile label={t("Active in Booking")} value={all.data ? active : undefined} icon={<CheckCircle2 size={16} />} />
        <KpiTile label={t("Inactive")} value={all.data ? everything.length - active : undefined} icon={<Power size={16} />} />
        <KpiTile
          label={t("CIS places without a country")}
          value={all.data ? all.data.unmapped.reduce((n, u) => n + u.destinations, 0) : undefined}
          icon={<MapPin size={16} />}
          hint={all.data?.unmapped.length ? all.data.unmapped.map((u) => u.name).join(", ") : undefined}
          hintTone="warning"
        />
      </div>
      <Section>
        <div className="mb-4">
          <FilterBar
            search={{ placeholder: t("Search countries") }}
            selects={[
              {
                key: "status",
                label: t("Status"),
                allLabel: t("All statuses"),
                options: [
                  { value: "active", label: t("Active") },
                  { value: "inactive", label: t("Inactive") },
                ],
              },
              { key: "area", label: t("Area"), allLabel: t("All areas"), options: (all.data?.areas ?? []).map((a) => ({ value: a, label: a })) },
            ]}
            tools={<ExportCsvButton rows={items} columns={COUNTRY_CSV} filename="countries.csv" disabled={list.isLoading} />}
          />
        </div>
        {list.isLoading ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-busy="true">
            {[0, 1, 2].map((i) => (
              <SkeletonBar key={i} h={290} r={12} />
            ))}
          </div>
        ) : list.isError ? (
          <ErrorState message={tx((list.error as Error).message)} onRetry={() => list.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            title={filtered ? t("No countries match") : t("No countries yet")}
            description={filtered ? t("Change the search or the filters.") : t("Countries appear as soon as CIS publishes a place or a tour there.")}
          />
        ) : (
          <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {items.map((c) => (
              <CountryCard
                key={c.country_code}
                c={c}
                menu={
                  <RowMenu
                    label={t("Actions for {0}", c.name)}
                    items={[
                      { label: t("View details"), icon: <Eye size={14} />, onSelect: () => router.push(`/catalog/countries/${c.country_code}`) },
                      ...(canWrite && c.booking_reference
                        ? [
                            { label: t("Edit country"), icon: <Pencil size={14} />, onSelect: () => setMany({ edit: c.country_code }) },
                            c.status === "active"
                              ? { label: t("Deactivate"), icon: <Power size={14} />, danger: true, divider: true, onSelect: () => status.ask("country", { id: c.country_code, name: c.name }, false) }
                              : { label: t("Activate"), icon: <CheckCircle2 size={14} />, divider: true, onSelect: () => status.ask("country", { id: c.country_code, name: c.name }, true) },
                          ]
                        : []),
                    ]}
                  />
                }
              />
            ))}
          </ul>
        )}
      </Section>
      {canWrite && (adding || editing) && (
        <CountryDrawer
          key={editing || "new"}
          iso={adding ? null : editing}
          listed={everything.map((c) => c.country_code)}
          onClose={() => {
            setAdding(false);
            setMany({ edit: null });
          }}
        />
      )}
      {status.dialogs}
    </PageFrame>
  );
}

function CountryCard({ c, menu }: { c: CountryItem; menu: React.ReactNode }) {
  const t = useT(MSG);
  return (
    <li className="flex min-w-0 flex-col rounded-lg border border-line bg-card">
      {/* The cover repeats the title link for the pointer; keyboard and screen readers use the name link below. */}
      <Link href={`/catalog/countries/${c.country_code}`} className="relative block overflow-hidden rounded-t-lg" tabIndex={-1} aria-hidden="true">
        <CisPhoto path={c.cover_photo_path} alt="" fallbackLabel={c.country_code} className="aspect-[16/7] w-full" />
        <span className="bk-tabular absolute bottom-2 left-2 inline-flex items-center gap-1 rounded-full bg-black/55 px-2 py-0.5 text-[11.5px] font-medium text-white">
          <ImageIcon size={12} /> {c.photos === 1 ? t("1 image") : t("{0} images", c.photos)}
        </span>
      </Link>
      <div className="flex min-w-0 flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-2">
          <Link href={`/catalog/countries/${c.country_code}`} className="min-w-0 hover:underline">
            <span className="block truncate text-[15px] font-semibold text-ink">{c.name}</span>
            <span className="font-mono text-xs text-muted">{c.country_code}</span>
          </Link>
          <span className="flex shrink-0 items-center gap-2">
            <GeoStatusBadge status={c.status} />
            {menu}
          </span>
        </div>
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[12.5px]">
          <Count icon={<MapPin size={13} />} label={t("Destinations")} value={c.destinations} />
          <Count icon={<Globe size={13} />} label={t("Tours")} value={c.tours} />
          <Count icon={<ImageIcon size={13} />} label={t("Images")} value={c.photos} />
          <Count icon={<Store size={13} />} label={t("Suppliers")} value={c.active_suppliers} />
        </dl>
        <p className="truncate text-xs text-muted">
          {c.area ? t("Area: {0}", c.area) : t("No area")}
          {c.status_reason ? ` · ${c.status_reason}` : ""}
        </p>
      </div>
    </li>
  );
}

function Count({ icon, label, value }: { icon: React.ReactNode; label: string; value: number }) {
  const tx = useTx();
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="flex items-center gap-1.5 text-muted">
        {icon} {tx(label)}
      </dt>
      <dd className="bk-tabular font-medium text-ink">{value}</dd>
    </div>
  );
}

/** AA-996 (audit slice 1): the Countries CSV, the cards' facts (the filtered list, as shown). */
const COUNTRY_CSV: { header: string; value: (c: CountryItem) => unknown }[] = [
  { header: "Country", value: (c) => c.name },
  { header: "ISO code", value: (c) => c.country_code },
  { header: "Status", value: (c) => c.status },
  { header: "Area", value: (c) => c.area ?? "" },
  { header: "Places (CIS)", value: (c) => c.destinations },
  { header: "Tours (CIS)", value: (c) => c.tours },
  { header: "Photos (CIS)", value: (c) => c.photos },
  { header: "Active suppliers", value: (c) => c.active_suppliers },
  { header: "In Booking", value: (c) => (c.booking_reference ? "yes" : "no") },
];
