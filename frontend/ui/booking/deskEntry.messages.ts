// ui/booking/deskEntry.messages.ts
// AA-1128 (B5): the English and Vietnamese text of deskEntry.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "no rooms": "no rooms",
  },
  {
    "no rooms": "không có phòng",
  },
  ),
);
