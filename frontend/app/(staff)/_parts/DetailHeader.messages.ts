// app/(staff)/_parts/DetailHeader.messages.ts
// AA-1128 (B5): the English and Vietnamese text of DetailHeader.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Back to {0}": (...a: unknown[]) => `Back to ${a[0]}`,
    "{0} sections": (...a: unknown[]) => `${a[0]} sections`,
  },
  {
    "Back to {0}": (...a: unknown[]) => `Quay lại ${a[0]}`,
    "{0} sections": (...a: unknown[]) => `${a[0]} mục`,
  },
  ),
);
