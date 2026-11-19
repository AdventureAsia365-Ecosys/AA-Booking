// app/(staff)/hotels/_parts/RoomTypeDrawer.messages.ts
// AA-1128 (B5): the English and Vietnamese text of RoomTypeDrawer.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "{0} saved.": (...a: unknown[]) => `${a[0]} saved.`,
    "Room type {0} added.": (...a: unknown[]) => `Room type ${a[0]} added.`,
    "Give the room type a name, e.g. Deluxe Double.": "Give the room type a name, e.g. Deluxe Double.",
    "Max occupancy is a whole number from 1 to 20.": "Max occupancy is a whole number from 1 to 20.",
    "Edit {0}": (...a: unknown[]) => `Edit ${a[0]}`,
    "Add room type": "Add room type",
    "Save room type": "Save room type",
    "Name": "Name",
    "e.g. Deluxe Double": "e.g. Deluxe Double",
    "Max occupancy": "Max occupancy",
    "Bed types": "Bed types",
    "Comma separated.": "Comma separated.",
    "Queen, Twin": "Queen, Twin",
    "Internal notes": "Internal notes",
    "Staff only.": "Staff only.",
  },
  {
    "{0} saved.": (...a: unknown[]) => `Đã lưu ${a[0]}.`,
    "Room type {0} added.": (...a: unknown[]) => `Đã thêm loại phòng ${a[0]}.`,
    "Give the room type a name, e.g. Deluxe Double.": "Hãy đặt tên cho loại phòng, ví dụ Deluxe Double.",
    "Max occupancy is a whole number from 1 to 20.": "Số người tối đa là số nguyên từ 1 đến 20.",
    "Edit {0}": (...a: unknown[]) => `Sửa ${a[0]}`,
    "Add room type": "Thêm loại phòng",
    "Save room type": "Lưu loại phòng",
    "Name": "Tên",
    "e.g. Deluxe Double": "ví dụ Deluxe Double",
    "Max occupancy": "Số người tối đa",
    "Bed types": "Loại giường",
    "Comma separated.": "Cách nhau bằng dấu phẩy.",
    "Queen, Twin": "Queen, Twin",
    "Internal notes": "Ghi chú nội bộ",
    "Staff only.": "Chỉ nhân viên.",
  },
  ),
);
