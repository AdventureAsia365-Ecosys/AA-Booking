"use client";
// app/(staff)/pricing/seasons/_parts/SeasonsView.tsx — Pricing → Seasons (AA-1091, bk-053, root ADR 0005b d4). Every
// season calendar (one per destination, the sell-side seasons a tour uses, or per supplier contract) with its ranges,
// what waits for approval and how many tours use it. A calendar opens in a drawer: the seasons of a year as the price
// code sees them (festive ranges from holiday events resolved, stop-sale shown apart), its ranges with their state, the
// season of the other dates, and a form to propose a range (fixed dates, or a festive range that follows a holiday
// event with days before and after). Every change waits for a second person (buttons from ui/booking/priceVersions.ts;
// the API decides: 403 maker_checker, 409 range_overlaps). Own language switch (English default; messages.ts).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import {
  SEASON_CODES,
  seasonsApi,
  type ChangeAction,
  type RangeBody,
  type SeasonCalendar,
  type SeasonCode,
  type SeasonRange,
} from "@/lib/api/priceVersions";
import { suppliersApi } from "@/lib/api/suppliers";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  DateField,
  Drawer,
  Field,
  FormDrawer,
  I18nProvider,
  Input,
  KpiTile,
  LangSwitch,
  PageFrame,
  Pill,
  Section,
  Select,
  formatDay,
  langStore,
  useCan,
  useStaff,
  useT,
  useToast,
  type ColumnDef,
} from "@/ui";
import { CHANGE_TONE, SEASON_TONE, changeActions } from "@/ui/booking/priceVersions";
import { S, type SeasonMessage } from "./messages";

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

function SeasonPill({ code }: { code: string }) {
  const t = useT(S);
  return <Pill tone={SEASON_TONE[code] ?? "neutral"}>{t(`season_${code}` as SeasonMessage) as string}</Pill>;
}

/** The buttons of one range or holiday event in its state, with a reason dialog for a rejection. */
export function ChangeButtons({
  item,
  onAct,
  busy,
}: {
  item: { state: SeasonRange["state"]; created_by: string; end_requested_by: string | null };
  onAct: (action: ChangeAction, reason?: string) => void;
  busy: boolean;
}) {
  const t = useT(S);
  const me = useStaff().cis_user_id;
  const actions = changeActions(item, me, useCan("season.write"), useCan("season.approve"));
  const [rejecting, setRejecting] = useState(false);
  if (!actions.length) return null;
  return (
    <span className="flex flex-wrap gap-1.5">
      {actions.map((a) => (
        <Button
          key={a}
          size="sm"
          variant={a === "approve" || a === "end_approve" ? "primary" : "secondary"}
          disabled={busy}
          onClick={() => (a === "reject" ? setRejecting(true) : onAct(a))}
        >
          {t(`act_${a}` as SeasonMessage) as string}
        </Button>
      ))}
      <ConfirmDialog
        open={rejecting}
        onClose={() => setRejecting(false)}
        onConfirm={(reason) => {
          setRejecting(false);
          onAct("reject", reason);
        }}
        title={t("rejectTitle")}
        confirmLabel={t("act_reject")}
        reason={{ label: t("reasonLabel"), required: true, maxLength: 300 }}
        danger
      />
    </span>
  );
}

function RangeForm({ calendarId, onDone }: { calendarId: string; onDone: () => void }) {
  const t = useT(S);
  const toast = useToast();
  const qc = useQueryClient();
  const [kind, setKind] = useState<"season" | "stop_sale">("season");
  const [code, setCode] = useState<SeasonCode>("high");
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [byEvent, setByEvent] = useState(false);
  const [country, setCountry] = useState("");
  const [event, setEvent] = useState("");
  const [before, setBefore] = useState("0");
  const [after, setAfter] = useState("0");
  const [note, setNote] = useState("");
  const events = useQuery({ queryKey: ["holiday-events", "all"], queryFn: () => seasonsApi.events(), staleTime: 60_000 });
  const followEvent = kind === "season" && code === "festive" && byEvent;
  const propose = useMutation({
    mutationFn: () => {
      const body: RangeBody = followEvent
        ? {
            kind: "season",
            season_code: "festive",
            holiday_country: country,
            holiday_code: event,
            days_before: Number(before) || 0,
            days_after: Number(after) || 0,
            note: note.trim() || null,
          }
        : { kind, season_code: kind === "season" ? code : null, date_from: first, date_to: last, note: note.trim() || null };
      return seasonsApi.proposeRange(calendarId, body);
    },
    onSuccess: () => {
      toast.success(t("proposed"));
      qc.invalidateQueries({ queryKey: ["season-calendars"] });
      onDone();
    },
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  const codes = [...new Set((events.data?.items ?? []).filter((e) => !country || e.country_iso2 === country).map((e) => e.code))];
  return (
    <div className="flex flex-col gap-3 rounded-md border border-line p-3" data-testid="range-form">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label={t("kind")}>
          <Select value={kind} onChange={(e) => setKind(e.target.value as "season" | "stop_sale")}>
            <option value="season">{t("kind_season")}</option>
            <option value="stop_sale">{t("kind_stop_sale")}</option>
          </Select>
        </Field>
        {kind === "season" && (
          <Field label={t("season")}>
            <Select value={code} onChange={(e) => setCode(e.target.value as SeasonCode)}>
              {SEASON_CODES.map((c) => (
                <option key={c} value={c}>
                  {t(`season_${c}` as SeasonMessage) as string}
                </option>
              ))}
            </Select>
          </Field>
        )}
      </div>
      {kind === "season" && code === "festive" && (
        <label className="flex items-center gap-2 text-[13px] text-body">
          <Checkbox checked={byEvent} onChange={(e) => setByEvent(e.target.checked)} />
          {t("useEvent")}
        </label>
      )}
      {followEvent ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t("eventCountry")}>
            <Select value={country} onChange={(e) => setCountry(e.target.value)}>
              <option value="">—</option>
              {(events.data?.countries ?? []).map((c) => (
                <option key={c.iso2} value={c.iso2}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("eventCode")} hint={t("eventHint")}>
            <Input list="holiday-codes" value={event} onChange={(e) => setEvent(e.target.value.trim().toLowerCase())} />
            <datalist id="holiday-codes">
              {codes.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
          </Field>
          <Field label={t("daysBefore")}>
            <Input inputMode="numeric" value={before} onChange={(e) => setBefore(e.target.value)} />
          </Field>
          <Field label={t("daysAfter")}>
            <Input inputMode="numeric" value={after} onChange={(e) => setAfter(e.target.value)} />
          </Field>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t("firstDay")}>
            <DateField value={first} onChange={setFirst} aria-label={t("firstDay")} />
          </Field>
          <Field label={t("lastDay")}>
            <DateField value={last} min={first || undefined} onChange={setLast} aria-label={t("lastDay")} />
          </Field>
        </div>
      )}
      <Field label={t("note")}>
        <Input maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Button
        className="self-start"
        variant="primary"
        disabled={propose.isPending || (followEvent ? !country || !event : !first || !last)}
        onClick={() => propose.mutate()}
      >
        {t("propose")}
      </Button>
    </div>
  );
}

function CalendarDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT(S);
  const toast = useToast();
  const qc = useQueryClient();
  const me = useStaff().cis_user_id;
  const canWrite = useCan("season.write");
  const canApprove = useCan("season.approve");
  const [year, setYear] = useState(new Date().getFullYear());
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState<SeasonCode>("low");
  const q = useQuery({ queryKey: ["season-calendars", "one", id, year], queryFn: () => seasonsApi.calendar(id, year) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["season-calendars"] });
  const act = useMutation({
    mutationFn: ({ rangeId, action, reason }: { rangeId: string; action: ChangeAction; reason?: string }) =>
      seasonsApi.decideRange(rangeId, action, reason),
    onSuccess: () => {
      toast.success(t("done"));
      refresh();
    },
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  const def = useMutation({
    mutationFn: (what: "propose" | "approve" | "reject") =>
      what === "propose" ? seasonsApi.proposeDefault(id, code) : seasonsApi.decideDefault(id, what),
    onSuccess: () => {
      toast.success(t("done"));
      refresh();
    },
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  const d = q.data;
  const thisYear = new Date().getFullYear();
  return (
    <Drawer open onClose={onClose} title={d?.name ?? t("title")} width={720}>
      <div className="flex min-w-0 flex-col gap-4" data-testid="calendar-drawer">
        {q.isError && <p className="text-[13px] text-danger">{errorText(q.error, t("failed"))}</p>}
        {d && (
          <>
            <section className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-[13px] font-semibold text-ink">{t("seasonsOfYear", year)}</h3>
                <Select aria-label={t("year")} value={String(year)} onChange={(e) => setYear(Number(e.target.value))}>
                  {[thisYear - 1, thisYear, thisYear + 1, thisYear + 2].map((y) => (
                    <option key={y} value={y}>
                      {y}
                    </option>
                  ))}
                </Select>
              </div>
              <ul className="flex flex-col gap-1" data-testid="season-segments">
                {(d.segments ?? []).map((s) => (
                  <li key={s.from} className="flex flex-wrap items-center gap-2 text-[13px] text-body">
                    <SeasonPill code={s.season} />
                    {t("fromTo", formatDay(s.from), formatDay(s.to))}
                  </li>
                ))}
                {(d.stop_sales ?? []).map((s) => (
                  <li key={`stop-${s.from}`} className="flex flex-wrap items-center gap-2 text-[13px] text-body">
                    <Pill tone="danger">{t("stopSale")}</Pill>
                    {t("fromTo", formatDay(s.from), formatDay(s.to))}
                  </li>
                ))}
              </ul>
            </section>
            <section className="flex flex-col gap-2">
              <h3 className="text-[13px] font-semibold text-ink">{t("defaultSeason")}</h3>
              <span className="flex flex-wrap items-center gap-2">
                <SeasonPill code={d.default_season} />
                {d.pending_default_season && (
                  <span className="text-xs text-muted">
                    {t("defaultPending", t(`season_${d.pending_default_season}` as SeasonMessage) as string, d.default_requested_by ?? "")}
                  </span>
                )}
              </span>
              {d.pending_default_season
                ? canApprove &&
                  d.default_requested_by !== me && (
                    <span className="flex flex-wrap gap-1.5">
                      <Button size="sm" variant="primary" disabled={def.isPending} onClick={() => def.mutate("approve")}>
                        {t("approveDefault")}
                      </Button>
                      <Button size="sm" variant="secondary" disabled={def.isPending} onClick={() => def.mutate("reject")}>
                        {t("rejectDefault")}
                      </Button>
                    </span>
                  )
                : canWrite && (
                    <span className="flex flex-wrap items-center gap-2">
                      <Select aria-label={t("defaultSeason")} value={code} onChange={(e) => setCode(e.target.value as SeasonCode)}>
                        {SEASON_CODES.map((c) => (
                          <option key={c} value={c}>
                            {t(`season_${c}` as SeasonMessage) as string}
                          </option>
                        ))}
                      </Select>
                      <Button size="sm" variant="secondary" disabled={def.isPending || code === d.default_season} onClick={() => def.mutate("propose")}>
                        {t("proposeDefault")}
                      </Button>
                    </span>
                  )}
            </section>
            <section className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-[13px] font-semibold text-ink">{t("ranges")}</h3>
                {canWrite && !adding && (
                  <Button size="sm" variant="dashed" onClick={() => setAdding(true)}>
                    <Plus size={13} /> {t("addRange")}
                  </Button>
                )}
              </div>
              {adding && <RangeForm calendarId={id} onDone={() => setAdding(false)} />}
              {!d.ranges.length && <p className="text-[13px] text-muted">{t("noRanges")}</p>}
              <ul className="flex flex-col gap-2">
                {d.ranges.map((r) => (
                  <li key={r.id} className="flex min-w-0 flex-col gap-1.5 rounded-md border border-line px-3 py-2.5" data-testid="season-range">
                    <span className="flex min-w-0 flex-wrap items-center gap-2 text-[13px]">
                      {r.kind === "stop_sale" ? <Pill tone="danger">{t("stopSale")}</Pill> : <SeasonPill code={r.season_code ?? "low"} />}
                      <span className="min-w-0 break-words text-body">
                        {r.holiday_code
                          ? t("eventRange", r.holiday_country ?? "", r.holiday_code, r.days_before, r.days_after)
                          : t("fromTo", formatDay(r.date_from), formatDay(r.date_to))}
                      </span>
                      <Pill tone={CHANGE_TONE[r.state] ?? "neutral"}>{t(`state_${r.state}` as SeasonMessage) as string}</Pill>
                    </span>
                    <span className="text-xs text-muted">
                      {t("byWho", r.created_by)}
                      {r.note ? ` · ${r.note}` : ""}
                      {r.reject_reason ? ` · ${r.reject_reason}` : ""}
                    </span>
                    <ChangeButtons item={r} busy={act.isPending} onAct={(action, reason) => act.mutate({ rangeId: r.id, action, reason })} />
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </div>
    </Drawer>
  );
}

function NewCalendarDrawer({ onClose, onMade }: { onClose: () => void; onMade: (id: string) => void }) {
  const t = useT(S);
  const toast = useToast();
  const qc = useQueryClient();
  const [kind, setKind] = useState<"destination" | "supplier_contract">("destination");
  const [search, setSearch] = useState("");
  const [destination, setDestination] = useState("");
  const [contract, setContract] = useState("");
  const [name, setName] = useState("");
  const places = useQuery({
    queryKey: ["catalog-places", search],
    queryFn: () => suppliersApi.places({ q: search || undefined, limit: 50 }),
    enabled: kind === "destination",
    staleTime: 60_000,
  });
  const create = useMutation({
    mutationFn: () =>
      seasonsApi.create({
        owner_kind: kind,
        destination_id: kind === "destination" ? destination : null,
        supplier_contract_id: kind === "supplier_contract" ? contract.trim() : null,
        name: name.trim(),
      }),
    onSuccess: (c) => {
      toast.success(t("created"));
      qc.invalidateQueries({ queryKey: ["season-calendars"] });
      onMade(c.id);
    },
  });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("newCalendar")}
      submitLabel={t("create")}
      busy={create.isPending}
      error={create.error ? errorText(create.error, t("failed")) : null}
      onSubmit={() => create.mutate()}
    >
      <Field label={t("ownerKind")}>
        <Select value={kind} onChange={(e) => setKind(e.target.value as "destination" | "supplier_contract")}>
          <option value="destination">{t("owner_destination")}</option>
          <option value="supplier_contract">{t("owner_supplier_contract")}</option>
        </Select>
      </Field>
      {kind === "destination" ? (
        <>
          <Field label={t("pickDestination")}>
            <Input value={search} onChange={(e) => setSearch(e.target.value)} />
          </Field>
          <Field label={t("destination")}>
            <Select
              value={destination}
              onChange={(e) => {
                setDestination(e.target.value);
                const place = places.data?.items.find((p) => p.destination_id === e.target.value);
                if (place && !name) setName(place.name);
              }}
            >
              <option value="">—</option>
              {(places.data?.items ?? []).map((p) => (
                <option key={p.destination_id} value={p.destination_id}>
                  {p.name} · {p.country}
                </option>
              ))}
            </Select>
          </Field>
        </>
      ) : (
        <Field label={t("contractId")}>
          <Input value={contract} onChange={(e) => setContract(e.target.value)} />
        </Field>
      )}
      <Field label={t("name")} required>
        <Input maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
    </FormDrawer>
  );
}

function Seasons() {
  const t = useT(S);
  const canWrite = useCan("season.write");
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const list = useQuery({ queryKey: ["season-calendars", "all"], queryFn: () => seasonsApi.calendars() });
  const items = list.data?.items ?? [];
  const columns: ColumnDef<SeasonCalendar, unknown>[] = [
    { id: "name", header: t("colName"), accessorFn: (r) => r.name, cell: ({ row }) => <span className="break-words font-medium text-ink">{row.original.name}</span> },
    {
      id: "owner",
      header: t("colOwner"),
      accessorFn: (r) => r.owner_kind,
      cell: ({ row }) => t(`owner_${row.original.owner_kind}` as SeasonMessage) as string,
    },
    { id: "default", header: t("colDefault"), accessorFn: (r) => r.default_season, cell: ({ row }) => <SeasonPill code={row.original.default_season} /> },
    { id: "ranges", header: t("colRanges"), accessorFn: (r) => r.ranges },
    { id: "waiting", header: t("colWaiting"), accessorFn: (r) => r.waiting + (r.pending_default_season ? 1 : 0) },
    { id: "plans", header: t("colPlans"), accessorFn: (r) => r.plans },
  ];
  return (
    <PageFrame
      title={t("title")}
      description={t("description")}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <LangSwitch ariaLabel={t("language")} />
          {canWrite && (
            <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
              <Plus size={13} /> {t("newCalendar")}
            </Button>
          )}
        </div>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <KpiTile label={t("kpiCalendars")} value={list.data ? items.length : undefined} />
        <KpiTile
          label={t("kpiWaiting")}
          value={list.data ? items.reduce((n, c) => n + c.waiting + (c.pending_default_season ? 1 : 0), 0) : undefined}
          hintTone="warning"
        />
        <KpiTile label={t("kpiPlans")} value={list.data ? items.reduce((n, c) => n + c.plans, 0) : undefined} />
      </div>
      <Section>
        <DataTable<SeasonCalendar>
          tableId="season-calendars"
          data={items}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? (list.error as Error).message : null}
          onRetry={() => list.refetch()}
          onRowClick={(r) => setOpen(r.id)}
          emptyTitle={t("emptyTitle")}
          emptyDescription={t("emptyHint")}
          tableMinWidth={640}
        />
      </Section>
      {open && <CalendarDrawer id={open} onClose={() => setOpen(null)} />}
      {creating && (
        <NewCalendarDrawer
          onClose={() => setCreating(false)}
          onMade={(id) => {
            setCreating(false);
            setOpen(id);
          }}
        />
      )}
    </PageFrame>
  );
}

/** The page in its own language (English by default; the switch keeps the choice in the `bk_lang` cookie). */
export function SeasonsView() {
  return (
    <I18nProvider store={langStore}>
      <Seasons />
    </I18nProvider>
  );
}
