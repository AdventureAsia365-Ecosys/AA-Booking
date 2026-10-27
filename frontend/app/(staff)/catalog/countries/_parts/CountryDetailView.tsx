"use client";
// app/(staff)/catalog/countries/_parts/CountryDetailView.tsx — one country (AA-996; UI spec §3; Figma COUNTRY DETAILS,
// -1, module 06; the drawer variant is folded into this page). Header: name, ISO code, Booking status, Deactivate /
// Activate and Edit country (admin). Tabs: Overview (the CIS counts, Booking state: area, reason, internal notes),
// Destinations (its CIS places with their own Booking status; a row opens the destination drawer) and Tours (CIS tours
// naming the country, linking to the trip plan). Content is read-only "From CIS"; descriptions, FAQs and festivals have
// no CIS source yet (UI spec Q18), so they are not shown.

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, Globe, Hash, ImageIcon, MapPin, Pencil, Power, Store } from "lucide-react";
import { geoApi, type CountryTour, type DestinationItem } from "@/lib/api/geo";
import { Button, DataTable, DateText, FromCisTag, KpiTile, ManagedInCisLink, Section, useCan, useStaff, useUrlFilters } from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { DetailHeader, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { CIS_EDITOR_ROLES, SellableBadge } from "@/app/(staff)/catalog/trip-plans/_parts/shared";
import { CIS_PLACES_PATH, CountryDrawer, DestinationDrawer, GeoStatusBadge, coords } from "../../_parts/GeoDrawers";
import { useGeoStatus } from "../../_parts/useGeoStatus";

const DEST_COLUMNS: ColumnDef<DestinationItem, unknown>[] = [
  { id: "name", header: "Destination", accessorKey: "name", cell: ({ row }) => <span className="text-[13.5px] font-medium text-ink">{row.original.name}</span> },
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
      <Link href={`/catalog/trip-plans/${encodeURIComponent(row.original.source_id)}`} className="text-[13.5px] font-medium text-ink hover:underline">
        {row.original.title ?? row.original.source_id}
      </Link>
    ),
  },
  { id: "sku", header: "SKU", accessorFn: (r) => r.sku ?? "", cell: ({ row }) => <span className="font-mono text-xs text-muted">{row.original.sku ?? "—"}</span> },
  { id: "days", header: "Days", accessorFn: (r) => r.duration_days ?? 0, cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.duration_days ?? "—"}</span> },
  { id: "sellable", header: "Status", accessorFn: (r) => (r.sellable ? 1 : 0), cell: ({ row }) => <SellableBadge sellable={row.original.sellable} /> },
];

export function CountryDetailView({ iso }: { iso: string }) {
  const me = useStaff();
  const canWrite = useCan("catalog.admin");
  const status = useGeoStatus();
  const { get, setMany } = useUrlFilters();
  const q = useQuery({ queryKey: ["geo", "country", iso.toUpperCase()], queryFn: () => geoApi.country(iso) });
  const c = q.data;
  const tabs = [
    { key: "overview", label: "Overview" },
    // The true counts (the item lists below are capped by the API: first 200 places, first 50 tours).
    { key: "destinations", label: "Destinations", count: c?.destinations },
    { key: "tours", label: "Tours", count: c?.tours },
  ];
  const [tab, setTab] = useDetailTab(tabs);
  const place = get("place");

  if (q.isLoading) return <PageSkeleton />;
  if (q.isError || !c) return <PageError error={q.error} onRetry={() => q.refetch()} what="this country" />;

  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <DetailHeader
        back={{ href: "/catalog/countries", label: "Countries" }}
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
            {CIS_EDITOR_ROLES.has(me.role) && <ManagedInCisLink path={CIS_PLACES_PATH} label="Open in CIS" />}
            {canWrite && (
              <Button
                variant={c.status === "active" ? "danger" : "secondary"}
                onClick={() => status.ask("country", { id: c.country_code, name: c.name }, c.status !== "active")}
              >
                {c.status === "active" ? <Power size={15} /> : <CheckCircle2 size={15} />} {c.status === "active" ? "Deactivate" : "Activate"}
              </Button>
            )}
            {canWrite && (
              <Button variant="primary" onClick={() => setMany({ edit: "1" })}>
                <Pencil size={15} /> Edit country
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
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <KpiTile label="Destinations" value={c.destinations} icon={<MapPin size={16} />} hint={<FromCisTag />} />
              <KpiTile label="Tours" value={c.tours} icon={<Globe size={16} />} hint={<FromCisTag />} />
              <KpiTile label="Images" value={c.photos} icon={<ImageIcon size={16} />} hint="photos of its places" />
              <KpiTile label="Active suppliers" value={c.active_suppliers} icon={<Store size={16} />} hint="in Booking" />
            </div>
            <Section title="Booking state" description="Owned by Booking; an admin changes it.">
              <FactGrid>
                <Fact label="Status">
                  <GeoStatusBadge status={c.status} />
                </Fact>
                <Fact label="Area">{c.area ?? "—"}</Fact>
                <Fact label="Reason">{c.status_reason ?? "—"}</Fact>
                <Fact label="Updated">
                  {c.profile_updated_at ? (
                    <>
                      <DateText value={c.profile_updated_at} withTime /> {c.profile_updated_by_name ? `by ${c.profile_updated_by_name}` : ""}
                    </>
                  ) : (
                    "Never changed (active by default)"
                  )}
                </Fact>
                <Fact label="Internal notes" wide>
                  <span className="whitespace-pre-line">{c.internal_notes ?? "—"}</span>
                </Fact>
              </FactGrid>
            </Section>
            <Section title="Country information" actions={<FromCisTag />}>
              <p className="text-[13px] text-muted">
                CIS has no country description, FAQ or festival content yet (UI spec Q18). When CIS publishes it, it shows here
                read-only; Booking does not write it.
              </p>
            </Section>
          </>
        )}
        {tab === "destinations" && (
          <Section padded={false} className="p-4" title="Destinations" actions={<FromCisTag />}>
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
              emptyTitle="No CIS places in this country"
              emptyDescription="Places appear here as soon as CIS publishes them."
              onRowClick={(r) => setMany({ place: r.destination_id })}
              tableMinWidth={620}
            />
          </Section>
        )}
        {tab === "tours" && (
          <Section padded={false} className="p-4" title="Tours" actions={<FromCisTag />}>
            <FirstOf
              shown={c.tour_items.length}
              total={c.tours}
              href={`/catalog/trip-plans?country=${encodeURIComponent(c.name)}`}
              what="trip plans"
            />
            <DataTable<CountryTour>
              tableId="country-tours"
              data={c.tour_items}
              columns={TOUR_COLUMNS}
              getRowId={(r) => r.source_id}
              pageSizeOptions={[25, 50]}
              emptyTitle="No CIS tours name this country"
              tableMinWidth={620}
            />
          </Section>
        )}
      </div>
      {canWrite && get("edit") === "1" && <CountryDrawer iso={c.country_code} onClose={() => setMany({ edit: null })} />}
      {place && <DestinationDrawer key={place} id={place} onClose={() => setMany({ place: null })} />}
      {status.dialogs}
    </main>
  );
}

/** The API returns the first N items of a country's places / tours; when there are more, say so and link to the
 * full list (server-paged and filtered by this country). */
function FirstOf({ shown, total, href, what }: { shown: number; total: number; href: string; what: string }) {
  if (shown >= total) return null;
  return (
    <p className="mb-3 text-[13px] text-muted" data-testid="country-first-of">
      Showing the first {shown} of {total}.{" "}
      <Link href={href} className="font-medium text-accent-deep hover:underline">
        See all {total} {what} →
      </Link>
    </p>
  );
}
