// app/(staff)/cases/_parts/fields.messages.ts
// AA-1128 (B5): the English and Vietnamese text of fields.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Other": "Other",
    "Budget too low": "Budget too low",
    "Not our destination": "Not our destination",
    "Duplicate inquiry": "Duplicate inquiry",
    "No response from the customer": "No response from the customer",
    "Adults must be a whole number from 1 to 999.": "Adults must be a whole number from 1 to 999.",
    "Children must be a whole number from 0 to 998.": "Children must be a whole number from 0 to 998.",
    "Adults plus children must be 999 or fewer.": "Adults plus children must be 999 or fewer.",
  },
  {
    "Other": "Khác",
    "Budget too low": "Ngân sách quá thấp",
    "Not our destination": "Không phải điểm đến của chúng tôi",
    "Duplicate inquiry": "Yêu cầu trùng",
    "No response from the customer": "Khách không phản hồi",
    "Adults must be a whole number from 1 to 999.": "Số người lớn phải là số nguyên từ 1 đến 999.",
    "Children must be a whole number from 0 to 998.": "Số trẻ em phải là số nguyên từ 0 đến 998.",
    "Adults plus children must be 999 or fewer.": "Tổng người lớn và trẻ em tối đa 999.",
  },
  ),
);
