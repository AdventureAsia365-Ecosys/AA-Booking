// ui/booking/TimeField.messages.ts
// AA-1128 (B5): the English and Vietnamese text of TimeField.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Not a valid time: use 24-hour HH:MM": "Not a valid time: use 24-hour HH:MM",
  },
  {
    "Not a valid time: use 24-hour HH:MM": "Giờ không hợp lệ: dùng định dạng 24 giờ HH:MM",
  },
  ),
);
