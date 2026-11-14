// app/(staff)/pricing/_shared/closeRow.messages.ts — S280 (AA-1096, bk-058): the text of Reject / Withdraw on a
// pending pricing rule, FX rate or cancellation fee schedule (Pricing rules & FX and Pricing › Policy › Cancellation
// fees), English first, Vietnamese second. Both pages wrap it in their own language switch.

import { defineMessages } from "@/ui";

export const C = defineMessages(
  {
    reject: "Reject",
    withdraw: "Withdraw",
    rejectHint: "Refuse this pending row with a reason; it is never used for prices.",
    withdrawHint: "Take back the row you entered; it is never used for prices.",
    rejectTitle: "Reject this pending row?",
    rejectBody: (what: string) =>
      `${what} will never be approved or used for prices. Its day stays taken: enter the corrected row with another day.`,
    rejectReason: "Why is it rejected? (the maker sees it)",
    withdrawTitle: "Withdraw this pending row?",
    withdrawBody: (what: string) =>
      `${what} will never be approved or used for prices. Its day stays taken: enter the corrected row with another day.`,
    rejected: "Row rejected.",
    withdrawn: "Row withdrawn.",
    closedRejected: (who: string, why: string) => `Rejected by ${who}: ${why}`,
    closedWithdrawn: (who: string) => `Withdrawn by ${who}`,
    errOwnRow: "You entered this row: withdraw it instead of rejecting it.",
    errNotMaker: "Only the person who entered a row withdraws it; reject it with a reason instead.",
    errAlreadyApproved: "This row is approved already. The list is refreshed.",
    errAlreadyDecided: "This row was rejected or withdrawn already. The list is refreshed.",
    errReason: "Type a reason of 1 to 300 characters.",
    errGone: "This row no longer exists. The list is refreshed.",
    errGeneric: (code: string) => `The change was not saved (${code}).`,
  },
  {
    reject: "Từ chối",
    withdraw: "Rút lại",
    rejectHint: "Từ chối dòng đang chờ duyệt kèm lý do; dòng này không bao giờ được dùng để tính giá.",
    withdrawHint: "Rút lại dòng bạn đã nhập; dòng này không bao giờ được dùng để tính giá.",
    rejectTitle: "Từ chối dòng đang chờ duyệt?",
    rejectBody: (what: string) =>
      `${what} sẽ không bao giờ được duyệt hay dùng để tính giá. Ngày của dòng vẫn bị giữ: hãy nhập dòng đã sửa với ngày khác.`,
    rejectReason: "Lý do từ chối (người nhập sẽ thấy)",
    withdrawTitle: "Rút lại dòng đang chờ duyệt?",
    withdrawBody: (what: string) =>
      `${what} sẽ không bao giờ được duyệt hay dùng để tính giá. Ngày của dòng vẫn bị giữ: hãy nhập dòng đã sửa với ngày khác.`,
    rejected: "Đã từ chối dòng.",
    withdrawn: "Đã rút lại dòng.",
    closedRejected: (who: string, why: string) => `${who} đã từ chối: ${why}`,
    closedWithdrawn: (who: string) => `${who} đã rút lại`,
    errOwnRow: "Bạn đã nhập dòng này: hãy rút lại thay vì từ chối.",
    errNotMaker: "Chỉ người nhập dòng mới rút lại được; hãy từ chối kèm lý do.",
    errAlreadyApproved: "Dòng này đã được duyệt. Danh sách đã được làm mới.",
    errAlreadyDecided: "Dòng này đã bị từ chối hoặc rút lại. Danh sách đã được làm mới.",
    errReason: "Hãy nhập lý do dài từ 1 đến 300 ký tự.",
    errGone: "Dòng này không còn nữa. Danh sách đã được làm mới.",
    errGeneric: (code: string) => `Chưa lưu được thay đổi (${code}).`,
  },
);

export type CKey = keyof typeof C.en;
