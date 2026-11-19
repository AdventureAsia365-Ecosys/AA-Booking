// ui/booking/earlyBooking.messages.ts
// AA-1128 (B5): the English and Vietnamese text of earlyBooking.ts (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "Enter the days before start, or clear the margin points.": "Enter the days before start, or clear the margin points.",
    "Enter the margin points off, or clear the days.": "Enter the margin points off, or clear the days.",
    "Days before start is a whole number from 1 to 365.": "Days before start is a whole number from 1 to 365.",
    "Margin points off is a number above 0 and below 100, for example 5.": "Margin points off is a number above 0 and below 100, for example 5.",
    "Only the B2B and fixed departure rules carry an early-booking discount.": "Only the B2B and fixed departure rules carry an early-booking discount.",
    "Enter both values, with no more margin points off than the target margin.": "Enter both values, with no more margin points off than the target margin.",
  },
  {
    "Enter the days before start, or clear the margin points.": "Hãy nhập số ngày trước khởi hành, hoặc xoá số điểm margin.",
    "Enter the margin points off, or clear the days.": "Hãy nhập số điểm margin giảm, hoặc xoá số ngày.",
    "Days before start is a whole number from 1 to 365.": "Số ngày trước khởi hành là số nguyên từ 1 đến 365.",
    "Margin points off is a number above 0 and below 100, for example 5.": "Số điểm margin giảm là số lớn hơn 0 và nhỏ hơn 100, ví dụ 5.",
    "Only the B2B and fixed departure rules carry an early-booking discount.": "Chỉ quy tắc B2B và khởi hành cố định có giảm giá đặt sớm.",
    "Enter both values, with no more margin points off than the target margin.": "Hãy nhập cả hai giá trị, số điểm margin giảm không quá margin mục tiêu.",
  },
  ),
);
