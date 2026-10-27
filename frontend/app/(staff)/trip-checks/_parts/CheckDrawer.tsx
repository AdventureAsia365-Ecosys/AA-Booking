"use client";
// app/(staff)/trip-checks/_parts/CheckDrawer.tsx
// AA-682 S2 (bk-027, S252 design §6) — the check drawer, shared by /trip-checks and the case Checks tab: status
// (Open / Done / Not applicable), due date, owner, note; the title of a custom check; and on the Emergency contact check
// of a B2C trip the contact's name and phone (a B2B trip keeps no traveller data in Booking: the agency holds it).
// PATCH /booking/trip-checks/{id} sends only the fields that changed. Read-only without ops.write, on a cancelled or
// completed trip, and for the contact once it was cleared after the trip.
// Also exports NewCheckDrawer (POST /booking/trip-cases/{ref}/checks: a custom check).

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, type FieldError } from "@/lib/api/client";
import { TRIP_CHECK_CODE_LABEL, tripChecksApi, type CheckChanges, type TripCheck, type TripCheckStatus } from "@/lib/api/tripChecks";
import { DateText, FormDrawer, Pill, TripCheckStatusBadge, useCan, useToast } from "@/ui";
import { Field, inputCls, LockedValue, textareaCls } from "../../cases/_parts/fields";
import { StaffSelect } from "../../cases/_parts/StaffSelect";

const STATUS_OPTIONS: { value: TripCheckStatus; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "done", label: "Done" },
  { value: "not_applicable", label: "Not applicable" },
];

export function checkTitle(c: Pick<TripCheck, "code" | "title">): string {
  return c.code === "custom" ? c.title : TRIP_CHECK_CODE_LABEL[c.code];
}

export function isOverdue(c: Pick<TripCheck, "status" | "due_date">, today = new Date().toISOString().slice(0, 10)): boolean {
  return c.status === "open" && !!c.due_date && c.due_date < today;
}

function errorParts(e: unknown): { message: string | null; fields: FieldError[] } {
  if (e instanceof ApiError) return { message: e.fieldErrors.length ? null : e.message, fields: e.fieldErrors };
  return { message: e instanceof Error ? e.message : e ? "Something went wrong" : null, fields: [] };
}

type Form = {
  status: TripCheckStatus;
  title: string;
  due_date: string;
  assignee_ref: string;
  note: string;
  emergency_contact_name: string;
  emergency_contact_phone: string;
};

function formOf(c: TripCheck): Form {
  return {
    status: c.status,
    title: c.title,
    due_date: c.due_date ?? "",
    assignee_ref: c.assignee_ref ?? "",
    note: c.note ?? "",
    emergency_contact_name: c.emergency_contact_name ?? "",
    emergency_contact_phone: c.emergency_contact_phone ?? "",
  };
}

/** Only the fields that changed; an emptied field is sent as null. */
function diff(c: TripCheck, f: Form): CheckChanges {
  const before = formOf(c);
  const out: CheckChanges = {};
  (Object.keys(f) as (keyof Form)[]).forEach((k) => {
    if (f[k].trim() === before[k].trim()) return;
    const v = f[k].trim();
    if (k === "status") out.status = f.status;
    else if (k === "title") out.title = v;
    else (out as Record<string, string | null>)[k] = v || null;
  });
  return out;
}

export function CheckDrawer({
  check,
  open,
  onClose,
  writable = true,
  showCase = false,
}: {
  check: TripCheck | null;
  open: boolean;
  onClose: () => void;
  /** False on a cancelled / completed trip (the API answers 409 case_closed). */
  writable?: boolean;
  /** /trip-checks shows a link to the trip case. */
  showCase?: boolean;
}) {
  if (!check) return null;
  return <CheckDrawerBody key={`${check.id}:${check.updated_at}`} check={check} open={open} onClose={onClose} writable={writable} showCase={showCase} />;
}

function CheckDrawerBody({
  check,
  open,
  onClose,
  writable,
  showCase,
}: {
  check: TripCheck;
  open: boolean;
  onClose: () => void;
  writable: boolean;
  showCase: boolean;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const canWrite = useCan("ops.write") && writable;
  const [form, setForm] = useState<Form>(() => formOf(check));
  const changes = diff(check, form);
  const dirty = Object.keys(changes).length > 0;
  const contactFields = check.contact_allowed && !check.cleared_at;
  const save = useMutation({
    mutationFn: () => tripChecksApi.update(check.id, changes),
    onSuccess: (r) => {
      toast.success(r.changed.length ? `${checkTitle(r)} saved` : "Nothing changed");
      qc.invalidateQueries({ queryKey: ["trip-checks"] });
      qc.invalidateQueries({ queryKey: ["trip-case", check.trip_case.code] });
      onClose();
    },
  });
  const err = errorParts(save.error);
  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <FormDrawer
      open={open}
      onClose={onClose}
      title={checkTitle(check)}
      onSubmit={() => (canWrite && dirty ? save.mutate() : onClose())}
      submitLabel={canWrite ? "Save check" : "Close"}
      busy={save.isPending}
      dirty={canWrite && dirty}
      error={err.message}
      fieldErrors={err.fields}
    >
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <TripCheckStatusBadge status={check.status} overdue={isOverdue(check)} />
        {showCase ? (
          <Link href={`/cases/${encodeURIComponent(check.trip_case.code)}?tab=checks`} className="font-medium text-accent-deep hover:underline">
            {check.trip_case.code}
          </Link>
        ) : (
          <span>{check.trip_case.code}</span>
        )}
        {check.trip_case.channel === "b2b" && <Pill tone="accent" dot={false}>B2B</Pill>}
        {check.trip_case.start_date && (
          <span>
            · starts <DateText value={check.trip_case.start_date} />
          </span>
        )}
      </div>
      {!canWrite && (
        <p className="rounded-md border border-line bg-panel px-3 py-2 text-[13px] text-muted">
          {writable ? "Read only: changing checks needs the operations role." : "This trip is closed: its checks are read only."}
        </p>
      )}
      {check.code === "custom" ? (
        <Field label="What to check" required>
          <input className={inputCls} value={form.title} onChange={set("title")} maxLength={200} disabled={!canWrite} />
        </Field>
      ) : (
        <Field label="Check">
          <LockedValue value={check.title} hint="A standard check of every sold trip." />
        </Field>
      )}
      <Field
        label="Status"
        hint={check.done_by && check.status === "done" ? <>Done by {check.done_by.name ?? check.done_by.id}{check.done_at ? <> · <DateText value={check.done_at} withTime /></> : null}</> : undefined}
      >
        <select className={inputCls} value={form.status} onChange={set("status")} disabled={!canWrite || (check.code === "emergency_contact" && !check.contact_allowed)}>
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Due date" hint={check.standard ? "Moves with the start date while open." : undefined}>
          <input type="date" className={inputCls} value={form.due_date} onChange={set("due_date")} disabled={!canWrite} />
        </Field>
        <Field label="Owner">
          {canWrite ? (
            <StaffSelect value={form.assignee_ref} onChange={(v) => setForm((f) => ({ ...f, assignee_ref: v }))} emptyLabel="Anyone on the case" ariaLabel="Owner" />
          ) : (
            <LockedValue value={check.assignee?.name ?? check.assignee?.id ?? "Anyone on the case"} />
          )}
        </Field>
      </div>
      {check.code === "emergency_contact" &&
        (contactFields ? (
          <fieldset className="flex flex-col gap-3 rounded-md border border-line p-3">
            <legend className="px-1 text-[13px] font-medium text-ink">Emergency contact</legend>
            <p className="text-xs text-muted">
              Someone at home to call if something happens on the trip. Needed to mark this check done. Removed automatically 90 days after the trip ends.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Name">
                <input className={inputCls} value={form.emergency_contact_name} onChange={set("emergency_contact_name")} maxLength={200} disabled={!canWrite} autoComplete="off" />
              </Field>
              <Field label="Phone" hint="With the country code, e.g. +84 90 123 4567.">
                <input
                  className={inputCls}
                  type="tel"
                  value={form.emergency_contact_phone}
                  onChange={set("emergency_contact_phone")}
                  maxLength={40}
                  disabled={!canWrite}
                  autoComplete="off"
                />
              </Field>
            </div>
          </fieldset>
        ) : (
          <p className="rounded-md border border-line bg-panel px-3 py-2 text-[13px] text-muted">
            {check.cleared_at
              ? "The emergency contact was removed after the trip (kept 90 days after the end)."
              : "B2B trip: the agency keeps its travellers' emergency contacts. Nothing is stored in Booking."}
          </p>
        ))}
      <Field label="Note" hint={check.cleared_at ? "Notes were removed after the trip." : undefined}>
        <textarea className={textareaCls} value={form.note} onChange={set("note")} maxLength={2000} disabled={!canWrite || !!check.cleared_at} />
      </Field>
    </FormDrawer>
  );
}

export function NewCheckDrawer({ caseCode, open, onClose }: { caseCode: string; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [owner, setOwner] = useState("");
  const [note, setNote] = useState("");
  const add = useMutation({
    mutationFn: () =>
      tripChecksApi.create(caseCode, { title: title.trim(), due_date: due || null, assignee_ref: owner || null, note: note.trim() || null }),
    onSuccess: (r) => {
      toast.success(`Check "${r.title}" added to ${caseCode}`);
      qc.invalidateQueries({ queryKey: ["trip-checks"] });
      qc.invalidateQueries({ queryKey: ["trip-case", caseCode] });
      setTitle("");
      setDue("");
      setOwner("");
      setNote("");
      onClose();
    },
  });
  const err = errorParts(add.error);
  return (
    <FormDrawer
      open={open}
      onClose={onClose}
      title="Add a check"
      onSubmit={() => (title.trim() ? add.mutate() : undefined)}
      submitLabel="Add check"
      busy={add.isPending}
      dirty={!!(title || due || owner || note)}
      error={err.message}
      fieldErrors={err.fields}
    >
      <Field label="What to check" required>
        <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder="e.g. Visa approval letters received" />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Due date">
          <input type="date" className={inputCls} value={due} onChange={(e) => setDue(e.target.value)} />
        </Field>
        <Field label="Owner">
          <StaffSelect value={owner} onChange={setOwner} emptyLabel="Anyone on the case" ariaLabel="Owner" />
        </Field>
      </div>
      <Field label="Note">
        <textarea className={textareaCls} value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} />
      </Field>
    </FormDrawer>
  );
}
