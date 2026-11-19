// app/(staff)/_parts/useRecordStatus.messages.ts
// AA-1128 (B5): the English and Vietnamese text of useRecordStatus.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Activate {0}": (...a: unknown[]) => `Activate ${a[0]}`,
    "Deactivate {0}": (...a: unknown[]) => `Deactivate ${a[0]}`,
    "Activate": "Activate",
    "Deactivate": "Deactivate",
    "Reason (optional)": "Reason (optional)",
    "e.g. closed for renovation": "e.g. closed for renovation",
    "? Sales can book it on proposal items again.": "? Sales can book it on proposal items again.",
    "? It stays on record with its rates and history, and items that already book it keep it, but it cannot be added to a proposal until it is activated again.": "? It stays on record with its rates and history, and items that already book it keep it, but it cannot be added to a proposal until it is activated again.",
  },
  {
    "Activate {0}": (...a: unknown[]) => `Kích hoạt ${a[0]}`,
    "Deactivate {0}": (...a: unknown[]) => `Ngưng hoạt động ${a[0]}`,
    "Activate": "Kích hoạt",
    "Deactivate": "Ngưng hoạt động",
    "Reason (optional)": "Lý do (không bắt buộc)",
    "e.g. closed for renovation": "ví dụ: đóng cửa để sửa chữa",
    "? Sales can book it on proposal items again.": "? Bộ phận bán hàng có thể đặt lại nó trong các mục của đề xuất.",
    "? It stays on record with its rates and history, and items that already book it keep it, but it cannot be added to a proposal until it is activated again.": "? Bản ghi vẫn được giữ cùng giá và lịch sử, các mục đã đặt nó vẫn giữ nguyên, nhưng không thể thêm vào đề xuất cho tới khi được kích hoạt lại.",
  },
  ),
);
