// app/(staff)/catalog/trip-plans/_parts/shared.messages.ts
// AA-1128 (B5): the English and Vietnamese text of shared.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Sellable": "Sellable",
    "Not for sale": "Not for sale",
    "per group": "per group",
    "per person": "per person",
  },
  {
    "Sellable": "Bán được",
    "Not for sale": "Không bán",
    "per group": "mỗi nhóm",
    "per person": "mỗi người",
  },
  ),
);
