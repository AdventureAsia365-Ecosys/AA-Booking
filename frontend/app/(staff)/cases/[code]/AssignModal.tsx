"use client";
// app/(staff)/cases/[code]/AssignModal.tsx
// AA-683 (fe-cases) — "Assign sales" (spec §3 modal table; not drawn in Figma). POST .../assign with the
// picked staff member, or null to leave the case unassigned. The API closes the previous holder's row and
// notifies the new one.

import { useState } from "react";
import type { TripCaseDetail } from "@/lib/api/types";
import { Button, Modal } from "@/ui";
import { Field } from "../_parts/fields";
import { StaffSelect } from "../_parts/StaffSelect";
import { friendlyError } from "../_parts/QueryError";
import { useAssign } from "./useCaseMutations";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./AssignModal.messages";

export function AssignModal({ c, open, onClose }: { c: TripCaseDetail; open: boolean; onClose: () => void }) {
  const t = useT(MSG);
  return (
    <Modal open={open} onClose={onClose} title={t("Assign {0}", c.code)} width={460}>
      {open && <AssignBody c={c} onClose={onClose} />}
    </Modal>
  );
}

function AssignBody({ c, onClose }: { c: TripCaseDetail; onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const current = c.assignees.find((a) => a.role === "sales")?.cis_user_id ?? "";
  const [rep, setRep] = useState(current);
  const assign = useAssign(c.code);
  return (
    <form
      className="flex flex-col gap-4 font-sans"
      onSubmit={(e) => {
        e.preventDefault();
        assign.mutate({ cisUserId: rep || null }, { onSuccess: onClose });
      }}
    >
      <Field label={t("Sales rep")} hint={t("The new rep gets a notification. Choose Unassigned to release the case.")}>
        <StaffSelect value={rep} onChange={setRep} />
      </Field>
      {assign.isError && <p className="text-[13px] text-danger">{tx(friendlyError(assign.error))}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose} disabled={assign.isPending}>
          {t("Cancel")}
        </Button>
        <Button variant="primary" type="submit" disabled={assign.isPending || rep === current}>
          {assign.isPending ? t("Saving…") : t("Save")}
        </Button>
      </div>
    </form>
  );
}
