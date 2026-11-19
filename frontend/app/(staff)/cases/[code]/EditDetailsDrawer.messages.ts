// app/(staff)/cases/[code]/EditDetailsDrawer.messages.ts
// AA-1128 (B5): the English and Vietnamese text of EditDetailsDrawer.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Edit trip case {0}": (...a: unknown[]) => `Edit trip case ${a[0]}`,
    "Save changes": "Save changes",
    "Some fields need attention.": "Some fields need attention.",
    "For": "For",
    "A case keeps who it is for.": "A case keeps who it is for.",
    "Trip details": "Trip details",
    "Start date": "Start date",
    "Clear it if the dates are not fixed yet.": "Clear it if the dates are not fixed yet.",
    "Adults": "Adults",
    "Children": "Children",
    "Currency": "Currency",
    "Proposals already sent keep the currency they were sent in.": "Proposals already sent keep the currency they were sent in.",
    "Countries": "Countries",
    "Notes": "Notes",
    "Internal notes": "Internal notes",
    "Staff only. The customer never sees these.": "Staff only. The customer never sees these.",
  },
  {
    "Edit trip case {0}": (...a: unknown[]) => `Sửa hồ sơ chuyến đi ${a[0]}`,
    "Save changes": "Lưu thay đổi",
    "Some fields need attention.": "Một số trường cần kiểm tra lại.",
    "For": "Cho",
    "A case keeps who it is for.": "Hồ sơ giữ nguyên người nhận.",
    "Trip details": "Chi tiết chuyến đi",
    "Start date": "Ngày bắt đầu",
    "Clear it if the dates are not fixed yet.": "Xoá nếu ngày chưa cố định.",
    "Adults": "Người lớn",
    "Children": "Trẻ em",
    "Currency": "Tiền tệ",
    "Proposals already sent keep the currency they were sent in.": "Đề xuất đã gửi giữ tiền tệ lúc gửi.",
    "Countries": "Quốc gia",
    "Notes": "Ghi chú",
    "Internal notes": "Ghi chú nội bộ",
    "Staff only. The customer never sees these.": "Chỉ nhân viên. Khách không bao giờ thấy.",
  },
  ),
);
