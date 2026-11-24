// app/(staff)/suppliers/_parts/ExpiringContracts.messages.ts
// AA-1177: the English and Vietnamese text of ExpiringContracts.tsx (kit core/i18n.ts). The keys are the English text.
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Contracts ending soon": "Contracts ending soon",
    "Published supplier contracts of every kind that end within {0} days (Pricing > Policy). Enter the next contract before then.": (...a: unknown[]) => `Published supplier contracts of every kind that end within ${a[0]} days (Pricing > Policy). Enter the next contract before then.`,
    "This supplier's published contracts that end within {0} days (Pricing > Policy). Enter the next contract before then.": (...a: unknown[]) => `This supplier's published contracts that end within ${a[0]} days (Pricing > Policy). Enter the next contract before then.`,
    "No published contract ends within {0} days.": (...a: unknown[]) => `No published contract ends within ${a[0]} days.`,
    "The list of contracts ending soon could not load. {0}": (...a: unknown[]) => `The list of contracts ending soon could not load. ${a[0]}`,
    "{0} days left": (...a: unknown[]) => `${a[0]} days left`,
    "1 day left": "1 day left",
    "ends": "ends",
    "E-mailed": "E-mailed",
    "Notified": "Notified",
    "The daily reminder e-mailed the staff roles set in Pricing > Policy.": "The daily reminder e-mailed the staff roles set in Pricing > Policy.",
    "The daily reminder posted a notification; the e-mail is not sent yet.": "The daily reminder posted a notification; the e-mail is not sent yet.",
    "Not announced yet: the daily reminder runs at 01:00 UTC.": "Not announced yet: the daily reminder runs at 01:00 UTC.",
    "Pending": "Pending",
  },
  {
    "Contracts ending soon": "Hợp đồng sắp hết hạn",
    "Published supplier contracts of every kind that end within {0} days (Pricing > Policy). Enter the next contract before then.": (...a: unknown[]) => `Các hợp đồng nhà cung cấp đã xuất bản (mọi loại) hết hạn trong ${a[0]} ngày (Pricing > Policy). Nhập hợp đồng kế tiếp trước ngày đó.`,
    "This supplier's published contracts that end within {0} days (Pricing > Policy). Enter the next contract before then.": (...a: unknown[]) => `Các hợp đồng đã xuất bản của nhà cung cấp này hết hạn trong ${a[0]} ngày (Pricing > Policy). Nhập hợp đồng kế tiếp trước ngày đó.`,
    "No published contract ends within {0} days.": (...a: unknown[]) => `Không có hợp đồng đã xuất bản nào hết hạn trong ${a[0]} ngày.`,
    "The list of contracts ending soon could not load. {0}": (...a: unknown[]) => `Không tải được danh sách hợp đồng sắp hết hạn. ${a[0]}`,
    "{0} days left": (...a: unknown[]) => `còn ${a[0]} ngày`,
    "1 day left": "còn 1 ngày",
    "ends": "hết hạn",
    "E-mailed": "Đã gửi e-mail",
    "Notified": "Đã thông báo",
    "The daily reminder e-mailed the staff roles set in Pricing > Policy.": "Nhắc việc hằng ngày đã gửi e-mail cho các vai trò nhân viên đặt trong Pricing > Policy.",
    "The daily reminder posted a notification; the e-mail is not sent yet.": "Nhắc việc hằng ngày đã đăng thông báo; e-mail chưa được gửi.",
    "Not announced yet: the daily reminder runs at 01:00 UTC.": "Chưa thông báo: nhắc việc hằng ngày chạy lúc 01:00 UTC.",
    "Pending": "Chờ",
  },
  ),
);
