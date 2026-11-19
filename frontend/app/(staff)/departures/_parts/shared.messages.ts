// app/(staff)/departures/_parts/shared.messages.ts
// AA-1128 (B5): the English and Vietnamese text of shared.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "At risk": "At risk",
    "Incident": "Incident",
    "{0} sold, {1} held, {2} kept for allotments of {3}": (...a: unknown[]) => `${a[0]} sold, ${a[1]} held, ${a[2]} kept for allotments of ${a[3]}`,
    "Scheduled": "Scheduled",
    "Guaranteed": "Guaranteed",
    "Cancelled": "Cancelled",
  },
  {
    "At risk": "Có rủi ro",
    "Incident": "Sự cố",
    "{0} sold, {1} held, {2} kept for allotments of {3}": (...a: unknown[]) => `${a[0]} đã bán, ${a[1]} đang giữ, ${a[2]} dành cho allotment trên ${a[3]}`,
    "Scheduled": "Đã lên lịch",
    "Guaranteed": "Đã đảm bảo",
    "Cancelled": "Đã huỷ",
  },
  ),
);
