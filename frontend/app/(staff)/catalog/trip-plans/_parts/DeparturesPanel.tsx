"use client";
// app/(staff)/catalog/trip-plans/_parts/DeparturesPanel.tsx — the "Departures" tab of a trip plan (AA-973 / AA-1073,
// bk-020 / bk-046). The list: start date, status (and At risk when the watch found it short of its minimum on its
// decision date), seats (sold, held, free, kept for agency allotments), the minimum to operate, when sales close and
// the decision date. "Add departure" makes one; "Generate departures" makes a weekly or monthly series with a preview
// (dry run) before it saves. A row opens the departure drawer (?departure=<id>): its rules (capacity, minimum, cutoff,
// decision days, auto-confirm, status) and its agency allotments (propose, approve by another person, reject or
// withdraw). Writes: trip_plan.write (admin, contracting) for departures, b2b_desk.write for allotments.
// AA-1073 rest (bk-048): an allotment in force or released offers "Charge fee" (allotted seats not released, approved
// by a second person); the drawer links to the departure board.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CalendarPlus, Plus, Repeat } from "lucide-react";
import { ApiError, type FieldError } from "@/lib/api/client";
import {
  ALLOTMENT_STATE_LABEL,
  END_REASON_LABEL,
  WEEKDAYS,
  departuresApi,
  minimumLabel,
  type Allotment,
  type Departure,
  type DepartureRules,
  type DepartureStatus,
  type GenerateRequest,
  type GenerateResult,
} from "@/lib/api/departures";
import {
  Button,
  Checkbox,
  DataTable,
  DateField,
  DateText,
  Drawer,
  FormDrawer,
  Input,
  Pill,
  Section,
  Select,
  formatDay,
  useCan,
  useStaff,
  useToast,
  useUrlFilters,
} from "@/ui";
import { FormField, errorFor, inputCls } from "../../../_parts/FormField";
import { ChargeFeeModal } from "../../../departures/_parts/ChargeFeeModal";

const STATUS: Record<DepartureStatus, { label: string; tone: "neutral" | "success" | "danger" }> = {
  scheduled: { label: "Scheduled", tone: "neutral" },
  guaranteed: { label: "Guaranteed", tone: "success" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

const DEFAULT_RULES: DepartureRules = {
  capacity: 16,
  min_to_operate: 0,
  sales_cutoff_days: 0,
  decision_days_before: 21,
  auto_confirm: true,
};

function errorText(e: unknown, fallback = "Something went wrong"): string {
  return e instanceof ApiError ? e.message : fallback;
}

function fieldErrors(e: unknown): FieldError[] {
  return e instanceof ApiError ? e.fieldErrors : [];
}

function toInt(value: string): number {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) ? n : 0;
}

/** "YYYY-MM-DD" of today + days (local calendar; the API checks against UTC). */
function dayFromToday(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function DepartureStatusPills({ d }: { d: Pick<Departure, "status" | "at_risk"> }) {
  return (
    <span className="flex flex-wrap gap-1">
      <Pill tone={STATUS[d.status].tone}>{STATUS[d.status].label}</Pill>
      {d.at_risk && <Pill tone="warning">At risk</Pill>}
    </span>
  );
}

const COLUMNS: ColumnDef<Departure, unknown>[] = [
  {
    id: "start",
    header: "Departure",
    accessorFn: (r) => r.start_date,
    cell: ({ row }) => <DateText value={row.original.start_date} className="font-medium text-ink" />,
  },
  { id: "status", header: "Status", accessorFn: (r) => r.status, cell: ({ row }) => <DepartureStatusPills d={row.original} /> },
  {
    id: "seats",
    header: "Seats",
    accessorFn: (r) => r.sold,
    cell: ({ row }) => {
      const d = row.original;
      return (
        <span className="flex flex-col">
          <span className="bk-tabular text-[13px] text-body">
            {d.sold} sold · {d.held} held · {d.available} free / {d.capacity}
          </span>
          {d.allotted > 0 && (
            <span className="text-xs text-muted">
              {d.allotted} in agency allotments ({d.allotment_unused} unused)
            </span>
          )}
        </span>
      );
    },
  },
  {
    id: "minimum",
    header: "Minimum",
    accessorFn: (r) => r.min_to_operate,
    cell: ({ row }) => <span className="text-[13px] text-body">{minimumLabel(row.original) ?? <span className="text-muted2">None</span>}</span>,
  },
  {
    id: "sales",
    header: "Sales close",
    accessorFn: (r) => r.sales_close_date,
    cell: ({ row }) => <DateText value={row.original.sales_close_date} className="text-[13px]" />,
  },
  {
    id: "decision",
    header: "Decision",
    accessorFn: (r) => r.decision_date,
    cell: ({ row }) =>
      row.original.min_to_operate ? <DateText value={row.original.decision_date} className="text-[13px]" /> : <span className="text-muted2">—</span>,
  },
  {
    id: "auto",
    header: "Confirm",
    accessorFn: (r) => r.auto_confirm,
    cell: ({ row }) => <span className="text-[13px] text-body">{row.original.auto_confirm ? "Instant" : "On request"}</span>,
  },
];

export function DeparturesPanel({ sourceId }: { sourceId: string }) {
  const canWrite = useCan("trip_plan.write");
  const { get, setMany } = useUrlFilters();
  const [adding, setAdding] = useState(false);
  const [generating, setGenerating] = useState(false);
  const list = useQuery({ queryKey: ["departures", sourceId], queryFn: () => departuresApi.list(sourceId) });
  const open = get("departure");
  const current = list.data?.items.find((d) => d.id === open) ?? null;
  return (
    <Section
      title="Departures"
      description="Fixed departures agencies can quote and hold. Sales close on the start date minus the cutoff; on the decision date a departure short of its minimum is flagged at risk on the B2B desk."
      actions={
        canWrite && (
          <span className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => setGenerating(true)}>
              <Repeat size={14} /> Generate departures
            </Button>
            <Button size="sm" variant="primary" onClick={() => setAdding(true)}>
              <Plus size={14} /> Add departure
            </Button>
          </span>
        )
      }
    >
      <DataTable<Departure>
        searchable={false}
        tableId="trip-plan-departures"
        data={list.data?.items ?? []}
        columns={COLUMNS}
        getRowId={(r) => r.id}
        onRowClick={(r) => setMany({ departure: r.id })}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error, "Could not load the departures") : null}
        onRetry={() => list.refetch()}
        emptyTitle="No departures yet"
        emptyDescription={canWrite ? "Add one, or generate a weekly or monthly series." : "Contracting adds the departures of a plan."}
        tableMinWidth={820}
      />
      {adding && <NewDepartureDrawer sourceId={sourceId} onClose={() => setAdding(false)} />}
      {generating && <GenerateDrawer sourceId={sourceId} onClose={() => setGenerating(false)} />}
      {current && <DepartureDrawer departure={current} sourceId={sourceId} onClose={() => setMany({ departure: null })} />}
    </Section>
  );
}

function RulesFields({
  rules,
  onChange,
  errors,
}: {
  rules: DepartureRules;
  onChange: (rules: DepartureRules) => void;
  errors: FieldError[];
}) {
  return (
    <>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="Seats" required error={errorFor(errors, "capacity")}>
          <Input
            className={inputCls}
            type="number"
            min={0}
            max={1000}
            value={rules.capacity}
            onChange={(e) => onChange({ ...rules, capacity: toInt(e.target.value) })}
          />
        </FormField>
        <FormField label="Minimum to operate" hint="Sold seats it needs to run; 0 = no minimum." error={errorFor(errors, "min_to_operate")}>
          <Input
            className={inputCls}
            type="number"
            min={0}
            max={rules.capacity}
            value={rules.min_to_operate}
            onChange={(e) => onChange({ ...rules, min_to_operate: toInt(e.target.value) })}
          />
        </FormField>
        <FormField label="Sales cutoff (days)" hint="Sales close this many days before the start." error={errorFor(errors, "sales_cutoff_days")}>
          <Input
            className={inputCls}
            type="number"
            min={0}
            max={90}
            value={rules.sales_cutoff_days}
            onChange={(e) => onChange({ ...rules, sales_cutoff_days: toInt(e.target.value) })}
          />
        </FormField>
        <FormField label="Decision (days before)" hint="Go or cancel on this day if the minimum is not met." error={errorFor(errors, "decision_days_before")}>
          <Input
            className={inputCls}
            type="number"
            min={0}
            max={365}
            value={rules.decision_days_before}
            onChange={(e) => onChange({ ...rules, decision_days_before: toInt(e.target.value) })}
          />
        </FormField>
      </div>
      <Checkbox
        label="Confirm agency bookings at once (otherwise AA ops confirm each line on the B2B desk)"
        checked={rules.auto_confirm}
        onChange={(e) => onChange({ ...rules, auto_confirm: e.target.checked })}
      />
    </>
  );
}

function NewDepartureDrawer({ sourceId, onClose }: { sourceId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [start, setStart] = useState(dayFromToday(30));
  const [rules, setRules] = useState<DepartureRules>(DEFAULT_RULES);
  const save = useMutation({
    mutationFn: () => departuresApi.create(sourceId, { start_date: start, ...rules }),
    onSuccess: (d) => {
      toast.success(`Departure ${formatDay(d.start_date)} added`);
      qc.invalidateQueries({ queryKey: ["departures", sourceId] });
      onClose();
    },
  });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Add departure"
      onSubmit={() => save.mutate()}
      busy={save.isPending}
      dirty
      error={save.isError ? errorText(save.error) : null}
      fieldErrors={fieldErrors(save.error)}
    >
      <FormField label="Start date" required error={errorFor(fieldErrors(save.error), "start_date")}>
        <DateField value={start} onChange={setStart} />
      </FormField>
      <RulesFields rules={rules} onChange={setRules} errors={fieldErrors(save.error)} />
    </FormDrawer>
  );
}

function GenerateDrawer({ sourceId, onClose }: { sourceId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [from, setFrom] = useState(dayFromToday(30));
  const [to, setTo] = useState(dayFromToday(120));
  const [kind, setKind] = useState<"weekly" | "monthly">("weekly");
  const [weekdays, setWeekdays] = useState<number[]>([6]);
  const [monthDay, setMonthDay] = useState(1);
  const [rules, setRules] = useState<DepartureRules>(DEFAULT_RULES);
  const [preview, setPreview] = useState<GenerateResult | null>(null);
  const body = (dryRun: boolean): GenerateRequest => ({
    date_from: from,
    date_to: to,
    pattern: kind === "weekly" ? { kind, weekdays } : { kind, day: monthDay },
    ...rules,
    dry_run: dryRun,
  });
  const run = useMutation({
    mutationFn: (dryRun: boolean) => departuresApi.generate(sourceId, body(dryRun)),
    onSuccess: (out) => {
      if (out.dry_run) {
        setPreview(out);
        return;
      }
      toast.success(`${out.created_count} departures created${out.skipped_count ? `, ${out.skipped_count} dates already had one` : ""}`);
      qc.invalidateQueries({ queryKey: ["departures", sourceId] });
      onClose();
    },
  });
  const changed = () => setPreview(null);
  const errors = fieldErrors(run.error);
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Generate departures"
      width={560}
      onSubmit={() => run.mutate(preview === null)}
      submitLabel={preview === null ? "Preview dates" : `Create ${preview.created_count} departures`}
      busy={run.isPending}
      dirty
      error={run.isError ? errorText(run.error) : null}
      fieldErrors={errors}
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="From" required error={errorFor(errors, "date_from")}>
          <DateField
            value={from}
            onChange={(v) => {
              setFrom(v);
              changed();
            }}
          />
        </FormField>
        <FormField label="To" required hint="At most 400 days after From." error={errorFor(errors, "date_to")}>
          <DateField
            value={to}
            onChange={(v) => {
              setTo(v);
              changed();
            }}
          />
        </FormField>
      </div>
      <FormField label="Repeat">
        <Select
          className={inputCls}
          value={kind}
          onChange={(e) => {
            setKind(e.target.value as "weekly" | "monthly");
            changed();
          }}
        >
          <option value="weekly">Every week on</option>
          <option value="monthly">Every month on day</option>
        </Select>
      </FormField>
      {kind === "weekly" ? (
        <fieldset className="flex flex-wrap gap-3" aria-label="Weekdays">
          {WEEKDAYS.map((w) => (
            <Checkbox
              key={w.value}
              label={w.label}
              checked={weekdays.includes(w.value)}
              onChange={(e) => {
                setWeekdays(e.target.checked ? [...weekdays, w.value].sort() : weekdays.filter((x) => x !== w.value));
                changed();
              }}
            />
          ))}
        </fieldset>
      ) : (
        <FormField label="Day of the month" hint="A month without this day is skipped.">
          <Input
            className={inputCls}
            type="number"
            min={1}
            max={31}
            value={monthDay}
            onChange={(e) => {
              setMonthDay(toInt(e.target.value));
              changed();
            }}
          />
        </FormField>
      )}
      <RulesFields
        rules={rules}
        onChange={(r) => {
          setRules(r);
          changed();
        }}
        errors={errors}
      />
      {preview && (
        <div className="rounded-md border border-line2 px-3 py-2 text-[13px]" data-testid="generate-preview">
          <p className="font-medium text-ink">
            {preview.created_count} new departure{preview.created_count === 1 ? "" : "s"}
            {preview.skipped_count > 0 && <span className="font-normal text-muted"> · {preview.skipped_count} dates already have one</span>}
          </p>
          <p className="mt-1 text-xs text-muted">{preview.created.map((d) => formatDay(d)).join(", ") || "Nothing new to create."}</p>
        </div>
      )}
    </FormDrawer>
  );
}

function DepartureDrawer({ departure, sourceId, onClose }: { departure: Departure; sourceId: string; onClose: () => void }) {
  const canWrite = useCan("trip_plan.write");
  const canDesk = useCan("b2b_desk.read");
  const qc = useQueryClient();
  const toast = useToast();
  const [rules, setRules] = useState<DepartureRules>({
    capacity: departure.capacity,
    min_to_operate: departure.min_to_operate,
    sales_cutoff_days: departure.sales_cutoff_days,
    decision_days_before: departure.decision_days_before,
    auto_confirm: departure.auto_confirm,
  });
  const [status, setStatus] = useState<DepartureStatus>(departure.status);
  const cancelled = departure.status === "cancelled";
  const save = useMutation({
    mutationFn: () => departuresApi.update(departure.id, { ...rules, status }),
    onSuccess: () => {
      toast.success("Departure saved");
      qc.invalidateQueries({ queryKey: ["departures", sourceId] });
      qc.invalidateQueries({ queryKey: ["allotments", departure.id] });
    },
  });
  return (
    <Drawer
      open
      onClose={onClose}
      width={600}
      title={`Departure ${formatDay(departure.start_date)}`}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          {canWrite && !cancelled && (
            <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "Saving…" : "Save departure"}
            </Button>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-5 font-sans">
        <div className="flex flex-wrap items-center gap-3 text-[13px] text-body">
          <DepartureStatusPills d={departure} />
          <span className="bk-tabular">
            {departure.sold} sold · {departure.held} held · {departure.available} free / {departure.capacity}
          </span>
          {departure.min_to_operate > 0 && (
            <span className="text-muted">
              Decision <DateText value={departure.decision_date} /> · sales close <DateText value={departure.sales_close_date} />
            </span>
          )}
        </div>
        {departure.at_risk && (
          <p role="status" className="rounded-md border border-line2 bg-warning-soft px-3 py-2 text-[13px] text-body">
            Short of its minimum on the decision date ({departure.sold} of {departure.min_to_operate} sold). Run it anyway (status Guaranteed) or
            cancel it; the agencies on it were told it is at risk.
          </p>
        )}
        {canWrite && !cancelled ? (
          <div className="flex flex-col gap-4">
            <RulesFields rules={rules} onChange={setRules} errors={fieldErrors(save.error)} />
            <FormField label="Status" hint="Cancelling is final: agencies are told and their allotments end.">
              <Select className={inputCls} value={status} onChange={(e) => setStatus(e.target.value as DepartureStatus)}>
                <option value="scheduled">Scheduled</option>
                <option value="guaranteed">Guaranteed</option>
                <option value="cancelled">Cancelled</option>
              </Select>
            </FormField>
            {save.isError && (
              <p role="alert" className="text-[13px] text-danger">
                {errorText(save.error)}
              </p>
            )}
          </div>
        ) : (
          <p className="text-[13px] text-muted">
            Minimum {departure.min_to_operate || "none"} · cutoff {departure.sales_cutoff_days} days · decision {departure.decision_days_before} days before ·{" "}
            {departure.auto_confirm ? "instant confirm" : "on request"}
          </p>
        )}
        {canDesk && <AllotmentsSection departure={departure} sourceId={sourceId} />}
      </div>
    </Drawer>
  );
}

function AllotmentsSection({ departure, sourceId }: { departure: Departure; sourceId: string }) {
  const canWrite = useCan("b2b_desk.write");
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["allotments", departure.id], queryFn: () => departuresApi.allotments(departure.id) });
  const [partner, setPartner] = useState("");
  const [charging, setCharging] = useState<Allotment | null>(null);
  const [seats, setSeats] = useState(4);
  const [release, setRelease] = useState(14);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["allotments", departure.id] });
    qc.invalidateQueries({ queryKey: ["departures", sourceId] });
  };
  const propose = useMutation({
    mutationFn: () => departuresApi.proposeAllotment(departure.id, { partner_id: partner, seats, release_days_before: release }),
    onSuccess: () => {
      toast.success("Allotment proposed; another person approves it");
      setPartner("");
      refresh();
    },
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "approve" | "end" }) =>
      action === "approve" ? departuresApi.approveAllotment(id) : departuresApi.endAllotment(id),
    onSuccess: (a) => {
      toast.success(a.state === "in_force" ? "Allotment approved" : `Allotment ${a.end_reason ?? "ended"}`);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const items = list.data?.items ?? [];
  const cancelled = departure.status === "cancelled";
  return (
    <section className="flex flex-col gap-3 border-t border-line2 pt-4" aria-label="Agency allotments">
      <div>
        <h3 className="text-[14px] font-semibold text-ink">Agency allotments</h3>
        <p className="text-xs text-muted">
          Seats kept for one agency until the release date; unused seats then go back on sale. A second person approves each one.
        </p>
      </div>
      {list.isLoading ? (
        <p className="text-[13px] text-muted">Loading…</p>
      ) : list.isError ? (
        <p className="text-[13px] text-danger">{errorText(list.error, "Could not load the allotments")}</p>
      ) : items.length === 0 ? (
        <p className="text-[13px] text-muted2">No allotment on this departure.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line2" data-testid="allotments">
          {items.map((a) => (
            <AllotmentRow
              key={a.id}
              a={a}
              canWrite={canWrite && !cancelled}
              isMaker={a.created_by?.id === me.cis_user_id}
              busy={act.isPending}
              onAct={(action) => act.mutate({ id: a.id, action })}
              onCharge={canWrite ? () => setCharging(a) : undefined}
            />
          ))}
        </ul>
      )}
      {canWrite && !cancelled && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)]">
          <FormField label="Agency" error={errorFor(fieldErrors(propose.error), "partner_id")}>
            <Select className={inputCls} value={partner} onChange={(e) => setPartner(e.target.value)}>
              <option value="">Choose an agency</option>
              {(list.data?.partner_options ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.agency_name}
                </option>
              ))}
            </Select>
          </FormField>
          <FormField label="Seats" error={errorFor(fieldErrors(propose.error), "seats")}>
            <Input className={inputCls} type="number" min={1} max={departure.capacity} value={seats} onChange={(e) => setSeats(toInt(e.target.value))} />
          </FormField>
          <FormField label="Release (days before)" error={errorFor(fieldErrors(propose.error), "release_days_before")}>
            <Input className={inputCls} type="number" min={0} max={365} value={release} onChange={(e) => setRelease(toInt(e.target.value))} />
          </FormField>
          <div className="sm:col-span-3 flex flex-wrap items-center gap-3">
            <Button size="sm" disabled={!partner || propose.isPending} onClick={() => propose.mutate()}>
              <CalendarPlus size={14} /> Propose allotment
            </Button>
            {propose.isError && <span className="text-[13px] text-danger">{errorText(propose.error)}</span>}
          </div>
        </div>
      )}
      {charging && (
        <ChargeFeeModal
          departureId={departure.id}
          title={`Departure ${formatDay(departure.start_date)}: allotment of ${charging.seats} seats for ${charging.partner.agency_name}`}
          reason="allotment_unreleased"
          partnerId={charging.partner.id}
          seats={charging.unused ?? undefined}
          onClose={() => setCharging(null)}
        />
      )}
    </section>
  );
}

function AllotmentRow({
  a,
  canWrite,
  isMaker,
  busy,
  onAct,
  onCharge,
}: {
  a: Allotment;
  canWrite: boolean;
  isMaker: boolean;
  busy: boolean;
  onAct: (action: "approve" | "end") => void;
  /** AA-1073 rest: propose a fee for allotted seats not released (in force or released allotments). */
  onCharge?: () => void;
}) {
  const tone = a.state === "in_force" ? "success" : a.state === "pending" ? "warning" : "neutral";
  return (
    <li className="flex flex-wrap items-start justify-between gap-3 py-2 first:pt-0 last:pb-0">
      <span className="flex min-w-0 flex-col text-[13px]">
        <span className="font-medium text-ink">
          {a.partner.agency_name} · {a.seats} seats
        </span>
        <span className="text-xs text-muted">
          Release <DateText value={a.release_date} />
          {a.state === "in_force" && a.unused !== null && ` · ${a.used ?? 0} used, ${a.unused} kept`}
          {a.created_by?.name && ` · proposed by ${a.created_by.name}`}
          {a.end_reason && ` · ${END_REASON_LABEL[a.end_reason]}`}
        </span>
      </span>
      <span className="flex flex-wrap items-center gap-2">
        <Pill tone={tone}>{ALLOTMENT_STATE_LABEL[a.state]}</Pill>
        {canWrite && a.state === "pending" && (
          <Button size="sm" variant="primary" disabled={busy || isMaker} title={isMaker ? "You proposed it: another person approves it" : undefined} onClick={() => onAct("approve")}>
            Approve
          </Button>
        )}
        {canWrite && (a.state === "pending" || a.state === "in_force") && (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAct("end")}>
            {a.state === "pending" ? "Reject" : "Withdraw"}
          </Button>
        )}
        {onCharge && (a.state === "in_force" || a.state === "released") && (
          <Button size="sm" variant="ghost" onClick={onCharge}>
            Charge fee
          </Button>
        )}
      </span>
    </li>
  );
}
