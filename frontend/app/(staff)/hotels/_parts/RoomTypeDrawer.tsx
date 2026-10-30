"use client";
// app/(staff)/hotels/_parts/RoomTypeDrawer.tsx — Add / edit a room type of a hotel (AA-823, bk-010): name, max
// occupancy, bed types, internal notes. Net rates hang off a room type and are entered under a supplier contract
// (supplier page → Rates).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { supplyApi, type RoomType, type RoomTypeBody } from "@/lib/api/supply";
import { Input, Textarea, FormDrawer, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { joinList, splitList, supplyError } from "@/app/(staff)/_parts/supply";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";

export function RoomTypeDrawer({ hotelRef, room, onClose }: { hotelRef: string; room?: RoomType; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const initial = {
    name: room?.name ?? "",
    max_occupancy: room?.max_occupancy ? String(room.max_occupancy) : "",
    bed_types: joinList(room?.bed_types),
    internal_notes: room?.internal_notes ?? "",
  };
  const [form, setForm] = useState(initial);
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const save = useMutation({
    mutationFn: (body: RoomTypeBody) => (room ? supplyApi.updateRoomType(room.id, body) : supplyApi.createRoomType(hotelRef, body)),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["hotel"] });
      qc.invalidateQueries({ queryKey: ["hotels"] });
      toast.success(room ? `${r.name} saved.` : `Room type ${r.name} added.`);
      onClose();
    },
  });

  function submit() {
    if (!form.name.trim()) return setLocalError("Give the room type a name, e.g. Deluxe Double.");
    const occ = form.max_occupancy ? Number(form.max_occupancy) : null;
    if (occ !== null && (!Number.isInteger(occ) || occ < 1 || occ > 20)) return setLocalError("Max occupancy is a whole number from 1 to 20.");
    setLocalError(null);
    save.mutate({
      name: form.name.trim(),
      max_occupancy: occ,
      bed_types: splitList(form.bed_types),
      internal_notes: form.internal_notes.trim() || null,
    });
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={room ? `Edit ${room.name}` : "Add room type"}
      submitLabel={room ? "Save room type" : "Add room type"}
      onSubmit={submit}
      busy={save.isPending}
      dirty={JSON.stringify(form) !== JSON.stringify(initial)}
      error={localError ?? (fe.length ? null : supplyError(save.error))}
      fieldErrors={fe}
    >
      <FormField label="Name" required error={errorFor(fe, "name")}>
        <Input autoFocus className={inputCls} maxLength={120} value={form.name} onChange={(e) => set("name")(e.target.value)} placeholder="e.g. Deluxe Double" />
      </FormField>
      <div className="grid grid-cols-2 gap-4">
        <FormField label="Max occupancy" error={errorFor(fe, "max_occupancy")}>
          <Input type="number" min={1} max={20} className={inputCls} value={form.max_occupancy} onChange={(e) => set("max_occupancy")(e.target.value)} />
        </FormField>
        <FormField label="Bed types" hint="Comma separated." error={errorFor(fe, "bed_types")}>
          <Input className={inputCls} value={form.bed_types} onChange={(e) => set("bed_types")(e.target.value)} placeholder="Queen, Twin" />
        </FormField>
      </div>
      <FormField label="Internal notes" hint="Staff only." error={errorFor(fe, "internal_notes")}>
        <Textarea className={textareaCls} maxLength={2000} value={form.internal_notes} onChange={(e) => set("internal_notes")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
