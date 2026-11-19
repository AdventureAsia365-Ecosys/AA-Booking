// ui/booking/WaveFrame.messages.ts
// AA-1128 (B5): the English and Vietnamese text of WaveFrame.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Coming in wave {0}": (...a: unknown[]) => `Coming in wave ${a[0]}`,
    "Available in wave {0}.": (...a: unknown[]) => `Available in wave ${a[0]}.`,
  },
  {
    "Coming in wave {0}": (...a: unknown[]) => `Có trong đợt ${a[0]}`,
    "Available in wave {0}.": (...a: unknown[]) => `Có từ đợt ${a[0]}.`,
  },
  ),
);
