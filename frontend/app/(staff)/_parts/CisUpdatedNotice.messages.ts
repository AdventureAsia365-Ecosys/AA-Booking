// app/(staff)/_parts/CisUpdatedNotice.messages.ts
// AA-1128 (B5): the English and Vietnamese text of CisUpdatedNotice.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "1 item title differs": "1 item title differs",
    "{0} item titles differ": (...a: unknown[]) => `${a[0]} item titles differ`,
    "CIS has newer text for": "CIS has newer text for",
    "this tour": "this tour",
    "these tours": "these tours",
    "{0} from what CIS publishes today": (...a: unknown[]) => `${a[0]} from what CIS publishes today`,
    "(updated": "(updated",
  },
  {
    "1 item title differs": "1 tiêu đề mục khác",
    "{0} item titles differ": (...a: unknown[]) => `${a[0]} tiêu đề mục khác`,
    "CIS has newer text for": "CIS có nội dung mới hơn cho",
    "this tour": "tour này",
    "these tours": "các tour này",
    "{0} from what CIS publishes today": (...a: unknown[]) => `${a[0]} so với nội dung CIS đang xuất bản hôm nay`,
    "(updated": "(cập nhật",
  },
  ),
);
