"use client";
// app/(staff)/departures/_parts/DepartureBoardView.tsx — Operations → Departures (AA-1073 rest, bk-048; S275 study
// item bk-04): every fixed departure across trip plans for one month, as a calendar or a table. Seats (sold, held,
// kept for agency allotments, free), occupancy, the minimum, At risk, an open incident, how many agencies are on it and
// the price column (the plan's "from" price, the departure's price adjustment and the early-booking discount). Filters:
// country, trip plan, status, at risk only (URL: month, view, country, plan, status, risk). A departure opens the
// drawer (?departure=<id>): seats by agency with their cases, the Ops section, fees and the price adjustment.
// Read permission b2b_desk.read (admin, sales, contracting).

import { useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CalendarClock, ChevronLeft, ChevronRight, Siren, TriangleAlert, Users } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  adjustmentLabel,
  departureBoardApi,
  monthRange,
  monthWeeks,
  shiftMonth,
  type BoardDeparture,
} from "@/lib/api/departureBoard";
import {
  Button,
  DataTable,
  DateText,
  FilterBar,
  KpiTile,
  PageFrame,
  Pressable,
  Section,
  SegmentedControl,
  formatDay,
  useUrlFilters,
} from "@/ui";
import { DepartureBoardDrawer } from "./DepartureBoardDrawer";
import { DepartureStatus, SeatsBar } from "./shared";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

function thisMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

const noSubscribe = () => () => {};

/** This month on the client; null while the server renders (its clock and time zone may differ: no hydration
 *  mismatch, the board waits for the browser's month). */
function useThisMonth(): string | null {
  return useSyncExternalStore(noSubscribe, thisMonth, () => null);
}

function monthLabel(month: string): string {
  const [y, m] = month.split("-").map((x) => Number.parseInt(x, 10));
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

function tourName(d: BoardDeparture): string {
  return d.tour_title ?? `Tour ${d.source_id.slice(0, 8)}`;
}

function priceText(d: BoardDeparture): string {
  const parts: string[] = [];
  // The trip plan's own "from" price, not what an agency is quoted (its tariff): labelled as a hint (S277 check).
  if (d.price.hint) parts.push(`plan hint from ${d.price.hint.amount} ${d.price.hint.currency}`);
  if (d.price.adjustment) parts.push(adjustmentLabel(d.price.adjustment));
  if (d.price.early_booking) parts.push(`early −${Math.round(Number(d.price.early_booking.pts) * 10000) / 100} pts`);
  return parts.join(" · ") || "—";
}

export function DepartureBoardView() {
  const { get } = useUrlFilters();
  const current = useThisMonth();
  const month = MONTH_RE.test(get("month")) ? get("month") : (current ?? "");
  if (!month) return <PageFrame title="Departures">{null}</PageFrame>;
  return <Board month={month} current={current} />;
}

function Board({ month, current }: { month: string; current: string | null }) {
  const { get, set, setMany } = useUrlFilters();
  const view = get("view") === "table" ? "table" : "calendar";
  const open = get("departure");
  const { from, to } = monthRange(month);
  const query = {
    from,
    to,
    country: get("country") || undefined,
    source_id: get("plan") || undefined,
    status: get("status") || undefined,
    at_risk: get("risk") === "1",
    limit: 500,
  };
  const board = useQuery({ queryKey: ["departure-board", "list", query], queryFn: () => departureBoardApi.board(query) });
  const items = board.data?.items ?? [];
  const t = board.data?.totals;
  const openDeparture = (id: string | null) => set("departure", id);

  return (
    <PageFrame
      title="Departures"
      description="Every fixed departure of the month across trip plans: seats sold, held and kept for agency allotments, the minimum to operate, departures at risk and open incidents. Open one for its agencies, ops sheet, fees and price."
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile label="Departures" value={t?.departures} icon={<CalendarClock size={16} />} hint={monthLabel(month)} hintTone="neutral" />
        <KpiTile
          label="Seats sold"
          value={t ? `${t.sold} / ${t.capacity}` : undefined}
          icon={<Users size={16} />}
          hint={t ? `${t.held} held · ${t.free} free` : undefined}
          hintTone="neutral"
        />
        <KpiTile
          label="At risk"
          value={t?.at_risk}
          icon={<TriangleAlert size={16} />}
          hint={t ? (t.at_risk ? "Short of the minimum" : "None") : undefined}
          hintTone={t?.at_risk ? "warning" : "neutral"}
        />
        <KpiTile
          label="Open incidents"
          value={t?.incidents}
          icon={<Siren size={16} />}
          hint={t ? (t.incidents ? "Agencies were told" : "None") : undefined}
          hintTone={t?.incidents ? "danger" : "neutral"}
        />
      </div>
      <Section>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" ariaLabel="Previous month" onClick={() => setMany({ month: shiftMonth(month, -1), departure: null })}>
              <ChevronLeft size={16} />
            </Button>
            <h2 className="min-w-[150px] text-center text-[15px] font-semibold text-ink" data-testid="board-month">
              {monthLabel(month)}
            </h2>
            <Button size="sm" variant="ghost" ariaLabel="Next month" onClick={() => setMany({ month: shiftMonth(month, 1), departure: null })}>
              <ChevronRight size={16} />
            </Button>
            {current && month !== current && (
              <Button size="sm" variant="ghost" onClick={() => set("month", null)}>
                This month
              </Button>
            )}
          </div>
          <SegmentedControl<"calendar" | "table">
            label="Board view"
            size="sm"
            value={view}
            onChange={(v) => set("view", v === "calendar" ? null : v)}
            options={[
              { value: "calendar", label: "Calendar" },
              { value: "table", label: "Table" },
            ]}
          />
        </div>
        <div className="mb-4">
          <FilterBar
            layout="toolbar"
            extraKeys={["month", "view", "departure"]}
            selects={[
              {
                key: "country",
                label: "Country",
                allLabel: "All countries",
                options: (board.data?.countries ?? []).map((c) => ({ value: c, label: c })),
                width: 170,
              },
              {
                key: "plan",
                label: "Trip plan",
                allLabel: "All trip plans",
                options: (board.data?.plans ?? []).map((p) => ({ value: p.source_id, label: p.title ?? `Tour ${p.source_id.slice(0, 8)}` })),
                width: 240,
              },
              {
                key: "status",
                label: "Status",
                allLabel: "All statuses",
                options: [
                  { value: "scheduled", label: "Scheduled" },
                  { value: "guaranteed", label: "Guaranteed" },
                  { value: "cancelled", label: "Cancelled" },
                ],
                width: 150,
              },
              { key: "risk", label: "Risk", allLabel: "Any", options: [{ value: "1", label: "At risk only" }], width: 130 },
            ]}
          />
        </div>
        {board.isError ? (
          <p role="alert" className="text-[13px] text-danger">
            {board.error instanceof ApiError ? board.error.message : "Could not load the departures"}
          </p>
        ) : view === "calendar" ? (
          <Calendar month={month} items={items} loading={board.isLoading} onOpen={openDeparture} />
        ) : (
          <BoardTable items={items} loading={board.isLoading} onOpen={openDeparture} onRetry={() => board.refetch()} />
        )}
      </Section>
      {open && <DepartureBoardDrawer departureId={open} onClose={() => openDeparture(null)} />}
    </PageFrame>
  );
}

function Chip({ d, onOpen }: { d: BoardDeparture; onOpen: (id: string) => void }) {
  const tone = d.status === "cancelled" ? "border-line2 opacity-60" : d.at_risk ? "border-warning" : "border-line2";
  return (
    <Pressable
      onClick={() => onOpen(d.id)}
      title={`${tourName(d)}: ${d.sold} sold, ${d.held} held, ${d.free} free of ${d.capacity}`}
      testId="board-chip"
      className="min-w-0"
    >
      <span className={`flex w-full min-w-0 flex-col gap-1 rounded-md border bg-card px-2 py-1.5 text-left hover:border-accent-border ${tone}`}>
        <span className="truncate text-xs font-medium text-ink">{tourName(d)}</span>
        <SeatsBar d={d} />
        {(d.at_risk || d.open_incident || d.status !== "scheduled") && <DepartureStatus d={d} />}
      </span>
    </Pressable>
  );
}

function Calendar({
  month,
  items,
  loading,
  onOpen,
}: {
  month: string;
  items: BoardDeparture[];
  loading: boolean;
  onOpen: (id: string) => void;
}) {
  const byDay = new Map<string, BoardDeparture[]>();
  for (const d of items) byDay.set(d.start_date, [...(byDay.get(d.start_date) ?? []), d]);
  const days = [...byDay.keys()].sort();
  return (
    <>
      {/* Phone: the days that have departures, as a list (a 7-column grid does not fit 390 px). */}
      <ul className="flex flex-col gap-3 sm:hidden" data-testid="board-day-list" aria-busy={loading}>
        {loading ? (
          <li className="text-[13px] text-muted">Loading…</li>
        ) : days.length === 0 ? (
          <li className="text-[13px] text-muted2">No departure this month.</li>
        ) : (
          days.map((day) => (
            <li key={day} className="flex flex-col gap-2">
              <span className="text-xs font-semibold text-muted">
                <DateText value={day} />
              </span>
              {byDay.get(day)!.map((d) => (
                <Chip key={d.id} d={d} onOpen={onOpen} />
              ))}
            </li>
          ))
        )}
      </ul>
      <div className="hidden sm:block" data-testid="board-calendar" aria-busy={loading}>
        {/* S277: a slow board must not look like an empty month. */}
        {loading && <p className="pb-2 text-[13px] text-muted">Loading the departures of this month…</p>}
        <div className="grid grid-cols-7 gap-1 pb-1">
          {WEEKDAYS.map((w) => (
            <span key={w} className="px-1 text-[11.5px] font-semibold uppercase tracking-[0.04em] text-muted">
              {w}
            </span>
          ))}
        </div>
        <div className="grid grid-cols-7 gap-1">
          {monthWeeks(month)
            .flat()
            .map((day, i) => (
              <div
                key={day ?? `pad-${i}`}
                className={`flex min-h-[96px] min-w-0 flex-col gap-1 rounded-md border p-1 ${day ? "border-line2" : "border-transparent"}`}
              >
                {day && <span className="bk-tabular px-1 text-xs text-muted">{Number(day.slice(8))}</span>}
                {day && (byDay.get(day) ?? []).map((d) => <Chip key={d.id} d={d} onOpen={onOpen} />)}
              </div>
            ))}
        </div>
        {!loading && items.length === 0 && <p className="mt-3 text-[13px] text-muted2">No departure this month with these filters.</p>}
      </div>
    </>
  );
}

function BoardTable({
  items,
  loading,
  onOpen,
  onRetry,
}: {
  items: BoardDeparture[];
  loading: boolean;
  onOpen: (id: string) => void;
  onRetry: () => void;
}) {
  const columns: ColumnDef<BoardDeparture, unknown>[] = [
    {
      id: "date",
      header: "Departure",
      accessorFn: (r) => r.start_date,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="text-[13.5px] font-medium text-ink">{formatDay(row.original.start_date)}</span>
          <span className="max-w-[260px] truncate text-xs text-muted" title={tourName(row.original)}>
            {tourName(row.original)}
            {row.original.country ? ` · ${row.original.country}` : ""}
          </span>
        </span>
      ),
    },
    { id: "status", header: "Status", accessorFn: (r) => r.status, cell: ({ row }) => <DepartureStatus d={row.original} /> },
    {
      id: "seats",
      header: "Seats",
      accessorFn: (r) => r.sold,
      cell: ({ row }) => (
        <span className="flex flex-col gap-1">
          <span className="bk-tabular whitespace-nowrap text-[13px] text-body">
            {row.original.sold} sold · {row.original.held} held · {row.original.free} free / {row.original.capacity}
          </span>
          <SeatsBar d={row.original} />
        </span>
      ),
    },
    {
      id: "minimum",
      header: "Minimum",
      accessorFn: (r) => r.min_to_operate,
      cell: ({ row }) => (
        <span className="bk-tabular text-[13px] text-body">{row.original.min_to_operate ? `${row.original.sold} / ${row.original.min_to_operate}` : "—"}</span>
      ),
    },
    {
      id: "agencies",
      header: "Agencies",
      accessorFn: (r) => r.agencies_count,
      cell: ({ row }) => <span className="bk-tabular text-[13px] text-body">{row.original.agencies_count}</span>,
    },
    {
      id: "price",
      header: "Price",
      accessorFn: (r) => r.price.adjustment?.id ?? "",
      cell: ({ row }) => <span className="max-w-[220px] text-xs text-body">{priceText(row.original)}</span>,
    },
  ];
  return (
    <DataTable<BoardDeparture>
      searchable={false}
      tableId="departure-board"
      data={items}
      columns={columns}
      getRowId={(r) => r.id}
      onRowClick={(r) => onOpen(r.id)}
      loading={loading}
      onRetry={onRetry}
      pageSizeOptions={[25, 50, 100]}
      emptyTitle="No departure this month"
      emptyDescription="Departures are created on a trip plan (Catalog → Trip Plans → Departures)."
      tableMinWidth={900}
    />
  );
}
