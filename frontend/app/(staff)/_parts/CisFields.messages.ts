// app/(staff)/_parts/CisFields.messages.ts
// AA-1128 (B5): the English and Vietnamese text of CisFields.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Read-only, from CIS": "Read-only, from CIS",
    "Not set in CIS": "Not set in CIS",
    "{0} from CIS": (...a: unknown[]) => `${a[0]} from CIS`,
  },
  {
    "Read-only, from CIS": "Chỉ đọc, từ CIS",
    "Not set in CIS": "Chưa đặt trong CIS",
    "{0} from CIS": (...a: unknown[]) => `${a[0]} từ CIS`,
  },
  ),
);
