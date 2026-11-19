"use client";
// app/(staff)/catalog/countries/_parts/CountryDetailView.tsx — one country (AA-996; UI spec §3; Figma COUNTRY DETAILS,
// -1, module 06; the drawer variant is folded into this page). Header: name, ISO code, Booking status, Deactivate /
// Activate and Edit country (admin). Tabs: Overview (CIS cover hero, the CIS counts, the "Photos · From CIS" gallery
// with a lightbox, Booking state: area, reason, internal notes),
// Destinations (its CIS places with their own Booking status; a row opens the destination drawer) and Tours (CIS tours
// naming the country, linking to the trip plan). Content is read-only "From CIS"; descriptions, FAQs and festivals have
// no CIS source yet (UI spec Q18), so they are not shown.

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, Globe, Hash, ImageIcon, MapPin, Pencil, Power, Store } from "lucide-react";
import { geoApi, type CountryTour, type DestinationItem } from "@/lib/api/geo";
import { PageFrame, Button, CisPhoto, DataTable, DateText, FromCisTag, KpiTile, ManagedInCisLink, Section, useCan, useStaff, useUrlFilters } from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { DetailHeader, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { CIS_EDITOR_ROLES, SellableBadge } from "@/app/(staff)/catalog/trip-plans/_parts/shared";
import { CIS_PLACES_PATH, CountryDrawer, DestinationDrawer, GeoStatusBadge, coords } from "../../_parts/GeoDrawers";
import { useGeoStatus } from "../../_parts/useGeoStatus";
import { CountryGallery, CountryHero } from "./CountryPhotos";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./CountryDetailView.messages";

const DEST_COLUMNS: ColumnDef<DestinationItem, unknown>[] = [
  {
    id: "name",
    header: "Destination",
    accessorKey: "name",
    cell: ({ row }) => (
      <span className="flex min-w-0 items-center gap-3">
        <CisPhoto path={row.original.cover_photo_path} alt="" className="h-9 w-12 shrink-0 rounded-md" />
        <span className="truncate text-[13.5px] font-medium text-ink">{row.original.name}</span>
      </span>
    ),
  },
  {
    id: "coords",
    header: "Coordinates",
    enableSorting: false,
    accessorFn: (r) => coords(r.lat, r.lng) ?? "",
    cell: ({ row }) => <span className="bk-tabular whitespace-nowrap text-[12.5px] text-muted">{coords(row.original.lat, row.original.lng) ?? "—"}</span>,
  },
  { id: "photos", header: "Media", accessorKey: "photo_count", cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.photo_count} files</span> },
  { id: "status", header: "Booking status", accessorKey: "status", cell: ({ row }) => <GeoStatusBadge status={row.original.status} /> },
];

const TOUR_COLUMNS: ColumnDef<CountryTour, unknown>[] = [
  {
    id: "title",
    header: "Tour",
    accessorFn: (r) => r.title ?? "",
    cell: ({ row }) => (
      <span className="flex min-w-0 items-center gap-3">
        <CisPhoto path={row.original.cover_photo_path} alt="" className="h-9 w-12 shrink-0 rounded-md" />
        <Link href={`/catalog/trip-plans/${encodeURIComponent(row.original.source_id)}`} className="truncate text-[13.5px] font-medium text-ink hover:underline">
          {row.original.title ?? row.original.source_id}
        </Link>
      </span>
    ),
  },
  { id: "sku", header: "SKU", accessorFn: (r) => r.sku ?? "", cell: ({ row }) => <span className="font-mono text-xs text-muted">{row.original.sku ?? "—"}</span> },
  { id: "days", header: "Days", accessorFn: (r) => r.duration_days ?? 0, cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.duration_days ?? "—"}</span> },
  { id: "sellable", header: "Status", accessorFn: (r) => (r.sellable ? 1 : 0), cell: ({ row }) => <SellableBadge sellable={row.original.sellable} /> },
];

export function CountryDetailView({ iso }: { iso: string }) {
  const t = useT(MSG);
  const me = useStaff();
  const canWrite = useCan("catalog.admin");
  const status = useGeoStatus();
  const { get, setMany } = useUrlFilters();
  const q = useQuery({ queryKey: ["geo", "country", iso.toUpperCase()], queryFn: () => geoApi.country(iso) });
  const c = q.data;
  const tabs = [
    { key: "overview", label: t("Overview") },
    // The true counts (the item lists below are capped by the API: first 200 places, first 50 tours).
    { key: "destinations", label: t("Destinations"), count: c?.destinations },
    { key: "tours", label: t("Tours"), count: c?.tours },
  ];
  const [tab, setTab] = useDetailTab(tabs);
  const place = get("place");

  if (q.isLoading) return <PageSkeleton />;
  if (q.isError || !c) return <PageError error={q.error} onRetry={() => q.refetch()} what={t("this country")} />;

  return (
    <PageFrame bleed>
      <DetailHeader
        back={{ href: "/catalog/countries", label: t("Countries") }}
        name={c.name}
        badges={
          <>
            <GeoStatusBadge status={c.status} />
            <FromCisTag />
          </>
        }
        meta={
          <>
            <MetaItem icon={<Hash size={13} />}>{c.country_code}</MetaItem>
            {c.area && <MetaItem icon={<Globe size={13} />}>{c.area}</MetaItem>}
          </>
        }
        actions={
          <>
            {CIS_EDITOR_ROLES.has(me.role) && <ManagedInCisLink path={CIS_PLACES_PATH} label={t("Open in CIS")} />}
            {canWrite && c.booking_reference && (
              <Button
                variant={c.status === "active" ? "danger" : "secondary"}
                onClick={() => status.ask("country", { id: c.country_code, name: c.name }, c.status !== "active")}
              >
                {c.status === "active" ? <Power size={15} /> : <CheckCircle2 size={15} />} {c.status === "active" ? t("Deactivate") : t("Activate")}
              </Button>
            )}
            {canWrite && c.booking_reference && (
              <Button variant="primary" onClick={() => setMany({ edit: "1" })}>
                <Pencil size={15} /> {t("Edit country")}
              </Button>
            )}
          </>
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      <div className="mx-auto flex max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-8">
        {tab === "overview" && (
          <>
            <CountryHero c={c} />
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <KpiTile label={t("Destinations")} value={c.destinations} icon={<MapPin size={16} />} hint={<FromCisTag />} />
              <KpiTile label={t("Tours")} value={c.tours} icon={<Globe size={16} />} hint={<FromCisTag />} />
              <KpiTile label={t("Images")} value={c.photos} icon={<ImageIcon size={16} />} hint={t("CIS photos of its places and tours")} />
              <KpiTile label={t("Active suppliers")} value={c.active_suppliers} icon={<Store size={16} />} hint={t("in Booking")} />
            </div>
            <CountryGallery c={c} canOpenCis={CIS_EDITOR_ROLES.has(me.role)} onPlace={(id) => setMany({ place: id })} />
            <Section title={t("Booking state")} description={t("Owned by Booking; an admin changes it.")}>
              <FactGrid>
                <Fact label={t("Status")}>
                  <GeoStatusBadge status={c.status} />
                </Fact>
                <Fact label={t("Area")}>{c.area ?? "—"}</Fact>
                <Fact label={t("Reason")}>{c.status_reason ?? "—"}</Fact>
                <Fact label={t("Updated")}>
                  {c.profile_updated_at ? (
                    <>
                      <DateText value={c.profile_updated_at} withTime /> {c.profile_updated_by_name ? t("by {0}", c.profile_updated_by_name) : ""}
                    </>
                  ) : (
                    t("Never changed (active by default)")
                  )}
                </Fact>
                <Fact label={t("Internal notes")} wide>
                  <span className="whitespace-pre-line">{c.internal_notes ?? "—"}</span>
                </Fact>
              </FactGrid>
            </Section>
            <Section title={t("Country information")} actions={<FromCisTag />}>
              <p className="text-[13px] text-muted">
                {t("CIS has no country description, FAQ or festival content yet. When CIS publishes it, it shows here read-only; Booking does not write it.")}
              </p>
            </Section>
          </>
        )}
        {tab === "destinations" && (
          <Section padded={false} className="p-4" title={t("Destinations")} actions={<FromCisTag />}>
            <FirstOf
              shown={c.destination_items.length}
              total={c.destinations}
              href={`/catalog/destinations?country=${encodeURIComponent(c.country_code)}`}
              what="destinations"
            />
            <DataTable<DestinationItem>
              tableId="country-destinations"
              data={c.destination_items}
              columns={DEST_COLUMNS}
              getRowId={(r) => r.destination_id}
              pageSizeOptions={[25, 50, 100]}
              emptyTitle={t("No CIS places in this country")}
              emptyDescription={t("Places appear here as soon as CIS publishes them.")}
              onRowClick={(r) => setMany({ place: r.destination_id })}
              tableMinWidth={620}
            />
          </Section>
        )}
        {tab === "tours" && (
          <Section padded={false} className="p-4" title={t("Tours")} actions={<FromCisTag />}>
            <FirstOf
              shown={c.tour_items.length}
              total={c.tours}
              href={`/catalog/trip-plans?country=${encodeURIComponent(c.name)}`}
              what={t("trip plans")}
            />
            <DataTable<CountryTour>
              tableId="country-tours"
              data={c.tour_items}
              columns={TOUR_COLUMNS}
              getRowId={(r) => r.source_id}
              pageSizeOptions={[25, 50]}
              emptyTitle={t("No CIS tours name this country")}
              tableMinWidth={620}
            />
          </Section>
        )}
      </div>
      {canWrite && get("edit") === "1" && <CountryDrawer iso={c.country_code} onClose={() => setMany({ edit: null })} />}
      {place && <DestinationDrawer key={place} id={place} onClose={() => setMany({ place: null })} />}
      {status.dialogs}
    </PageFrame>
  );
}

/** The API returns the first N items of a country's places / tours; when there are more, say so and link to the
 * full list (server-paged and filtered by this country). */
function FirstOf({ shown, total, href, what }: { shown: number; total: number; href: string; what: string }) {
  const t = useT(MSG);
  if (shown >= total) return null;
  return (
    <p className="mb-3 text-[13px] text-muted" data-testid="country-first-of">
      {t("Showing the first {0} of {1}.", shown, total)}
      <Link href={href} className="font-medium text-accent-deep hover:underline">
        {t("See all {0} {1} →", total, what)}
      </Link>
    </p>
  );
}
