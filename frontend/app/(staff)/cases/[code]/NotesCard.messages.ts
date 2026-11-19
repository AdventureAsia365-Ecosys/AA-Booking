// app/(staff)/cases/[code]/NotesCard.messages.ts
// AA-1128 (B5): the English and Vietnamese text of NotesCard.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Internal notes": "Internal notes",
    "Staff only. Earlier text is kept in the Activity tab.": "Staff only. Earlier text is kept in the Activity tab.",
    "Edit internal notes": "Edit internal notes",
    "Cancel": "Cancel",
    "Saving…": "Saving…",
    "Save notes": "Save notes",
    "No notes yet.": "No notes yet.",
    " Use the pencil to add one.": " Use the pencil to add one.",
  },
  {
    "Internal notes": "Ghi chú nội bộ",
    "Staff only. Earlier text is kept in the Activity tab.": "Chỉ nhân viên. Nội dung trước đó được giữ ở thẻ Hoạt động.",
    "Edit internal notes": "Sửa ghi chú nội bộ",
    "Cancel": "Huỷ",
    "Saving…": "Đang lưu…",
    "Save notes": "Lưu ghi chú",
    "No notes yet.": "Chưa có ghi chú.",
    " Use the pencil to add one.": " Dùng biểu tượng bút chì để thêm.",
  },
  ),
);
