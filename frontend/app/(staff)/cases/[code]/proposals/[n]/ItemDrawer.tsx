"use client";
// app/(staff)/cases/[code]/proposals/[n]/ItemDrawer.tsx
// AA-683 — add / edit one proposal item (UI spec §4.8, C7 "slot edit form"): title, day range, staff note
// and the attached CIS tour (read-only; detach only). The item is a commercial label written for this one
// customer; CIS day text is never copied into it (ADR 0004 O7). Edits stay in the builder until Save.

import { useState } from "react";
import { Unlink } from "lucide-react";
import { FormDrawer, FromCisTag } from "@/ui";
import { MAX_DAY, NOTES_MAX, TITLE_MAX, validateItem, type DraftItem } from "@/lib/proposalDraft";

export type ItemDraftFields = Pick<DraftItem, "title" | "day_from" | "day_to" | "notes" | "source_tour_id">;

const inputCls = "h-10 w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";

export function ItemDrawer({
  open,
  initial,
  isNew,
  onClose,
  onApply,
}: {
  open: boolean;
  initial: ItemDraftFields;
  isNew: boolean;
  onClose: () => void;
  onApply: (fields: ItemDraftFields) => void;
}) {
  // Remount per opening so the form starts from `initial`.
  return open ? <ItemForm initial={initial} isNew={isNew} onClose={onClose} onApply={onApply} /> : null;
}

function ItemForm({
  initial,
  isNew,
  onClose,
  onApply,
}: {
  initial: ItemDraftFields;
  isNew: boolean;
  onClose: () => void;
  onApply: (fields: ItemDraftFields) => void;
}) {
  const [title, setTitle] = useState(initial.title);
  const [from, setFrom] = useState(String(initial.day_from));
  const [to, setTo] = useState(String(initial.day_to));
  const [notes, setNotes] = useState(initial.notes);
  const [tour, setTour] = useState(initial.source_tour_id);
  const [touched, setTouched] = useState(false);

  const fields: ItemDraftFields = { title, day_from: Number(from), day_to: Number(to), notes, source_tour_id: tour };
  const errors = validateItem(fields);
  const dirty =
    title !== initial.title || from !== String(initial.day_from) || to !== String(initial.day_to) || notes !== initial.notes || tour !== initial.source_tour_id;

  return (
    <FormDrawer
      open
      onClose={onClose}
      title={isNew ? "Add item" : "Edit item"}
      submitLabel={isNew ? "Add item" : "Apply"}
      dirty={dirty}
      onSubmit={() => {
        setTouched(true);
        if (Object.keys(errors).length) return;
        onApply({ ...fields, title: title.trim() });
      }}
    >
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">
          Title <span className="text-danger">*</span>
        </span>
        <input autoFocus value={title} maxLength={TITLE_MAX} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Hanoi city walk and street food evening" className={inputCls} />
        {touched && errors.title && <span className="text-xs text-danger">{errors.title}</span>}
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-ink">From day</span>
          <input type="number" min={1} max={MAX_DAY} value={from} onChange={(e) => setFrom(e.target.value)} className={inputCls} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-ink">To day</span>
          <input type="number" min={1} max={MAX_DAY} value={to} onChange={(e) => setTo(e.target.value)} className={inputCls} />
        </label>
        {touched && errors.days && <span className="col-span-2 text-xs text-danger">{errors.days}</span>}
        <span className="col-span-2 text-xs text-muted">An item can span several days, e.g. a 3-day trek on days 4–6.</span>
      </div>
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">Note to the customer</span>
        <textarea
          value={notes}
          rows={5}
          maxLength={NOTES_MAX}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Pick-up time, room note, what is arranged for this customer. Tour descriptions come from CIS."
          className="w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border"
        />
        <span className="text-right text-xs text-muted2">
          {notes.length} / {NOTES_MAX}
        </span>
      </label>
      <div className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">CIS tour</span>
        {tour ? (
          <div className="flex items-center justify-between gap-2 rounded-md border border-line bg-panel px-3 py-2">
            <span className="flex min-w-0 items-center gap-2 text-[13px] text-body">
              <FromCisTag /> <span className="truncate font-mono text-xs">{tour}</span>
            </span>
            <button type="button" onClick={() => setTour(null)} className="inline-flex cursor-pointer items-center gap-1 text-xs font-semibold text-muted hover:text-danger">
              <Unlink size={13} /> Detach
            </button>
          </div>
        ) : (
          <p className="text-xs text-muted">
            No CIS tour attached. Picking a tour from the CIS catalog arrives with the catalog endpoint (GET /booking/catalog/tours).
          </p>
        )}
      </div>
    </FormDrawer>
  );
}
