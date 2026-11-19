// app/(staff)/cases/[code]/ProposalsTab.messages.ts
// AA-1128 (B5): the English and Vietnamese text of ProposalsTab.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Draft V{0} discarded": (...a: unknown[]) => `Draft V${a[0]} discarded`,
    "Version": "Version",
    "Title / notes": "Title / notes",
    "Status": "Status",
    "Items": "Items",
    "Total": "Total",
    "Sent": "Sent",
    "Updated": "Updated",
    "Actions": "Actions",
    "Preview": "Preview",
    "Preview {0}": (...a: unknown[]) => `Preview ${a[0]}`,
    "A sent or approved version is kept": "A sent or approved version is kept",
    "The case is closed": "The case is closed",
    "Discard draft": "Discard draft",
    "Discard draft {0}": (...a: unknown[]) => `Discard draft ${a[0]}`,
    "Proposal versions": "Proposal versions",
    "Each version is a full itinerary and price. A sent version is locked.": "Each version is a full itinerary and price. A sent version is locked.",
    "No proposal yet": "No proposal yet",
    "The first version is built from the trip legs.": "The first version is built from the trip legs.",
    "Discard draft version": "Discard draft version",
    "? It disappears from the case; its number is not reused. Sent and approved versions are kept.": "? It disappears from the case; its number is not reused. Sent and approved versions are kept.",
  },
  {
    "Draft V{0} discarded": (...a: unknown[]) => `Đã bỏ bản nháp V${a[0]}`,
    "Version": "Phiên bản",
    "Title / notes": "Tiêu đề / ghi chú",
    "Status": "Trạng thái",
    "Items": "Mục",
    "Total": "Tổng",
    "Sent": "Đã gửi",
    "Updated": "Cập nhật",
    "Actions": "Thao tác",
    "Preview": "Xem trước",
    "Preview {0}": (...a: unknown[]) => `Xem trước ${a[0]}`,
    "A sent or approved version is kept": "Phiên bản đã gửi hoặc đã duyệt được giữ lại",
    "The case is closed": "Hồ sơ đã đóng",
    "Discard draft": "Bỏ bản nháp",
    "Discard draft {0}": (...a: unknown[]) => `Bỏ bản nháp ${a[0]}`,
    "Proposal versions": "Phiên bản đề xuất",
    "Each version is a full itinerary and price. A sent version is locked.": "Mỗi phiên bản là một lịch trình và giá đầy đủ. Phiên bản đã gửi bị khoá.",
    "No proposal yet": "Chưa có đề xuất",
    "The first version is built from the trip legs.": "Phiên bản đầu tiên được tạo từ các chặng của chuyến đi.",
    "Discard draft version": "Bỏ phiên bản nháp",
    "? It disappears from the case; its number is not reused. Sent and approved versions are kept.": "? Nó biến mất khỏi hồ sơ; số của nó không được dùng lại. Phiên bản đã gửi và đã duyệt được giữ lại.",
  },
  ),
);
