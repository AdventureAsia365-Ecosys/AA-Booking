"use client";
// app/(staff)/b2b-desk/_parts/B2BDeskView.tsx — Operations → B2B desk (AA-975, bk-022; design v3 §2.5 "Booking staff
// admin": on-request desk + "Flag incident on departure", Fix B3; Booking commercial-core memo seam 10).
//
// Tab "On request": lines of confirmed B2B bookings that AA ops must accept or decline within 1 business day (ops time
// zone), with the SLA badge (overdue / due soon / on time). Accept = confirmed; decline needs a reason code.
// Tab "Departures": departures from 14 days ago to 90 days ahead, seats, how many agencies are on each, and the open
// incident; "Flag incident" picks a code (no free text: an incident never carries traveller data), "Clear" ends it.
// Every answer, flag and clear reaches the agencies as an event on booking.b2b.v1 GET /events (the CRM shows it).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { AlarmClock, CircleAlert, ClipboardCheck, Siren } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  DECLINE_CODES,
  DECLINE_LABEL,
  INCIDENT_CODES,
  INCIDENT_LABEL,
  b2bDeskApi,
  type DeclineCode,
  type DeskDeparture,
  type IncidentCode,
  type LineState,
  type OnRequestLine,
  type Sla,
} from "@/lib/api/b2bDesk";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  DataTable,
  DateText,
  KpiTile,
  Modal,
  PageFrame,
  Pill,
  Section,
  useCan,
  useToast,
  useUrlFilters,
  type BookingTone,
} from "@/ui";
import { SavedTabs } from "../../_parts/SavedTabs";

const SLA_META: Record<Sla, { label: string; tone: BookingTone; help: string }> = {
  overdue: { label: "Overdue", tone: "danger", help: "Past the 1 business day answer deadline" },
  due_soon: { label: "Due soon", tone: "warning", help: "Answer deadline within the next hours" },
  on_time: { label: "On time", tone: "success", help: "Within the 1 business day answer deadline" },
  answered: { label: "Answered", tone: "neutral", help: "Accepted or declined" },
  closed: { label: "Closed", tone: "neutral", help: "The booking was cancelled before an answer" },
};
const LINE_STATUS: Record<OnRequestLine["status"], { label: string; tone: BookingTone }> = {
  on_request: { label: "On request", tone: "warning" },
  confirmed: { label: "Confirmed", tone: "success" },
  declined: { label: "Declined", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};
const KIND_LABEL: Record<string, string> = {
  trip_plan: "Tour",
  hotel_room: "Hotel room",
  transfer: "Transfer",
  guide: "Guide",
  activity: "Activity",
  entrance: "Entrance",
  meal: "Meal",
};
const fieldCls = "h-9 w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

function pax(p: OnRequestLine["pax"]): string {
  const parts = [`${p.adult} adult${p.adult === 1 ? "" : "s"}`];
  if (p.child) parts.push(`${p.child} child${p.child === 1 ? "" : "ren"}`);
  if (p.infant) parts.push(`${p.infant} infant${p.infant === 1 ? "" : "s"}`);
  return parts.join(", ");
}

export function B2BDeskView() {
  const { get } = useUrlFilters();
  const tab = get("tab") || "on-request";
  const summary = useQuery({ queryKey: ["b2b-desk", "summary"], queryFn: b2bDeskApi.summary, refetchInterval: 60_000 });
  const s = summary.data;
  return (
    <PageFrame
      title="B2B desk"
      description="Agency bookings that need AA ops: on-request lines to accept or decline within 1 business day, and incidents on departures. Agencies see every answer in their CRM."
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile label="On request" value={s?.on_request.open} icon={<ClipboardCheck size={16} />} hint="Waiting for an answer" hintTone="neutral" />
        <KpiTile
          label="Overdue"
          value={s?.on_request.overdue}
          icon={<CircleAlert size={16} />}
          hint={s ? (s.on_request.overdue ? "Past the deadline" : "None") : undefined}
          hintTone={s?.on_request.overdue ? "danger" : "neutral"}
        />
        <KpiTile
          label="Due soon"
          value={s?.on_request.due_soon}
          icon={<AlarmClock size={16} />}
          hint={s ? `Within ${s.due_soon_hours} h` : undefined}
          hintTone={s?.on_request.due_soon ? "warning" : "neutral"}
        />
        <KpiTile
          label="Open incidents"
          value={s?.open_incidents}
          icon={<Siren size={16} />}
          hint={s ? (s.open_incidents ? "Agencies were told" : "None") : undefined}
          hintTone={s?.open_incidents ? "warning" : "neutral"}
        />
      </div>
      <Section>
        <div className="mb-4">
          <SavedTabs
            tabs={[
              { key: "on-request", label: "On request", count: s?.on_request.open },
              { key: "departures", label: "Departures & incidents", count: s?.open_incidents },
            ]}
          />
        </div>
        {tab === "departures" ? <DeparturesTab /> : <OnRequestTab />}
      </Section>
    </PageFrame>
  );
}

// ── On request ───────────────────────────────────────────────────────────────

function OnRequestTab() {
  const { get, set } = useUrlFilters();
  const paging = useOffsetPage();
  const canWrite = useCan("b2b_desk.write");
  const qc = useQueryClient();
  const toast = useToast();
  const state = (get("state") || "open") as LineState;
  const query = { state, limit: paging.limit, offset: paging.offset };
  const list = useQuery({ queryKey: ["b2b-desk", "on-request", query], queryFn: () => b2bDeskApi.onRequest(query) });
  const [declining, setDeclining] = useState<OnRequestLine | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["b2b-desk"] });
  const accept = useMutation({
    mutationFn: (l: OnRequestLine) => b2bDeskApi.accept(l.hold_id, l.line_ref),
    onSuccess: (l) => {
      toast.success(`Line ${l.line_ref} of ${l.trip_case.code} confirmed; the agency is told`);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const columns: ColumnDef<OnRequestLine, unknown>[] = [
    {
      id: "sla",
      header: "SLA",
      accessorFn: (r) => r.answer_due_at ?? "",
      cell: ({ row }) => {
        const meta = SLA_META[row.original.sla];
        return (
          <span className="flex min-w-0 flex-col gap-1">
            <Pill tone={meta.tone} title={meta.help}>
              {meta.label}
            </Pill>
            {row.original.answer_due_at && row.original.status === "on_request" && (
              <span className="text-xs text-muted">
                by <DateText value={row.original.answer_due_at} withTime />
              </span>
            )}
          </span>
        );
      },
    },
    {
      id: "item",
      header: "Item",
      accessorFn: (r) => r.component.label ?? r.component.id,
      cell: ({ row }) => {
        const l = row.original;
        return (
          // AA-910: capped on desktop (full text in the tooltips) so the table fits 1400 px with the action buttons.
          <span className="flex min-w-0 flex-col md:max-w-[210px]">
            <span className="max-w-[300px] truncate text-[13.5px] font-medium text-ink" title={l.component.label ?? l.component.id}>
              {l.component.label ?? l.component.id}
            </span>
            <span
              className="text-xs text-muted md:truncate"
              title={`${KIND_LABEL[l.component.kind] ?? l.component.kind}${l.component.supplier ? ` · ${l.component.supplier.name}` : ""} · line ${l.line_ref}`}
            >
              {KIND_LABEL[l.component.kind] ?? l.component.kind}
              {l.component.supplier ? ` · ${l.component.supplier.name}` : ""} · line {l.line_ref}
            </span>
          </span>
        );
      },
    },
    {
      id: "when",
      header: "Service date",
      accessorFn: (r) => r.service_date,
      cell: ({ row }) => (
        <span className="flex flex-col text-[13px] text-body">
          <DateText value={row.original.service_date} />
          <span className="text-xs text-muted">
            {pax(row.original.pax)}
            {row.original.nights ? ` · ${row.original.nights} night${row.original.nights === 1 ? "" : "s"}` : ""}
          </span>
        </span>
      ),
    },
    {
      id: "booking",
      header: "Booking",
      accessorFn: (r) => r.trip_case.code,
      cell: ({ row }) => (
        <span className="flex flex-col">
          <a className="text-[13px] font-medium text-accent-deep hover:underline" href={`/cases/${encodeURIComponent(row.original.trip_case.code)}`}>
            {row.original.trip_case.code}
          </a>
          <span className="max-w-[200px] truncate text-xs text-muted md:max-w-[160px]" title={row.original.partner.agency_name}>
            {row.original.partner.agency_name}
          </span>
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      accessorFn: (r) => r.status,
      cell: ({ row }) => {
        const l = row.original;
        const meta = LINE_STATUS[l.status];
        return (
          <span className="flex flex-col gap-1">
            <Pill tone={meta.tone}>{meta.label}</Pill>
            {l.decline_code && <span className="text-xs text-muted">{DECLINE_LABEL[l.decline_code]}</span>}
            {l.answered_at && (
              <span className="text-xs text-muted">
                {l.answered_by?.name ?? "Staff"} · <DateText value={l.answered_at} relative className="whitespace-nowrap" />
              </span>
            )}
          </span>
        );
      },
    },
    ...(canWrite
      ? [
          {
            id: "actions",
            header: "",
            enableSorting: false,
            cell: ({ row }: { row: { original: OnRequestLine } }) =>
              row.original.status === "on_request" ? (
                <span className="flex justify-end gap-2">
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={accept.isPending}
                    onClick={() => accept.mutate(row.original)}
                    ariaLabel={`Accept line ${row.original.line_ref} of ${row.original.trip_case.code}`}
                  >
                    Accept
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => setDeclining(row.original)}
                    ariaLabel={`Decline line ${row.original.line_ref} of ${row.original.trip_case.code}`}
                  >
                    Decline
                  </Button>
                </span>
              ) : null,
          } satisfies ColumnDef<OnRequestLine, unknown>,
        ]
      : []),
  ];

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {(["open", "answered", "all"] as const).map((k) => (
          <button
            key={k}
            onClick={() => set("state", k === "open" ? null : k)}
            className={`rounded-full border px-3 py-1 text-xs ${
              state === k ? "border-accent bg-accent-tint font-semibold text-accent-deep" : "border-line text-muted hover:text-ink"
            }`}
          >
            {k === "open" ? "Waiting for an answer" : k === "answered" ? "Answered" : "All"}
          </button>
        ))}
      </div>
      <DataTable<OnRequestLine>
        searchable={false}
        tableId="b2b-desk-on-request"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => `${r.hold_id}:${r.line_ref}`}
        loading={list.isLoading}
        error={list.isError ? (list.error as Error).message : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={state === "open" ? "Nothing waits for an answer" : "No lines here yet"}
        emptyDescription="A line comes here when an agency confirms a booking with an item AA must check with the supplier first (no fixed departure, or a departure that does not auto-confirm)."
        tableMinWidth={canWrite ? 980 : 820}
      />
      <p className="mt-3 text-xs text-muted">
        The deadline is 1 business day (Monday to Friday, Vietnam time) after the agency confirmed. Public holidays are not counted
        yet.
      </p>
      <DeclineModal line={declining} onClose={() => setDeclining(null)} onDone={refresh} />
    </>
  );
}

function DeclineModal({ line, onClose, onDone }: { line: OnRequestLine | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [code, setCode] = useState<DeclineCode>("supplier_unavailable");
  const decline = useMutation({
    mutationFn: () => b2bDeskApi.decline(line!.hold_id, line!.line_ref, code),
    onSuccess: (l) => {
      toast.success(`Line ${l.line_ref} of ${l.trip_case.code} declined; the agency is told`);
      onDone();
      onClose();
    },
  });
  return (
    <Modal
      open={!!line}
      onClose={onClose}
      title="Decline on-request line"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" disabled={decline.isPending} onClick={() => decline.mutate()}>
            Decline line
          </Button>
        </>
      }
    >
      {line && (
        <div className="flex flex-col gap-3 text-[13px] text-body">
          <p>
            {line.component.label ?? line.component.id} on <DateText value={line.service_date} /> for {line.trip_case.code} (
            {line.partner.agency_name}). Its seats are released and the agency sees the line as declined.
          </p>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Reason</span>
            <select className={fieldCls} value={code} onChange={(e) => setCode(e.target.value as DeclineCode)}>
              {DECLINE_CODES.map((c) => (
                <option key={c} value={c}>
                  {DECLINE_LABEL[c]}
                </option>
              ))}
            </select>
          </label>
          {decline.isError && <p className="text-danger">{errorText(decline.error)}</p>}
        </div>
      )}
    </Modal>
  );
}

// ── Departures and incidents ─────────────────────────────────────────────────

const DEP_STATUS: Record<DeskDeparture["status"], { label: string; tone: BookingTone }> = {
  scheduled: { label: "Scheduled", tone: "neutral" },
  guaranteed: { label: "Guaranteed", tone: "success" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

function DeparturesTab() {
  const canWrite = useCan("b2b_desk.write");
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["b2b-desk", "departures"], queryFn: () => b2bDeskApi.departures({}) });
  const [flagging, setFlagging] = useState<DeskDeparture | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["b2b-desk"] });
  const clear = useMutation({
    mutationFn: (incidentId: string) => b2bDeskApi.clear(incidentId),
    onSuccess: () => {
      toast.success("Incident cleared; the agencies are told");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const columns: ColumnDef<DeskDeparture, unknown>[] = [
    {
      id: "date",
      header: "Departure",
      accessorFn: (r) => r.start_date,
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span className="text-[13.5px] font-medium text-ink">
            <DateText value={row.original.start_date} />
          </span>
          <span className="max-w-[300px] truncate text-xs text-muted" title={row.original.tour_title ?? row.original.source_id}>
            {row.original.tour_title ?? `Tour ${row.original.source_id.slice(0, 8)}`}
          </span>
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      accessorFn: (r) => r.status,
      cell: ({ row }) => <Pill tone={DEP_STATUS[row.original.status].tone}>{DEP_STATUS[row.original.status].label}</Pill>,
    },
    {
      id: "seats",
      header: "Seats",
      accessorFn: (r) => r.sold,
      cell: ({ row }) => (
        <span className="bk-tabular text-[13px] text-body">
          {row.original.sold} sold · {row.original.held} held · {row.original.available} free / {row.original.capacity}
        </span>
      ),
    },
    {
      id: "partners",
      header: "Agencies",
      accessorFn: (r) => r.partners,
      cell: ({ row }) => <span className="bk-tabular text-[13px] text-body">{row.original.partners}</span>,
    },
    {
      id: "incident",
      header: "Incident",
      accessorFn: (r) => r.open_incident?.incident_code ?? "",
      cell: ({ row }) => {
        const inc = row.original.open_incident;
        if (!inc) return <span className="text-[13px] text-muted2">—</span>;
        return (
          <span className="flex flex-col gap-1">
            <Pill tone="danger">{INCIDENT_LABEL[inc.incident_code]}</Pill>
            <span className="text-xs text-muted">
              {inc.flagged_by.name ?? "Staff"} · <DateText value={inc.flagged_at} relative className="whitespace-nowrap" />
            </span>
          </span>
        );
      },
    },
    ...(canWrite
      ? [
          {
            id: "actions",
            header: "",
            enableSorting: false,
            cell: ({ row }: { row: { original: DeskDeparture } }) => {
              const d = row.original;
              if (d.status === "cancelled") return null;
              return (
                <span className="flex justify-end">
                  {d.open_incident ? (
                    <Button size="sm" disabled={clear.isPending} onClick={() => clear.mutate(d.open_incident!.id)}>
                      Clear incident
                    </Button>
                  ) : (
                    <Button size="sm" variant="danger" onClick={() => setFlagging(d)} ariaLabel={`Flag incident on departure ${d.start_date}`}>
                      Flag incident
                    </Button>
                  )}
                </span>
              );
            },
          } satisfies ColumnDef<DeskDeparture, unknown>,
        ]
      : []),
  ];

  return (
    <>
      <DataTable<DeskDeparture>
        searchable={false}
        tableId="b2b-desk-departures"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error as Error).message : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        emptyTitle="No departures in this window"
        emptyDescription="Departures are created on a trip plan (Catalog → Trip Plans). This list shows those starting from 14 days ago to 90 days ahead."
        tableMinWidth={canWrite ? 900 : 760}
      />
      <p className="mt-3 text-xs text-muted">
        Flagging tells every agency with seats on the departure (CRM banner and draft message). AA never messages travellers, and an
        incident carries only its type: no names, no free text.
      </p>
      <FlagModal departure={flagging} onClose={() => setFlagging(null)} onDone={refresh} />
    </>
  );
}

function FlagModal({ departure, onClose, onDone }: { departure: DeskDeparture | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [code, setCode] = useState<IncidentCode>("weather");
  const flag = useMutation({
    mutationFn: () => b2bDeskApi.flag(departure!.id, code),
    onSuccess: (inc) => {
      toast.success(`Incident flagged; ${inc.partners_notified ?? 0} agenc${inc.partners_notified === 1 ? "y" : "ies"} told`);
      onDone();
      onClose();
    },
  });
  return (
    <Modal
      open={!!departure}
      onClose={onClose}
      title="Flag incident on departure"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" disabled={flag.isPending} onClick={() => flag.mutate()}>
            Flag incident
          </Button>
        </>
      }
    >
      {departure && (
        <div className="flex flex-col gap-3 text-[13px] text-body">
          <p>
            {departure.tour_title ?? "This tour"}, departure <DateText value={departure.start_date} />. {departure.partners} agenc
            {departure.partners === 1 ? "y is" : "ies are"} on it and will see the incident in their CRM.
          </p>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Type</span>
            <select className={fieldCls} value={code} onChange={(e) => setCode(e.target.value as IncidentCode)}>
              {INCIDENT_CODES.map((c) => (
                <option key={c} value={c}>
                  {INCIDENT_LABEL[c]}
                </option>
              ))}
            </select>
          </label>
          {flag.isError && <p className="text-danger">{errorText(flag.error)}</p>}
        </div>
      )}
    </Modal>
  );
}
