// app/(staff)/cases/_parts/TripPlanPicker.messages.ts
// AA-1128 (B5): the English and Vietnamese text of TripPlanPicker.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Change": "Change",
    "Search sellable trip plans": "Search sellable trip plans",
    "Search sellable trip plans by title, SKU or CIS id": "Search sellable trip plans by title, SKU or CIS id",
    "Sellable trip plans": "Sellable trip plans",
    "Searching…": "Searching…",
    "No sellable trip plan matches. Mark a plan sellable in Catalog → Trip plans first.": "No sellable trip plan matches. Mark a plan sellable in Catalog → Trip plans first.",
  },
  {
    "Change": "Đổi",
    "Search sellable trip plans": "Tìm kế hoạch chuyến đi bán được",
    "Search sellable trip plans by title, SKU or CIS id": "Tìm kế hoạch chuyến đi bán được theo tên, SKU hoặc mã CIS",
    "Sellable trip plans": "Kế hoạch chuyến đi bán được",
    "Searching…": "Đang tìm…",
    "No sellable trip plan matches. Mark a plan sellable in Catalog → Trip plans first.": "Không có kế hoạch bán được nào khớp. Hãy đánh dấu một kế hoạch là bán được trong Danh mục → Kế hoạch chuyến đi trước.",
  },
  ),
);
