// app/(staff)/suppliers/_parts/EditSupplierView.messages.ts
// AA-1128 (B5): the English and Vietnamese text of EditSupplierView.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Loading supplier": "Loading supplier",
    "this supplier": "this supplier",
  },
  {
    "Loading supplier": "Đang tải nhà cung cấp",
    "this supplier": "nhà cung cấp này",
  },
  ),
);
