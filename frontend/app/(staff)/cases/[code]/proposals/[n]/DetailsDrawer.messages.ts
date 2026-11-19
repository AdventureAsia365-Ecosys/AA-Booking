// app/(staff)/cases/[code]/proposals/[n]/DetailsDrawer.messages.ts
// AA-1128 (B5): the English and Vietnamese text of DetailsDrawer.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "V{0}: {1} updated": (...a: unknown[]) => `V${a[0]}: ${a[1]} updated`,
    "Nothing changed": "Nothing changed",
    "Title and notes · V{0}": (...a: unknown[]) => `Title and notes · V${a[0]}`,
    "Proposal title": "Proposal title",
    "e.g. Vietnam north to south, family pace": "e.g. Vietnam north to south, family pace",
    "Shown to the customer as the proposal name. A title cannot be cleared once set.": "Shown to the customer as the proposal name. A title cannot be cleared once set.",
    "Internal notes": "Internal notes",
  },
  {
    "V{0}: {1} updated": (...a: unknown[]) => `V${a[0]}: đã cập nhật ${a[1]}`,
    "Nothing changed": "Không có gì thay đổi",
    "Title and notes · V{0}": (...a: unknown[]) => `Tiêu đề và ghi chú · V${a[0]}`,
    "Proposal title": "Tiêu đề đề xuất",
    "e.g. Vietnam north to south, family pace": "ví dụ: Việt Nam từ Bắc vào Nam, nhịp độ gia đình",
    "Shown to the customer as the proposal name. A title cannot be cleared once set.": "Hiển thị cho khách làm tên đề xuất. Đã đặt tiêu đề thì không xoá được.",
    "Internal notes": "Ghi chú nội bộ",
  },
  ),
);
