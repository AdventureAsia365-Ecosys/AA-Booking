// ui/booking/financePeriod.messages.ts
// AA-1128 (B5): the English and Vietnamese text of financePeriod.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "This month": "This month",
    "Last 3 months": "Last 3 months",
    "This year": "This year",
    "Any date": "Any date",
  },
  {
    "This month": "Tháng này",
    "Last 3 months": "3 tháng qua",
    "This year": "Năm nay",
    "Any date": "Mọi ngày",
  },
  ),
);
