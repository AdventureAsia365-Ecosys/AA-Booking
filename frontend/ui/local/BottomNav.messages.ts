// ui/local/BottomNav.messages.ts
// AA-1128 (B5): the English and Vietnamese text of BottomNav.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Primary (mobile)": "Primary (mobile)",
    "More navigation": "More navigation",
    "More": "More",
    "Dashboard": "Dashboard",
    "Cases": "Cases",
    "B2B desk": "B2B desk",
    "Alerts": "Alerts",
  },
  {
    "Primary (mobile)": "Điều hướng chính (di động)",
    "More navigation": "Điều hướng khác",
    "More": "Thêm",
    "Dashboard": "Bảng điều khiển",
    "Cases": "Hồ sơ",
    "B2B desk": "Bàn B2B",
    "Alerts": "Cảnh báo",
  },
  ),
);
