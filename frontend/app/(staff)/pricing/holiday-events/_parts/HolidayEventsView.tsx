"use client";
// app/(staff)/pricing/holiday-events/_parts/HolidayEventsView.tsx — Pricing → Holiday events (AA-1091, bk-054, root
// ADR 0005b d4). Holidays per country and year, entered as data each year (no holiday date is ever written in code):
// filters by country and year, each event with its dates, its state and how many season calendars follow it, the
// maker-checker buttons (ui/booking/priceVersions.ts; the API decides), and a drawer to propose a new one (the code is
// made from the name and stays the same every year). Text in ../../seasons/_parts/messages.ts (one catalogue for the
// two season pages); own language switch.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { seasonsApi, type ChangeAction, type HolidayEvent } from "@/lib/api/priceVersions";
import {
  Button,
  DataTable,
  DateField,
  Field,
  FilterBar,
  FormDrawer,
  I18nProvider,
  Input,
  LangSwitch,
  PageFrame,
  Pill,
  Section,
  Select,
  formatDay,
  langStore,
  useCan,
  useT,
  useToast,
  useUrlFilters,
  type ColumnDef,
} from "@/ui";
import { CHANGE_TONE, eventCode } from "@/ui/booking/priceVersions";
import { ChangeButtons, useWho } from "../../seasons/_parts/SeasonsView";
import { S, type SeasonMessage } from "../../seasons/_parts/messages";

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

function NewEventDrawer({ countries, onClose }: { countries: { iso2: string; name: string }[]; onClose: () => void }) {
  const t = useT(S);
  const toast = useToast();
  const qc = useQueryClient();
  const [country, setCountry] = useState("");
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [reason, setReason] = useState("");
  const propose = useMutation({
    mutationFn: () =>
      seasonsApi.proposeEvent({
        country_iso2: country,
        code: code || eventCode(name),
        name: name.trim(),
        date_from: first,
        date_to: last,
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success(t("proposed"));
      qc.invalidateQueries({ queryKey: ["holiday-events"] });
      onClose();
    },
  });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("newEvent")}
      submitLabel={t("propose")}
      busy={propose.isPending}
      error={propose.error ? errorText(propose.error, t("failed")) : null}
      onSubmit={() => propose.mutate()}
    >
      <Field label={t("country")} required>
        <Select value={country} onChange={(e) => setCountry(e.target.value)}>
          <option value="">—</option>
          {countries.map((c) => (
            <option key={c.iso2} value={c.iso2}>
              {c.name}
            </option>
          ))}
        </Select>
      </Field>
      <Field label={t("eventName")} required>
        <Input maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label={t("eventCode")} hint={t("eventCodeHint")}>
        <Input maxLength={40} value={code} placeholder={name ? eventCode(name) : ""} onChange={(e) => setCode(e.target.value.trim().toLowerCase())} />
      </Field>
      <Field label={t("firstDay")} required>
        <DateField value={first} onChange={setFirst} aria-label={t("firstDay")} />
      </Field>
      <Field label={t("lastDay")} required>
        <DateField value={last} min={first || undefined} onChange={setLast} aria-label={t("lastDay")} />
      </Field>
      <Field label={t("reasonLabel")} hint={t("reasonBody")} required>
        <Input maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
    </FormDrawer>
  );
}

function HolidayEvents() {
  const t = useT(S);
  const toast = useToast();
  const qc = useQueryClient();
  const canWrite = useCan("season.write");
  const who = useWho();
  const { get } = useUrlFilters();
  const country = get("country") ?? undefined;
  const year = get("year") ? Number(get("year")) : undefined;
  const [creating, setCreating] = useState(false);
  const list = useQuery({ queryKey: ["holiday-events", country, year], queryFn: () => seasonsApi.events(country, year) });
  const act = useMutation({
    mutationFn: ({ id, action, reason }: { id: string; action: ChangeAction; reason?: string }) =>
      seasonsApi.decideEvent(id, action, reason),
    onSuccess: () => {
      toast.success(t("done"));
      qc.invalidateQueries({ queryKey: ["holiday-events"] });
    },
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  const thisYear = new Date().getFullYear();
  const columns: ColumnDef<HolidayEvent, unknown>[] = [
    { id: "country", header: t("colCountry"), accessorFn: (r) => r.country_iso2 },
    {
      id: "event",
      header: t("colEvent"),
      accessorFn: (r) => r.name,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="break-words font-medium text-ink">{row.original.name}</span>
          <span className="text-xs text-muted">{row.original.code}</span>
        </span>
      ),
    },
    { id: "year", header: t("colYear"), accessorFn: (r) => r.year },
    {
      id: "dates",
      header: t("colDates"),
      accessorFn: (r) => r.date_from,
      cell: ({ row }) => t("fromTo", formatDay(row.original.date_from), formatDay(row.original.date_to)),
    },
    {
      id: "state",
      header: t("colState"),
      accessorFn: (r) => r.state,
      cell: ({ row }) => (
        <span className="flex flex-col items-start gap-1.5">
          <Pill tone={CHANGE_TONE[row.original.state] ?? "neutral"}>{t(`state_${row.original.state}` as SeasonMessage) as string}</Pill>
          <ChangeButtons
            item={row.original}
            busy={act.isPending}
            onAct={(action, reason) => act.mutate({ id: row.original.id, action, reason })}
          />
        </span>
      ),
    },
    {
      id: "by",
      header: t("colBy"),
      accessorFn: (r) => who(r.created_by),
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col gap-0.5 text-xs">
          <span className="text-ink">{who(row.original.created_by)}</span>
          <span className="break-words text-muted">{row.original.reason}</span>
          {row.original.reject_reason && <span className="break-words text-danger">{row.original.reject_reason}</span>}
          {row.original.withdraw_reason && (
            <span className="break-words text-muted">{t("withdrawnLine", row.original.withdraw_reason)}</span>
          )}
          {row.original.end_requested_by && row.original.end_reason && (
            <span className="break-words text-muted">{t("endLine", who(row.original.end_requested_by), row.original.end_reason)}</span>
          )}
        </span>
      ),
    },
    { id: "calendars", header: t("colCalendars"), accessorFn: (r) => r.calendars },
  ];
  return (
    <PageFrame
      title={t("eventsTitle")}
      description={t("eventsDescription")}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <LangSwitch ariaLabel={t("language")} />
          {canWrite && (
            <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
              <Plus size={13} /> {t("newEvent")}
            </Button>
          )}
        </div>
      }
    >
      <Section>
        <DataTable<HolidayEvent>
          filters={
            <FilterBar
              selects={[
                {
                  key: "country",
                  label: t("filterCountry"),
                  allLabel: t("allCountries"),
                  options: (list.data?.countries ?? []).map((c) => ({ value: c.iso2, label: c.name })),
                  width: 190,
                },
                {
                  key: "year",
                  label: t("filterYear"),
                  allLabel: t("allYears"),
                  options: [thisYear - 1, thisYear, thisYear + 1, thisYear + 2].map((y) => ({ value: String(y), label: String(y) })),
                  width: 130,
                },
              ]}
            />
          }
          searchable={false}
          tableId="holiday-events"
          data={list.data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? (list.error as Error).message : null}
          onRetry={() => list.refetch()}
          emptyTitle={t("eventsEmpty")}
          emptyDescription={t("eventsEmptyHint")}
          tableMinWidth={880}
        />
      </Section>
      {creating && <NewEventDrawer countries={list.data?.countries ?? []} onClose={() => setCreating(false)} />}
    </PageFrame>
  );
}

/** The page in its own language (English by default; the switch keeps the choice in the `bk_lang` cookie). */
export function HolidayEventsView() {
  return (
    <I18nProvider store={langStore}>
      <HolidayEvents />
    </I18nProvider>
  );
}
