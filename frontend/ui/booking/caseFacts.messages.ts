// ui/booking/caseFacts.messages.ts
// AA-1128 (B5): the English and Vietnamese text of caseFacts.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "read-only": "read-only",
    "Traveller (B2C history)": "Traveller (B2C history)",
    "B2B agency": "B2B agency",
    "No agency on this case": "No agency on this case",
    "B2B agency booking": "B2B agency booking",
    "Agency not in Booking yet": "Agency not in Booking yet",
    "The price saved on this version when it was approved. It no longer changes.": "The price saved on this version when it was approved. It no longer changes.",
    "The price saved on this version when it was sent. It no longer changes.": "The price saved on this version when it was sent. It no longer changes.",
    "This is the price the customer received. To change it, create a new version from the case.": "This is the price the customer received. To change it, create a new version from the case.",
    "The agency confirmed this price with its booking. Agency invoices are made from it.": "The agency confirmed this price with its booking. Agency invoices are made from it.",
    "The customer approved this price. Invoices are made from it on the Billing tab.": "The customer approved this price. Invoices are made from it on the Billing tab.",
    "Approved by the agency": "Approved by the agency",
    "Approved by the customer": "Approved by the customer",
  },
  {
    "read-only": "chỉ đọc",
    "Traveller (B2C history)": "Khách lẻ (lịch sử B2C)",
    "B2B agency": "Đại lý B2B",
    "No agency on this case": "Hồ sơ này không có đại lý",
    "B2B agency booking": "Đặt chỗ của đại lý B2B",
    "Agency not in Booking yet": "Đại lý chưa có trong Booking",
    "The price saved on this version when it was approved. It no longer changes.": "Giá được lưu trên phiên bản này khi được duyệt. Giá không còn thay đổi.",
    "The price saved on this version when it was sent. It no longer changes.": "Giá được lưu trên phiên bản này khi được gửi. Giá không còn thay đổi.",
    "This is the price the customer received. To change it, create a new version from the case.": "Đây là giá khách đã nhận. Muốn đổi, hãy tạo phiên bản mới từ hồ sơ.",
    "The agency confirmed this price with its booking. Agency invoices are made from it.": "Đại lý đã xác nhận giá này khi đặt chỗ. Hoá đơn đại lý được lập từ giá này.",
    "The customer approved this price. Invoices are made from it on the Billing tab.": "Khách đã duyệt giá này. Hoá đơn được lập từ giá này ở thẻ Thanh toán.",
    "Approved by the agency": "Đại lý đã duyệt",
    "Approved by the customer": "Khách đã duyệt",
  },
  ),
);
