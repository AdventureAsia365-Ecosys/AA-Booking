"use client";
// app/(staff)/_parts/RecordStatusButton.tsx — the Deactivate / Activate button of a hotel or a service (AA-823), shared by
// the hotel drawer and the service page. Deactivate needs supply.write; Activate is an approval and needs
// supplier.activate (AA-1155), so the button is not shown to a role that cannot use it (useRecordStatus().canToggle).

import { Ban, Power } from "lucide-react";
import { Button } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import type { useRecordStatus } from "./useRecordStatus";
import { MSG } from "./useRecordStatus.messages";

export function RecordStatusButton({
  status,
  kind,
  record,
  active,
  compact = false,
}: {
  status: ReturnType<typeof useRecordStatus>;
  kind: "hotel" | "service";
  record: { code: string; name: string };
  /** The record is active now: the button deactivates it; otherwise it activates it. */
  active: boolean;
  /** Drawer header: small button, red when it deactivates. */
  compact?: boolean;
}) {
  const tr = useT(MSG);
  if (!status.canToggle(active)) return null;
  const iconSize = compact ? 13 : 14;
  return (
    <Button
      size={compact ? "sm" : undefined}
      variant={compact && active ? "danger" : "secondary"}
      onClick={() => status.ask(kind, record, !active)}
    >
      {active ? <Ban size={iconSize} /> : <Power size={iconSize} />} {active ? tr("Deactivate") : tr("Activate")}
    </Button>
  );
}
