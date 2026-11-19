// app/(staff)/cases/_parts/StaffSelect.messages.ts
// AA-1128 (B5): the English and Vietnamese text of StaffSelect.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Loading staff…": "Loading staff…",
    "(inactive)": "(inactive)",
    " (me)": " (me)",
  },
  {
    "Loading staff…": "Đang tải nhân viên…",
    "(inactive)": "(ngưng hoạt động)",
    " (me)": " (tôi)",
  },
  ),
);
