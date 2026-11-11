"use client";
// app/(staff)/b2b-desk/_parts/AgencyBookingsTab.tsx — B2B desk → "Agency bookings" (AA-1075 bk-11, bk-047).
//
// Bookings AA staff enter for an agency that wrote by e-mail or chat: one call quotes, holds and confirms with the
// agency API's own rules (seats, allotments, credit stop, cancellation policy), and the agency's CRM imports the
// booking from its event feed. A price below the floor margin waits here until a second person approves it (never the
// one who entered it); the seats stay held meanwhile. Travellers are counts only.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus } from "lucide-react";
import { ApiError, type FieldError } from "@/lib/api/client";
import {
  ENTRY_STATUS_LABEL,
  actionKey,
  agencyOpsApi,
  type DeskEntry,
  type EntryStatus,
} from "@/lib/api/agencyOps";
import { departuresApi } from "@/lib/api/departures";
import type { TripPlanListItem } from "@/lib/api/tripPlans";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  DataTable,
  DateField,
  DateText,
  Field,
  FormDrawer,
  Input,
  Pill,
  SegmentedControl,
  Select,
  Textarea,
  useCan,
  useToast,
  useUrlFilters,
  type BookingTone,
} from "@/ui";
import { TripPlanPicker } from "../../cases/_parts/TripPlanPicker";

const STATUS_TONE: Record<EntryStatus, BookingTone> = {
  awaiting_approval: "warning",
  booked: "success",
  rejected: "danger",
  lapsed: "neutral",
};
const CURRENCIES = ["USD", "EUR", "VND"];

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function AgencyBookingsTab() {
  const { get, set } = useUrlFilters();
  const paging = useOffsetPage();
  const canBook = useCan("b2b_desk.book");
  const qc = useQueryClient();
  const toast = useToast();
  const status = (get("entry") || "") as EntryStatus | "";
  const query = { status: status || undefined, limit: paging.limit, offset: paging.offset };
  const list = useQuery({ queryKey: ["b2b-desk", "entries", query], queryFn: () => agencyOpsApi.entries(query) });
  const [creating, setCreating] = useState(false);

  const refresh = () => qc.invalidateQueries({ queryKey: ["b2b-desk"] });
  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      approve ? agencyOpsApi.approveEntry(id) : agencyOpsApi.rejectEntry(id),
    onSuccess: (e) => {
      toast.success(e.status === "booked" ? `Booked as ${e.trip_case?.code}; the agency is told` : "Rejected; the seats are released");
      refresh();
    },
    onError: (e) => {
      toast.error(errorText(e));
      refresh();
    },
  });

  const columns: ColumnDef<DeskEntry, unknown>[] = [
    {
      id: "agency",
      header: "Agency",
      accessorFn: (r) => r.agency.name,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="max-w-[220px] truncate text-[13.5px] font-medium text-ink" title={row.original.agency.name}>
            {row.original.agency.name}
          </span>
          <span className="text-xs text-muted">Ref {row.original.agency_ref}</span>
        </span>
      ),
    },
    {
      id: "booking",
      header: "Booking",
      accessorFn: (r) => r.trip_case?.code ?? "",
      cell: ({ row }) =>
        row.original.trip_case ? (
          <a className="text-[13px] font-medium text-accent-deep hover:underline" href={`/cases/${encodeURIComponent(row.original.trip_case.code)}`}>
            {row.original.trip_case.code}
          </a>
        ) : (
          <span className="text-xs text-muted">
            Held until <DateText value={row.original.hold_expires_at} withTime />
          </span>
        ),
    },
    {
      id: "price",
      header: "Price",
      accessorFn: (r) => Number(r.total),
      cell: ({ row }) => {
        const e = row.original;
        return (
          <span className="flex flex-col text-[13px] text-body">
            <span className="font-medium text-ink">
              {e.total} {e.currency}
            </span>
            {e.override_total && (
              <span className="text-xs text-muted" title={e.override_reason ?? ""}>
                Rule price {e.quoted_total}
                {e.floor_total ? ` · floor ${e.floor_total}` : ""}
              </span>
            )}
          </span>
        );
      },
    },
    {
      id: "status",
      header: "Status",
      accessorFn: (r) => r.status,
      cell: ({ row }) => (
        <span className="flex flex-col gap-1">
          <Pill tone={STATUS_TONE[row.original.status]}>{ENTRY_STATUS_LABEL[row.original.status]}</Pill>
          <span className="text-xs text-muted">
            {row.original.entered_by.name ?? "Staff"} · <DateText value={row.original.entered_at} relative />
          </span>
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      enableSorting: false,
      cell: ({ row }) =>
        row.original.can_decide ? (
          <span className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="primary"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ id: row.original.id, approve: true })}
              ariaLabel={`Approve the price for ${row.original.agency.name}`}
            >
              Approve
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ id: row.original.id, approve: false })}
              ariaLabel={`Reject the price for ${row.original.agency.name}`}
            >
              Reject
            </Button>
          </span>
        ) : null,
    },
  ];

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <SegmentedControl
          appearance="chips"
          size="sm"
          label="Booking state"
          options={[
            { value: "", label: "All" },
            { value: "awaiting_approval", label: "Waiting for approval" },
            { value: "booked", label: "Booked" },
          ]}
          value={status}
          onChange={(k) => set("entry", k || null)}
        />
        {canBook && (
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus size={14} /> New agency booking
          </Button>
        )}
      </div>
      <DataTable<DeskEntry>
        searchable={false}
        tableId="b2b-desk-entries"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error as Error).message : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle="No agency booking entered by staff yet"
      />
      {creating && (
        <NewBookingDrawer
          onClose={() => setCreating(false)}
          onDone={(e) => {
            setCreating(false);
            toast.success(
              e.status === "booked"
                ? `Booked as ${e.trip_case?.code}; the agency's CRM imports it`
                : "The price is below the floor: a second person approves it; the seats are held",
            );
            refresh();
          }}
        />
      )}
    </>
  );
}

function NewBookingDrawer({ onClose, onDone }: { onClose: () => void; onDone: (e: DeskEntry) => void }) {
  const agencies = useQuery({ queryKey: ["b2b-desk", "agencies"], queryFn: agencyOpsApi.agencies });
  const [key] = useState(() => actionKey("desk"));
  const [partner, setPartner] = useState("");
  const [agencyRef, setAgencyRef] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [plan, setPlan] = useState<TripPlanListItem | null>(null);
  const [departure, setDeparture] = useState("");
  const [serviceDate, setServiceDate] = useState("");
  const [adult, setAdult] = useState("2");
  const [child, setChild] = useState("0");
  const [infant, setInfant] = useState("0");
  const [override, setOverride] = useState("");
  const [reason, setReason] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldError[]>([]);
  const deps = useQuery({
    queryKey: ["departures", plan?.source_id],
    queryFn: () => departuresApi.list(plan!.source_id),
    enabled: !!plan,
  });
  const upcoming = useMemo(
    () => (deps.data?.items ?? []).filter((d) => d.status !== "cancelled"),
    [deps.data],
  );
  const save = useMutation({
    mutationFn: () => {
      const dep = upcoming.find((d) => d.id === departure);
      return agencyOpsApi.enter(
        {
          partner_id: partner,
          agency_ref: agencyRef.trim(),
          currency,
          lines: [
            {
              line_ref: "tour",
              component_id: `tp:${plan?.plan_id ?? ""}`,
              departure_id: dep?.id ?? null,
              service_date: dep?.start_date ?? serviceDate,
              pax: { adult: Number(adult) || 0, child: Number(child) || 0, infant: Number(infant) || 0 },
            },
          ],
          override_total: override.trim() || null,
          override_reason: override.trim() ? reason.trim() || null : null,
        },
        key,
      );
    },
    onSuccess: onDone,
    onError: (e) => setFieldErrors(e instanceof ApiError ? e.fieldErrors : []),
  });
  const ready = partner && agencyRef.trim() && plan?.plan_id && (departure || serviceDate) && Number(adult) >= 1;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="New agency booking"
      submitLabel={override.trim() ? "Hold and ask for approval" : "Book"}
      onSubmit={() => (ready ? save.mutate() : undefined)}
      busy={save.isPending}
      dirty={!!(partner || agencyRef || plan)}
      error={save.isError ? errorText(save.error) : null}
      fieldErrors={fieldErrors}
      width={560}
    >
      <p className="mb-4 text-[13px] text-muted">
        For an agency that sent its booking by e-mail or chat. The agency&apos;s rules apply (seats, allotment, credit stop,
        cancellation policy) and its CRM imports the booking. Enter counts only, never traveller names.
      </p>
      <div className="flex flex-col gap-4">
        <Field label="Agency" required>
          <Select
            value={partner}
            onChange={(e) => {
              setPartner(e.target.value);
              const a = agencies.data?.items.find((x) => x.partner_id === e.target.value);
              if (a) setCurrency(a.billing_currency);
            }}
            placeholder={agencies.isLoading ? "Loading…" : "Choose the agency"}
            options={(agencies.data?.items ?? []).map((a) => ({ value: a.partner_id, label: a.agency_name }))}
          />
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Agency reference" required hint="The agency's own booking id (from its e-mail)">
            <Input value={agencyRef} maxLength={64} onChange={(e) => setAgencyRef(e.target.value)} />
          </Field>
          <Field label="Currency">
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)} options={CURRENCIES.map((c) => ({ value: c, label: c }))} />
          </Field>
        </div>
        <Field label="Tour" required as="div">
          <TripPlanPicker value={plan} onChange={(p) => { setPlan(p); setDeparture(""); }} />
        </Field>
        {plan && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="Departure" hint={upcoming.length ? "Fixed departures of this tour" : "No fixed departure: on request"}>
              <Select
                value={departure}
                onChange={(e) => setDeparture(e.target.value)}
                placeholder={upcoming.length ? "Choose a departure" : "None"}
                disabled={!upcoming.length}
                options={upcoming.map((d) => ({ value: d.id, label: `${d.start_date} · ${d.available} free` }))}
              />
            </Field>
            {!departure && (
              <Field label="Start date" required>
                <DateField value={serviceDate} onChange={setServiceDate} />
              </Field>
            )}
          </div>
        )}
        <div className="grid grid-cols-3 gap-3">
          <Field label="Adults" required>
            <Input type="number" min={1} max={60} value={adult} onChange={(e) => setAdult(e.target.value)} />
          </Field>
          <Field label="Children">
            <Input type="number" min={0} max={60} value={child} onChange={(e) => setChild(e.target.value)} />
          </Field>
          <Field label="Infants">
            <Input type="number" min={0} max={20} value={infant} onChange={(e) => setInfant(e.target.value)} />
          </Field>
        </div>
        <Field label="Price override (total)" hint="Leave empty for the agency's price. Below the floor margin a second person approves it.">
          <Input inputMode="decimal" value={override} placeholder="e.g. 1250.00" onChange={(e) => setOverride(e.target.value)} />
        </Field>
        {override.trim() && (
          <Field label="Why this price" required>
            <Textarea value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
      </div>
    </FormDrawer>
  );
}
