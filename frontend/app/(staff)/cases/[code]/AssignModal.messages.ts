// app/(staff)/cases/[code]/AssignModal.messages.ts
// AA-1128 (B5): the English and Vietnamese text of AssignModal.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Assign {0}": (...a: unknown[]) => `Assign ${a[0]}`,
    "Sales rep": "Sales rep",
    "The new rep gets a notification. Choose Unassigned to release the case.": "The new rep gets a notification. Choose Unassigned to release the case.",
    "Cancel": "Cancel",
    "Saving…": "Saving…",
    "Save": "Save",
  },
  {
    "Assign {0}": (...a: unknown[]) => `Giao ${a[0]}`,
    "Sales rep": "Nhân viên bán hàng",
    "The new rep gets a notification. Choose Unassigned to release the case.": "Người phụ trách mới sẽ nhận thông báo. Chọn Chưa phân công để bỏ giao hồ sơ.",
    "Cancel": "Huỷ",
    "Saving…": "Đang lưu…",
    "Save": "Lưu",
  },
  ),
);
