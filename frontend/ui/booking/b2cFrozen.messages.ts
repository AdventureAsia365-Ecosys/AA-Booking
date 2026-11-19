// ui/booking/b2cFrozen.messages.ts
// AA-1128 (B5): the English and Vietnamese text of b2cFrozen.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Read-only: Adventure Asia works with partner agencies only, so this traveller trip is kept as history and can no longer be changed.": "Read-only: Adventure Asia works with partner agencies only, so this traveller trip is kept as history and can no longer be changed.",
  },
  {
    "Read-only: Adventure Asia works with partner agencies only, so this traveller trip is kept as history and can no longer be changed.": "Chỉ đọc: Adventure Asia chỉ làm việc với đại lý đối tác, nên chuyến đi của khách lẻ này được giữ làm lịch sử và không thể thay đổi nữa.",
  },
  ),
);
