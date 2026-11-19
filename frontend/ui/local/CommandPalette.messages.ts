// ui/local/CommandPalette.messages.ts
// AA-1128 (B5): the English and Vietnamese text of CommandPalette.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Custom trip": "Custom trip",
    "Pages": "Pages",
    "Trip cases": "Trip cases",
    "Global search": "Global search",
    "Search pages and trip cases…": "Search pages and trip cases…",
    "Search pages and trip cases": "Search pages and trip cases",
    "Esc": "Esc",
    "Type at least 2 characters to search.": "Type at least 2 characters to search.",
    "No results for “{0}”.": (...a: unknown[]) => `No results for “${a[0]}”.`,
    "↑ ↓ navigate": "↑ ↓ navigate",
    "↵ open": "↵ open",
    "esc close": "esc close",
  },
  {
    "Custom trip": "Chuyến đi theo yêu cầu",
    "Pages": "Trang",
    "Trip cases": "Hồ sơ chuyến đi",
    "Global search": "Tìm kiếm chung",
    "Search pages and trip cases…": "Tìm trang và hồ sơ chuyến đi…",
    "Search pages and trip cases": "Tìm trang và hồ sơ chuyến đi",
    "Esc": "Esc",
    "Type at least 2 characters to search.": "Gõ ít nhất 2 ký tự để tìm.",
    "No results for “{0}”.": (...a: unknown[]) => `Không có kết quả cho “${a[0]}”.`,
    "↑ ↓ navigate": "↑ ↓ di chuyển",
    "↵ open": "↵ mở",
    "esc close": "esc đóng",
  },
  ),
);
