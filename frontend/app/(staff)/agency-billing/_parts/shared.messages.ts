// app/(staff)/agency-billing/_parts/shared.messages.ts
// AA-1128 (B5): the English and Vietnamese text of shared.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Overdue {0} d": (...a: unknown[]) => `Overdue ${a[0]} d`,
    "Draft": "Draft",
    "Issued": "Issued",
    "Paid": "Paid",
    "Void": "Void",
    "Waiting for approval": "Waiting for approval",
    "Approved": "Approved",
    "Rejected": "Rejected",
    "Nothing overdue past the warning days": "Nothing overdue past the warning days",
    "Warning": "Warning",
    "Overdue past the warning days: the agency is warned": "Overdue past the warning days: the agency is warned",
    "Past due": "Past due",
    "Overdue past the suspend days: the account is suspended and new booking holds are paused": "Overdue past the suspend days: the account is suspended and new booking holds are paused",
    "Something went wrong": "Something went wrong",
  },
  {
    "Overdue {0} d": (...a: unknown[]) => `Quá hạn ${a[0]} ngày`,
    "Draft": "Bản nháp",
    "Issued": "Đã phát hành",
    "Paid": "Đã thanh toán",
    "Void": "Huỷ bỏ",
    "Waiting for approval": "Đang chờ duyệt",
    "Approved": "Đã duyệt",
    "Rejected": "Bị từ chối",
    "Nothing overdue past the warning days": "Không có gì quá hạn vượt số ngày cảnh báo",
    "Warning": "Cảnh báo",
    "Overdue past the warning days: the agency is warned": "Quá hạn vượt số ngày cảnh báo: đại lý được cảnh báo",
    "Past due": "Quá hạn",
    "Overdue past the suspend days: the account is suspended and new booking holds are paused": "Quá hạn vượt số ngày tạm ngưng: tài khoản bị tạm ngưng và việc giữ chỗ mới bị dừng",
    "Something went wrong": "Đã có lỗi xảy ra",
  },
  ),
);
