"use client";
// app/(staff)/_parts/supply.tsx — labels, pickers, the rate table and error copy shared by the hotel, service and
// supplier-rate pages (AA-823, bk-010). Facts only: Booking keeps the supply data; customer-facing text and photos are
// CIS content (ADR 0004a, Q17), so no page here edits a description or a photo.

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Star } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { suppliersApi, type SupplierCategory } from "@/lib/api/suppliers";
import type { RateUnit, ServiceType, ServiceUnit, SupplierRate } from "@/lib/api/supply";
import { Select, Code, DataTable, DateText, Pill } from "@/ui";
import { inputCls } from "./FormField";

export const SERVICE_TYPE_LABEL: Record<ServiceType, string> = {
  activity: "Activity",
  transfer: "Transfer",
  guide: "Guide",
  entrance: "Entrance",
  meal: "Meal",
};

/** The supplier category a hotel or a service type needs (bk-008 categories). */
export const CATEGORY_FOR: Record<ServiceType | "hotel", SupplierCategory> = {
  hotel: "hotel",
  activity: "activity",
  transfer: "transport",
  guide: "guide",
  entrance: "entrance",
  meal: "meal",
};

export const UNIT_LABEL: Record<RateUnit, string> = {
  room_night: "per room night",
  person: "per person",
  group: "per group",
  vehicle: "per vehicle",
  day: "per day",
};

export const SERVICE_UNIT_OPTIONS: { value: ServiceUnit; label: string }[] = [
  { value: "person", label: "Per person" },
  { value: "group", label: "Per group" },
  { value: "vehicle", label: "Per vehicle" },
  { value: "day", label: "Per day" },
];

export function Stars({ value }: { value: number | null }) {
  if (!value) return <span className="text-[13px] text-muted">—</span>;
  return (
    <span className="inline-flex items-center gap-0.5 text-warning" aria-label={`${value} stars`} title={`${value} stars`}>
      {Array.from({ length: value }, (_, i) => (
        <Star key={i} size={13} fill="currentColor" strokeWidth={0} />
      ))}
    </span>
  );
}

/** "a, b, c" ↔ ["a", "b", "c"] for amenity / inclusion / bed type inputs. */
export const splitList = (text: string): string[] =>
  text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
export const joinList = (items: string[] | null | undefined): string => (items ?? []).join(", ");

/** Active suppliers that carry a category, as a select (the API checks the same on save). */
export function SupplierSelect({
  value,
  category,
  onChange,
  disabled,
}: {
  value: string;
  category: SupplierCategory;
  onChange: (id: string) => void;
  disabled?: boolean;
}) {
  const list = useQuery({
    queryKey: ["suppliers", "list", { status: "active", category }, 100, 0],
    queryFn: () => suppliersApi.list({ status: "active", category, limit: 100 }),
    staleTime: 60_000,
  });
  const items = list.data?.items ?? [];
  return (
    <Select className={inputCls} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled || list.isLoading}>
      <option value="">{list.isLoading ? "Loading suppliers…" : items.length ? "Pick a supplier" : "No active supplier with this category"}</option>
      {items.map((s) => (
        <option key={s.id} value={s.id}>
          {s.company_name} ({s.code})
        </option>
      ))}
    </Select>
  );
}

export function rateTarget(r: SupplierRate): string {
  return r.hotel_room_type_id ? `${r.hotel_name} · ${r.room_type_name}` : (r.service_title ?? "");
}

export function amountText(amount: string, currency: string): string {
  const n = Number(amount);
  const text = Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: 2 }) : amount;
  return `${text} ${currency}`;
}

function paxText(r: SupplierRate): string {
  if (r.pax_min == null && r.pax_max == null) return "Any";
  if (r.pax_min != null && r.pax_max != null) return r.pax_min === r.pax_max ? `${r.pax_min}` : `${r.pax_min}–${r.pax_max}`;
  return r.pax_min != null ? `${r.pax_min}+` : `up to ${r.pax_max}`;
}

const RATE_STATE: Record<string, { label: string; tone: "success" | "neutral" | "info" | "warning" }> = {
  draft: { label: "Draft", tone: "neutral" },
  published: { label: "Published", tone: "info" },
  expired: { label: "Expired", tone: "neutral" },
};

export function RateState({ rate }: { rate: SupplierRate }) {
  if (rate.in_force) return <Pill tone="success">In force</Pill>;
  const meta = RATE_STATE[rate.contract_status] ?? { label: rate.contract_status, tone: "neutral" as const };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}

/** Net rates as a table: what (optional), season + validity, meal plan + pax band, net, state and an optional menu.
 *  AA-910: fits the 1092 px detail frame at 1440 px (season + validity and meal plan + pax band share a column, text
 *  cells have a desktop max width), and the row menu column stays pinned to the right when the table has to scroll
 *  inside its frame. */
export function RatesTable({
  rates,
  loading,
  error,
  onRetry,
  showTarget = true,
  showContract = false,
  compact = false,
  menu,
  emptyTitle,
  emptyDescription,
  tableId,
}: {
  rates: SupplierRate[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  showTarget?: boolean;
  showContract?: boolean;
  /** Narrow frame (the 880 px hotel drawer, which already names the hotel): no code / hotel line under the room type,
   *  narrower text cells, and the validity dates may wrap. */
  compact?: boolean;
  menu?: (r: SupplierRate) => React.ReactNode;
  emptyTitle: string;
  emptyDescription: string;
  tableId: string;
}) {
  const columns: ColumnDef<SupplierRate, unknown>[] = [
    ...(showTarget
      ? [
          {
            id: "target",
            header: "Room type / service",
            accessorFn: (r: SupplierRate) => rateTarget(r),
            cell: ({ row }: { row: { original: SupplierRate } }) => {
              const r = row.original;
              const what = r.hotel_room_type_id ? r.room_type_name : r.service_title;
              return (
                <span className={`flex min-w-0 flex-col ${compact ? "max-w-[130px]" : "max-w-[240px]"}`} title={rateTarget(r)}>
                  <span className="truncate text-[13.5px] font-medium text-ink">{what ?? "—"}</span>
                  {!compact && (
                    <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
                      <Code>{r.hotel_code ?? r.service_code}</Code>
                      {r.hotel_room_type_id && r.hotel_name && <span className="truncate">· {r.hotel_name}</span>}
                    </span>
                  )}
                </span>
              );
            },
          } as ColumnDef<SupplierRate, unknown>,
        ]
      : []),
    ...(showContract
      ? [
          {
            id: "contract",
            header: "Contract",
            accessorKey: "contract_title",
            cell: ({ row }: { row: { original: SupplierRate } }) => (
              <span className={`block break-words text-[13px] text-body md:truncate ${compact ? "md:max-w-[90px]" : "md:max-w-[140px]"}`} title={row.original.contract_title}>
                {row.original.contract_title}
              </span>
            ),
          } as ColumnDef<SupplierRate, unknown>,
        ]
      : []),
    {
      id: "validity",
      header: "Season / valid",
      accessorKey: "valid_from",
      cell: ({ row }) => (
        <span className={`flex min-w-0 flex-col text-[13px] text-body ${compact ? "md:max-w-[120px]" : "md:max-w-[200px]"}`}>
          <span className="md:truncate" title={row.original.season ?? undefined}>
            {row.original.season ?? "—"}
          </span>
          <span className={`text-xs text-muted ${compact ? "" : "whitespace-nowrap"}`}>
            <DateText value={row.original.valid_from} className="whitespace-nowrap" /> –{" "}
            {row.original.valid_to ? (
              <DateText value={row.original.valid_to} className="whitespace-nowrap" />
            ) : (
              <span className="whitespace-nowrap">open-ended</span>
            )}
          </span>
        </span>
      ),
    },
    {
      id: "meal",
      header: "Meal / pax",
      accessorFn: (r) => r.meal_plan ?? "",
      cell: ({ row }) => (
        <span className="flex flex-col whitespace-nowrap text-[13px] text-body">
          {row.original.meal_plan ?? "—"}
          <span className="text-xs text-muted">{paxText(row.original)} pax</span>
        </span>
      ),
    },
    {
      id: "net",
      header: "Net",
      accessorFn: (r) => Number(r.net_amount),
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span className="bk-tabular whitespace-nowrap text-[13px] font-medium text-ink">
            {amountText(row.original.net_amount, row.original.currency)}
          </span>
          <span className="text-xs text-muted">{UNIT_LABEL[row.original.unit]}</span>
        </span>
      ),
    },
    { id: "state", header: "Status", enableSorting: false, cell: ({ row }) => <RateState rate={row.original} /> },
    ...(menu
      ? [{ id: "actions", header: "Actions", enableSorting: false, cell: ({ row }) => menu(row.original) } as ColumnDef<SupplierRate, unknown>]
      : []),
  ];
  return (
    <div className={menu ? "bk-dt-sticky-end" : undefined}>
      <DataTable<SupplierRate>
        searchable={false}
        tableId={tableId}
        data={rates}
        columns={columns}
        getRowId={(r) => r.id}
        loading={loading}
        error={error ?? null}
        onRetry={onRetry}
        pageSizeOptions={[25, 50, 100]}
        emptyTitle={emptyTitle}
        emptyDescription={emptyDescription}
        tableMinWidth={showTarget ? 760 : 600}
      />
    </div>
  );
}

const FRIENDLY: Record<string, string> = {
  hotel_exists: "This supplier already has a hotel with this name at this place.",
  service_exists: "This supplier already has a service of this type with this title at this place.",
  room_type_exists: "This hotel already has a room type with this name.",
  supplier_inactive: "The supplier is inactive. Activate it first.",
  supplier_category_missing: "The supplier does not have the matching category. Add it on the supplier first.",
  supplier_not_found: "Pick a supplier from the list.",
  hotel_inactive: "Activate the hotel first.",
  service_has_rates: "This service already has rates in its unit. Add a new service for another unit.",
  geo_pair: "Give both latitude and longitude, or neither.",
  unknown_destination: "That place is not in the CIS catalog. Pick it again from the list.",
  destination_country_mismatch: "The place is in another country than the one selected. Change one of them.",
  unknown_country: "That country is not set up in Booking.",
  catalog_unavailable: "The CIS catalog cannot be read right now, so the place cannot be checked. Try again later.",
  contract_locked: "Rates of a published contract are fixed. Add a new contract for new prices.",
  rate_outside_contract: "A rate stays inside its contract's validity.",
  rate_supplier_mismatch: "Pick a room type or service of this contract's supplier.",
  meal_plan_rooms_only: "A meal plan applies to room rates only.",
  invalid_pax_band: "The pax band ends at or after where it starts.",
  invalid_validity: "“Valid to” is on or after “valid from”.",
  rate_in_use: "A proposal cost line uses this rate.",
};

export function supplyError(err: unknown, fallback = "Something went wrong."): string | null {
  if (!err) return null;
  if (err instanceof ApiError) {
    if (FRIENDLY[err.code]) return FRIENDLY[err.code];
    if (err.fieldErrors.length) return "Some fields are not valid.";
    return err.message || fallback;
  }
  return err instanceof Error ? err.message : fallback;
}
