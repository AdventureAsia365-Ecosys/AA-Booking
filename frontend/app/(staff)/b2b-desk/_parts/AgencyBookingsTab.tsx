"use client";
// app/(staff)/b2b-desk/_parts/AgencyBookingsTab.tsx — B2B desk → "Agency bookings" (AA-1075 bk-11, bk-047).
//
// Bookings AA staff enter for an agency that wrote by e-mail or chat: one call quotes, holds and confirms with the
// agency API's own rules (seats, allotments, credit stop, cancellation policy), and the agency's CRM imports the
// booking from its event feed. A price below the floor margin waits here until a second person approves it (never the
// one who entered it); the seats stay held meanwhile. Travellers are counts only. The form takes the tour's room mix
// (empty = the engine default: doubles plus a single when the party is odd) and extra hotel rooms or services from
// supply, each its own line with the tour's travellers.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, X } from "lucide-react";
import { ApiError, type FieldError } from "@/lib/api/client";
import {
  ENTRY_STATUS_LABEL,
  OCCUPANCIES,
  OCCUPANCY_LABEL,
  actionKey,
  agencyOpsApi,
  type DeskEntry,
  type EntryLine,
  type EntryStatus,
  type Occupancy,
  type RoomMix,
} from "@/lib/api/agencyOps";
import type { SupplyOption } from "@/lib/api/supply";
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
  IconButton,
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
import { SupplyPicker } from "../../cases/[code]/proposals/[n]/SupplyPicker";
import { defaultRoomsLabel, deskLines } from "@/ui/booking/deskEntry";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./AgencyBookingsTab.messages";

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
  const t = useT(MSG);
  const tx = useTx();
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
      toast.success(e.status === "booked" ? t("Booked as {0}; the agency is told", e.trip_case?.code) : t("Rejected; the seats are released"));
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
      header: t("Agency"),
      accessorFn: (r) => r.agency.name,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="max-w-[220px] truncate text-[13.5px] font-medium text-ink" title={row.original.agency.name}>
            {row.original.agency.name}
          </span>
          <span className="text-xs text-muted">{t("Ref")} {row.original.agency_ref}</span>
        </span>
      ),
    },
    {
      id: "booking",
      header: t("Booking"),
      accessorFn: (r) => r.trip_case?.code ?? "",
      cell: ({ row }) =>
        row.original.trip_case ? (
          <a className="text-[13px] font-medium text-accent-deep hover:underline" href={`/cases/${encodeURIComponent(row.original.trip_case.code)}`}>
            {row.original.trip_case.code}
          </a>
        ) : (
          <span className="text-xs text-muted">
            {t("Held until")} <DateText value={row.original.hold_expires_at} withTime />
          </span>
        ),
    },
    {
      id: "price",
      header: t("Price"),
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
                {t("Rule price {0}{1}", e.quoted_total, e.floor_total ? ` · floor ${e.floor_total}` : "")}
              </span>
            )}
          </span>
        );
      },
    },
    {
      id: "status",
      header: t("Status"),
      accessorFn: (r) => r.status,
      cell: ({ row }) => (
        <span className="flex flex-col items-start gap-1">
          <Pill tone={STATUS_TONE[row.original.status]}>{tx(ENTRY_STATUS_LABEL[row.original.status])}</Pill>
          <span className="text-xs text-muted">
            {row.original.entered_by.name ?? t("Staff")} · <DateText value={row.original.entered_at} relative />
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
              ariaLabel={t("Approve the price for {0}", row.original.agency.name)}
            >
              {t("Approve")}
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ id: row.original.id, approve: false })}
              ariaLabel={t("Reject the price for {0}", row.original.agency.name)}
            >
              {t("Reject")}
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
          label={t("Booking state")}
          options={[
            { value: "", label: t("All") },
            { value: "awaiting_approval", label: t("Waiting for approval") },
            { value: "booked", label: t("Booked") },
          ]}
          value={status}
          onChange={(k) => set("entry", k || null)}
        />
        {canBook && (
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus size={14} /> {t("New agency booking")}
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
        emptyTitle={t("No agency booking entered by staff yet")}
      />
      {creating && (
        <NewBookingDrawer
          onClose={() => setCreating(false)}
          onDone={(e) => {
            setCreating(false);
            toast.success(
              e.status === "booked"
                ? t("Booked as {0}; the agency's CRM imports it", e.trip_case?.code)
                : t("The price is below the floor: a second person approves it; the seats are held"),
            );
            refresh();
          }}
        />
      )}
    </>
  );
}

function NewBookingDrawer({ onClose, onDone }: { onClose: () => void; onDone: (e: DeskEntry) => void }) {
  const t = useT(MSG);
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
  const [rooms, setRooms] = useState<RoomMix[]>([]);
  const [extras, setExtras] = useState<Extra[]>([]);
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
      const lines: EntryLine[] = deskLines({
        planId: plan?.plan_id ?? "",
        departureId: dep?.id ?? null,
        start: dep?.start_date ?? serviceDate,
        pax: { adult: Number(adult) || 0, child: Number(child) || 0, infant: Number(infant) || 0 },
        rooms,
        extras: extras.map((x) => ({ kind: x.option.kind, id: x.option.id, date: x.date, nights: x.nights })),
      });
      return agencyOpsApi.enter(
        {
          partner_id: partner,
          agency_ref: agencyRef.trim(),
          currency,
          lines,
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
      title={t("New agency booking")}
      submitLabel={override.trim() ? t("Hold and ask for approval") : t("Book")}
      onSubmit={() => (ready ? save.mutate() : undefined)}
      busy={save.isPending}
      dirty={!!(partner || agencyRef || plan)}
      error={save.isError ? errorText(save.error) : null}
      fieldErrors={fieldErrors}
      width={560}
    >
      <p className="mb-4 text-[13px] text-muted">
        {t("For an agency that sent its booking by e-mail or chat. The agency's rules apply (seats, allotment, credit stop, cancellation policy) and its CRM imports the booking. Enter counts only, never traveller names.")}
      </p>
      <div className="flex flex-col gap-4">
        <Field label={t("Agency")} required>
          <Select
            value={partner}
            onChange={(e) => {
              setPartner(e.target.value);
              const a = agencies.data?.items.find((x) => x.partner_id === e.target.value);
              if (a) setCurrency(a.billing_currency);
            }}
            placeholder={agencies.isLoading ? t("Loading…") : t("Choose the agency")}
            options={(agencies.data?.items ?? []).map((a) => ({ value: a.partner_id, label: a.agency_name }))}
          />
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label={t("Agency reference")} required hint={t("The agency's own booking id (from its e-mail)")}>
            <Input value={agencyRef} maxLength={64} onChange={(e) => setAgencyRef(e.target.value)} />
          </Field>
          <Field label={t("Currency")}>
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)} options={CURRENCIES.map((c) => ({ value: c, label: c }))} />
          </Field>
        </div>
        <Field label={t("Tour")} required as="div">
          <TripPlanPicker value={plan} onChange={(p) => { setPlan(p); setDeparture(""); }} />
        </Field>
        {plan && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label={t("Departure")} hint={upcoming.length ? t("Fixed departures of this tour") : t("No fixed departure: on request")}>
              <Select
                value={departure}
                onChange={(e) => setDeparture(e.target.value)}
                placeholder={upcoming.length ? t("Choose a departure") : t("None")}
                disabled={!upcoming.length}
                options={upcoming.map((d) => ({ value: d.id, label: t("{0} · {1} free", d.start_date, d.available) }))}
              />
            </Field>
            {!departure && (
              <Field label={t("Start date")} required>
                <DateField value={serviceDate} onChange={setServiceDate} />
              </Field>
            )}
          </div>
        )}
        <div className="grid grid-cols-3 gap-3">
          <Field label={t("Adults")} required>
            <Input type="number" min={1} max={60} value={adult} onChange={(e) => setAdult(e.target.value)} />
          </Field>
          <Field label={t("Children")}>
            <Input type="number" min={0} max={60} value={child} onChange={(e) => setChild(e.target.value)} />
          </Field>
          <Field label={t("Infants")}>
            <Input type="number" min={0} max={20} value={infant} onChange={(e) => setInfant(e.target.value)} />
          </Field>
        </div>
        <RoomMixEditor rooms={rooms} onChange={setRooms} party={(Number(adult) || 0) + (Number(child) || 0)} />
        <ExtrasEditor extras={extras} onChange={setExtras} />
        <Field label={t("Price override (total)")} hint={t("Leave empty for the agency's price. Below the floor margin a second person approves it.")}>
          <Input inputMode="decimal" value={override} placeholder="e.g. 1250.00" onChange={(e) => setOverride(e.target.value)} />
        </Field>
        {override.trim() && (
          <Field label={t("Why this price")} required>
            <Textarea value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
      </div>
    </FormDrawer>
  );
}

/** One extra line of a staff booking: a hotel room or a service from supply, for the tour's travellers. */
interface Extra {
  key: string;
  option: SupplyOption;
  date: string; // empty = the tour's start date
  nights: string; // rooms only
}

/** The tour's room mix. Empty = the engine default (doubles plus a single when the party is odd). */
function RoomMixEditor({ rooms, onChange, party }: { rooms: RoomMix[]; onChange: (r: RoomMix[]) => void; party: number }) {
  const t = useT(MSG);
  const tx = useTx();
  const used = new Set(rooms.map((r) => r.occupancy));
  const next = OCCUPANCIES.find((o) => !used.has(o));
  const set = (i: number, patch: Partial<RoomMix>) => onChange(rooms.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Field
      label={t("Room mix")}
      as="div"
      hint={
        rooms.length
          ? t("The rooms must hold the adults; children may share.")
          : t("Empty = {0} for {1} travellers (doubles, plus a single when odd).", defaultRoomsLabel(party), party || 0)
      }
    >
      <div className="flex flex-col gap-2" data-testid="desk-room-mix">
        {rooms.map((r, i) => (
          <div key={r.occupancy} className="grid grid-cols-[1fr_96px_auto] items-center gap-2">
            <Select
              aria-label={t("Room occupancy")}
              value={r.occupancy}
              onChange={(e) => set(i, { occupancy: e.target.value as Occupancy })}
              options={OCCUPANCIES.filter((o) => o === r.occupancy || !used.has(o)).map((o) => ({
                value: o,
                label: tx(OCCUPANCY_LABEL[o]),
              }))}
            />
            <Input
              aria-label={t("Rooms")}
              type="number"
              min={1}
              max={30}
              value={String(r.count)}
              onChange={(e) => set(i, { count: Math.min(30, Math.max(0, Number(e.target.value) || 0)) })}
            />
            <IconButton title={t("Remove")} label={t("Remove the {0} rooms", OCCUPANCY_LABEL[r.occupancy])} onClick={() => onChange(rooms.filter((_, j) => j !== i))}>
              <X size={14} />
            </IconButton>
          </div>
        ))}
        {next && (
          <span>
            <Button size="sm" variant="ghost" onClick={() => onChange([...rooms, { occupancy: next, count: 1 }])}>
              <Plus size={14} /> {t("Add rooms")}
            </Button>
          </span>
        )}
      </div>
    </Field>
  );
}

/** Extra hotel rooms or services (transfers, activities, guides) booked with the tour, each its own priced line. */
function ExtrasEditor({ extras, onChange }: { extras: Extra[]; onChange: (x: Extra[]) => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const set = (key: string, patch: Partial<Extra>) => onChange(extras.map((x) => (x.key === key ? { ...x, ...patch } : x)));
  return (
    <Field label={t("Extra rooms and services")} as="div" hint={t("Priced at the supplier rate in force, for the same travellers. Date empty = the tour's start.")}>
      <div className="flex flex-col gap-2" data-testid="desk-extras">
        {extras.map((x) => (
          <div key={x.key} className="flex flex-col gap-2 rounded-lg border border-line2 p-2">
            <div className="flex items-center justify-between gap-2 text-[13px]">
              <span className="min-w-0 truncate text-ink">
                <span className="text-muted">{x.option.kind === "room_type" ? t("Room · ") : t("Service · ")}</span>
                {tx(x.option.label)}
              </span>
              <IconButton title={t("Remove")} label={t("Remove {0}", x.option.label)} onClick={() => onChange(extras.filter((y) => y.key !== x.key))}>
                <X size={14} />
              </IconButton>
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <DateField value={x.date} onChange={(d) => set(x.key, { date: d })} />
              {x.option.kind === "room_type" && (
                <Input
                  aria-label={t("Nights")}
                  type="number"
                  min={1}
                  max={60}
                  value={x.nights}
                  onChange={(e) => set(x.key, { nights: e.target.value })}
                />
              )}
            </div>
          </div>
        ))}
        {extras.length < 20 && (
          <SupplyPicker
            onPick={(o) => onChange([...extras, { key: `${o.kind}:${o.id}:${Date.now()}`, option: o, date: "", nights: "1" }])}
          />
        )}
      </div>
    </Field>
  );
}
