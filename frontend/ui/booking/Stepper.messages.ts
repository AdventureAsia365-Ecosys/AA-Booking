// ui/booking/Stepper.messages.ts
// AA-1128 (B5): the English and Vietnamese text of Stepper.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "This trip case is cancelled.": "This trip case is cancelled.",
    "Reason:": "Reason:",
    "Trip case progress": "Trip case progress",
    "Inquiry": "Inquiry",
    "Proposal": "Proposal",
    "Booking": "Booking",
    "Travel": "Travel",
  },
  {
    "This trip case is cancelled.": "Hồ sơ chuyến đi này đã bị huỷ.",
    "Reason:": "Lý do:",
    "Trip case progress": "Tiến độ hồ sơ chuyến đi",
    "Inquiry": "Yêu cầu",
    "Proposal": "Đề xuất",
    "Booking": "Booking",
    "Travel": "Chuyến đi",
  },
  ),
);
