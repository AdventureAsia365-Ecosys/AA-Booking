// app/(staff)/pricing/calculator/page.messages.ts
// AA-1128 (B5): the English and Vietnamese text of page.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Pricing calculator": "Pricing calculator",
    "Price a set of net costs with the rule and FX rates in force today, without a trip case. Nothing is saved.": "Price a set of net costs with the rule and FX rates in force today, without a trip case. Nothing is saved.",
    "Reset": "Reset",
    "Net costs": "Net costs",
    "One line per supplier cost: the net amount and the supplier's currency.": "One line per supplier cost: the net amount and the supplier's currency.",
    "Settings": "Settings",
    "Channel, selling currency, peak season and an optional override total.": "Channel, selling currency, peak season and an optional override total.",
    "Result": "Result",
    "Rule in force, FX rates used, each line in USD and selling currency, rounding, margin and the floor check.": "Rule in force, FX rates used, each line in USD and selling currency, rounding, margin and the floor check.",
    "Complete every cost line (amount and 3-letter currency) to see the price.": "Complete every cost line (amount and 3-letter currency) to see the price.",
    "The calculator only shows the check: a below-floor price needs a reason when it is applied to a proposal version.": "The calculator only shows the check: a below-floor price needs a reason when it is applied to a proposal version.",
  },
  {
    "Pricing calculator": "Máy tính giá",
    "Price a set of net costs with the rule and FX rates in force today, without a trip case. Nothing is saved.": "Tính giá cho một bộ chi phí net theo quy tắc và tỷ giá đang áp dụng hôm nay, không cần hồ sơ chuyến đi. Không lưu gì cả.",
    "Reset": "Đặt lại",
    "Net costs": "Chi phí net",
    "One line per supplier cost: the net amount and the supplier's currency.": "Mỗi chi phí nhà cung cấp một dòng: số tiền net và tiền tệ của nhà cung cấp.",
    "Settings": "Cài đặt",
    "Channel, selling currency, peak season and an optional override total.": "Kênh, tiền tệ bán, mùa cao điểm và tổng ghi đè (không bắt buộc).",
    "Result": "Kết quả",
    "Rule in force, FX rates used, each line in USD and selling currency, rounding, margin and the floor check.": "Quy tắc đang áp dụng, tỷ giá đã dùng, từng dòng bằng USD và tiền tệ bán, làm tròn, margin và kiểm tra mức sàn.",
    "Complete every cost line (amount and 3-letter currency) to see the price.": "Hãy điền đủ mọi dòng chi phí (số tiền và mã tiền tệ 3 chữ cái) để xem giá.",
    "The calculator only shows the check: a below-floor price needs a reason when it is applied to a proposal version.": "Máy tính chỉ hiển thị kết quả kiểm tra: giá dưới mức sàn cần lý do khi áp dụng cho một phiên bản đề xuất.",
  },
  ),
);
