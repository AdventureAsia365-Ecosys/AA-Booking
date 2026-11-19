// app/(staff)/billing/_parts/evidence.messages.ts
// AA-1128 (B5): the English and Vietnamese text of evidence.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "The file could not be read.": "The file could not be read.",
    "A PDF is at most 3 MB here. Save a smaller copy or a photo of it.": "A PDF is at most 3 MB here. Save a smaller copy or a photo of it.",
    "Something went wrong": "Something went wrong",
  },
  {
    "The file could not be read.": "Không đọc được tệp.",
    "A PDF is at most 3 MB here. Save a smaller copy or a photo of it.": "PDF ở đây tối đa 3 MB. Hãy lưu bản nhỏ hơn hoặc chụp ảnh nó.",
    "Something went wrong": "Đã có lỗi xảy ra",
  },
  ),
);
