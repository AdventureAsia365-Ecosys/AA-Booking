// app/(staff)/cases/_parts/StatusMultiSelect.messages.ts
// AA-1128 (B5): the English and Vietnamese text of StatusMultiSelect.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "All statuses": "All statuses",
    "{0} statuses": (...a: unknown[]) => `${a[0]} statuses`,
    "Closed": "Closed",
    "Status": "Status",
    "Statuses": "Statuses",
    "Clear status filter": "Clear status filter",
  },
  {
    "All statuses": "Mọi trạng thái",
    "{0} statuses": (...a: unknown[]) => `${a[0]} trạng thái`,
    "Closed": "Đã đóng",
    "Status": "Trạng thái",
    "Statuses": "Trạng thái",
    "Clear status filter": "Bỏ lọc trạng thái",
  },
  ),
);
