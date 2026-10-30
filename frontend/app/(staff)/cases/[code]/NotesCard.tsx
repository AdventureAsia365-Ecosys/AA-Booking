"use client";
// app/(staff)/cases/[code]/NotesCard.tsx
// AA-683 (fe-cases) — internal notes, editable inline by A and S (spec §4.7 side column). One text field on
// the case (`trip_cases.notes`, PATCH notes); every save is audited, so earlier text stays in History.

import { useState } from "react";
import { Pencil } from "lucide-react";
import type { TripCaseDetail } from "@/lib/api/types";
import { Textarea, Button, IconButton, Section, useCan } from "@/ui";
import { textareaCls } from "../_parts/fields";
import { friendlyError } from "../_parts/QueryError";
import { useUpdateCase } from "./useCaseMutations";

export function NotesCard({ c }: { c: TripCaseDetail }) {
  const canWrite = useCan("trip_case.write");
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const update = useUpdateCase(c.code);
  const current = c.notes?.trim() ?? "";

  function start() {
    setText(c.notes ?? "");
    update.reset();
    setEditing(true);
  }

  return (
    <Section
      title="Internal notes"
      description="Staff only. Earlier text is kept in the Activity tab."
      actions={
        canWrite && !editing ? (
          <IconButton onClick={start} label="Edit internal notes">
            <Pencil size={14} />
          </IconButton>
        ) : undefined
      }
    >
      {editing ? (
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim() === current) return setEditing(false);
            update.mutate({ notes: text.trim() || null }, { onSuccess: () => setEditing(false) });
          }}
        >
          <Textarea className={textareaCls} value={text} maxLength={10000} rows={6} onChange={(e) => setText(e.target.value)} aria-label="Internal notes" autoFocus />
          {update.isError && <p className="text-[13px] text-danger">{friendlyError(update.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)} disabled={update.isPending}>
              Cancel
            </Button>
            <Button variant="primary" size="sm" type="submit" disabled={update.isPending}>
              {update.isPending ? "Saving…" : "Save notes"}
            </Button>
          </div>
        </form>
      ) : current ? (
        <p className="whitespace-pre-wrap break-words text-[13px] text-body">{c.notes}</p>
      ) : (
        <p className="text-[13px] text-muted">No notes yet.{canWrite && " Use the pencil to add one."}</p>
      )}
    </Section>
  );
}
