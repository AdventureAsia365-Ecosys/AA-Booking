// app/(staff)/catalog/trip-plans/_parts/ComponentsPanel.messages.ts
// AA-1128 (B5): the English and Vietnamese text of ComponentsPanel.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Hotels & services": "Hotels & services",
    "Suggest from the itinerary": "Suggest from the itinerary",
    "See suggestions": "See suggestions",
    "The hotels and services an agency quote prices, day by day. Each needs a published supplier rate for the tour to price;": "The hotels and services an agency quote prices, day by day. Each needs a published supplier rate for the tour to price;",
    "Price setup": "Price setup",
    "shows what every tour still needs.": "shows what every tour still needs.",
    "Loading…": "Loading…",
    "No hotels or services yet.": "No hotels or services yet.",
    "Day": "Day",
    "Inactive": "Inactive",
  },
  {
    "Hotels & services": "Khách sạn & dịch vụ",
    "Suggest from the itinerary": "Gợi ý từ lịch trình",
    "See suggestions": "Xem gợi ý",
    "The hotels and services an agency quote prices, day by day. Each needs a published supplier rate for the tour to price;": "Các khách sạn và dịch vụ mà báo giá đại lý tính giá, theo từng ngày. Mỗi mục cần giá nhà cung cấp đã công bố để tour có giá;",
    "Price setup": "Thiết lập giá",
    "shows what every tour still needs.": "cho thấy mỗi tour còn thiếu gì.",
    "Loading…": "Đang tải…",
    "No hotels or services yet.": "Chưa có khách sạn hoặc dịch vụ.",
    "Day": "Ngày",
    "Inactive": "Ngưng hoạt động",
  },
  ),
);
