// app/(staff)/catalog/trip-plans/_parts/useSellableToggle.messages.ts
// AA-1128 (B5): the English and Vietnamese text of useSellableToggle.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "no longer sellable": "no longer sellable",
    "now sellable": "now sellable",
    "not sellable": "not sellable",
    "sellable": "sellable",
    "{0} is {1}.": (...a: unknown[]) => `${a[0]} is ${a[1]}.`,
    "Make not sellable": "Make not sellable",
    "Make sellable": "Make sellable",
    "Stop selling": "Stop selling",
    "? The tour stays in CIS and on this list, and trip cases already started from it keep their proposals, but it cannot start a new trip case until it is made sellable again.": "? The tour stays in CIS and on this list, and trip cases already started from it keep their proposals, but it cannot start a new trip case until it is made sellable again.",
    "Make": "Make",
    "{0}? Sales can then start trip cases from it.": (...a: unknown[]) => `${a[0]}? Sales can then start trip cases from it.`,
  },
  {
    "no longer sellable": "không còn bán được",
    "now sellable": "đã bán được",
    "not sellable": "không bán",
    "sellable": "bán được",
    "{0} is {1}.": (...a: unknown[]) => `${a[0]} giờ là ${a[1]}.`,
    "Make not sellable": "Đặt là không bán",
    "Make sellable": "Đặt là bán được",
    "Stop selling": "Ngừng bán",
    "? The tour stays in CIS and on this list, and trip cases already started from it keep their proposals, but it cannot start a new trip case until it is made sellable again.": "? Tour vẫn ở CIS và trong danh sách này, các hồ sơ chuyến đi đã bắt đầu từ nó giữ nguyên đề xuất, nhưng không thể bắt đầu hồ sơ mới cho tới khi được đặt lại là bán được.",
    "Make": "Đặt",
    "{0}? Sales can then start trip cases from it.": (...a: unknown[]) => `${a[0]}? Sau đó bộ phận bán hàng có thể bắt đầu hồ sơ chuyến đi từ nó.`,
  },
  ),
);
