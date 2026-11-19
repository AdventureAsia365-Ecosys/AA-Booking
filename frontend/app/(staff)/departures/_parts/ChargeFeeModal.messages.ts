// app/(staff)/departures/_parts/ChargeFeeModal.messages.ts
// AA-1128 (B5): the English and Vietnamese text of ChargeFeeModal.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Fee of {0} {1} proposed; another person approves it": (...a: unknown[]) => `Fee of ${a[0]} ${a[1]} proposed; another person approves it`,
    "Charge departure fee": "Charge departure fee",
    "Cancel": "Cancel",
    "Proposing…": "Proposing…",
    "Propose fee": "Propose fee",
    "{0}. The fee waits for a second person to approve it; the agency is then told and it goes on its next settlement invoice.": (...a: unknown[]) => `${a[0]}. The fee waits for a second person to approve it; the agency is then told and it goes on its next settlement invoice.`,
    "Agency": "Agency",
    "Loading…": "Loading…",
    "Choose an agency": "Choose an agency",
    "{0} (on this departure)": (...a: unknown[]) => `${a[0]} (on this departure)`,
    "Reason": "Reason",
    "Seats": "Seats",
    "Price per seat": "Price per seat",
    "Currency": "Currency",
    "Note": "Note",
    "Why the fee is charged. Never a traveller's name.": "Why the fee is charged. Never a traveller's name.",
    "Total {0} {1}": (...a: unknown[]) => `Total ${a[0]} ${a[1]}`,
    "Something went wrong": "Something went wrong",
  },
  {
    "Fee of {0} {1} proposed; another person approves it": (...a: unknown[]) => `Đã đề xuất phí ${a[0]} ${a[1]}; người khác sẽ duyệt`,
    "Charge departure fee": "Thu phí chuyến khởi hành",
    "Cancel": "Huỷ",
    "Proposing…": "Đang đề xuất…",
    "Propose fee": "Đề xuất phí",
    "{0}. The fee waits for a second person to approve it; the agency is then told and it goes on its next settlement invoice.": (...a: unknown[]) => `${a[0]}. Phí chờ người thứ hai duyệt; sau đó đại lý được báo và phí được đưa vào hoá đơn quyết toán tiếp theo.`,
    "Agency": "Đại lý",
    "Loading…": "Đang tải…",
    "Choose an agency": "Chọn đại lý",
    "{0} (on this departure)": (...a: unknown[]) => `${a[0]} (trên chuyến khởi hành này)`,
    "Reason": "Lý do",
    "Seats": "Chỗ",
    "Price per seat": "Giá mỗi chỗ",
    "Currency": "Tiền tệ",
    "Note": "Ghi chú",
    "Why the fee is charged. Never a traveller's name.": "Lý do thu phí. Không bao giờ ghi tên khách.",
    "Total {0} {1}": (...a: unknown[]) => `Tổng ${a[0]} ${a[1]}`,
    "Something went wrong": "Đã có lỗi xảy ra",
  },
  ),
);
