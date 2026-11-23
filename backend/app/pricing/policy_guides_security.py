"""The operator's guides of the business-owner self-approval switches (AA-1158, bk-064;
:mod:`app.pricing.policy_params_security`), in the shape of :mod:`app.pricing.policy_guides` (split out of it: file
size limit). Text only."""

from __future__ import annotations

PARTS = ("what", "raise", "lower", "when", "example")


def _g(en: tuple[str, str, str, str, str], vi: tuple[str, str, str, str, str]) -> dict[str, dict[str, str]]:
    return {"en": dict(zip(PARTS, en, strict=True)), "vi": dict(zip(PARTS, vi, strict=True))}


SECURITY_GUIDES: dict[str, dict[str, dict[str, str]]] = {
    "owner_self_approval_allowed": _g(
        (
            "Whether the business owner may approve work she made herself (a supplier, a contract, a price version, a "
            "season, a supplier bill, an agency payment, a price adjustment, a billing entity change).",
            "On: the business owner is not blocked by her own work; each self-approval is flagged and listed under "
            "Business-owner self-approvals in the audit log.",
            "Off: every approval needs a second person, the business owner included.",
            "Turn it off when a fourth person joins who can check the business owner's work.",
            "Starting value on (tech lead decision Q5).",
        ),
        (
            "Chủ doanh nghiệp có được tự duyệt việc chính mình làm (nhà cung cấp, hợp đồng, phiên bản giá, mùa, hoá "
            "đơn nhà cung cấp, khoản thanh toán của đại lý, điều chỉnh giá, thay đổi pháp nhân xuất hoá đơn) hay "
            "không.",
            "Bật: chủ doanh nghiệp không bị chặn bởi việc của chính mình; mỗi lần tự duyệt được đánh dấu và liệt kê "
            "trong mục Chủ doanh nghiệp tự duyệt của nhật ký.",
            "Tắt: mọi lần duyệt cần người thứ hai, kể cả chủ doanh nghiệp.",
            "Tắt khi có người thứ tư kiểm được việc của chủ doanh nghiệp.",
            "Giá trị ban đầu là bật (quyết định Q5 của trưởng nhóm kỹ thuật).",
        ),
    ),
    "owner_payout_after_approval_allowed": _g(
        (
            "Whether the business owner may record the payout of a supplier bill she approved herself.",
            "On: the business owner approves the bill and records the transfer she made; the payout is flagged.",
            "Off: the payout of a bill is always recorded by someone who did not approve it (the tech lead is the "
            "fallback payer).",
            "Turn it off when another person can record payouts.",
            "Starting value on (tech lead decision Q4).",
        ),
        (
            "Chủ doanh nghiệp có được ghi khoản chi trả của một hoá đơn nhà cung cấp mà chính mình đã duyệt hay không.",
            "Bật: chủ doanh nghiệp duyệt hoá đơn và ghi lại khoản chuyển tiền mình đã làm; khoản chi được đánh dấu.",
            "Tắt: khoản chi của một hoá đơn luôn do người không duyệt nó ghi (trưởng nhóm kỹ thuật là người chi dự "
            "phòng).",
            "Tắt khi có người khác ghi được khoản chi.",
            "Giá trị ban đầu là bật (quyết định Q4 của trưởng nhóm kỹ thuật).",
        ),
    ),
    "supplier_bank_payout_lock_hours": _g(
        (
            "How long supplier payouts stay locked after a change of the supplier's bank account is approved. While "
            "a change waits for approval, payouts to that supplier are locked too.",
            "Raise it to give the business owner more time to notice a wrong or forged bank change before money "
            "leaves.",
            "Lower it when urgent supplier payments wait too long after a checked change; the call-back check stays.",
            "Change it when the payment routine changes, for example payouts once a week.",
            "Starting value 24 hours (tech lead decision Q10).",
        ),
        (
            "Khoá chi trả cho nhà cung cấp trong bao lâu sau khi một thay đổi tài khoản ngân hàng của họ được duyệt. "
            "Trong lúc thay đổi còn chờ duyệt, việc chi trả cho nhà cung cấp đó cũng bị khoá.",
            "Tăng để chủ doanh nghiệp có thêm thời gian phát hiện một thay đổi tài khoản sai hoặc giả trước khi tiền "
            "đi.",
            "Giảm khi các khoản chi gấp cho nhà cung cấp phải chờ quá lâu sau một thay đổi đã kiểm; bước gọi lại xác "
            "nhận vẫn giữ.",
            "Đổi khi lịch chi trả thay đổi, ví dụ chi mỗi tuần một lần.",
            "Giá trị ban đầu là 24 giờ (quyết định Q10 của trưởng nhóm kỹ thuật).",
        ),
    ),
    "supplier_bank_contact_change_days": _g(
        (
            "How many days back the approval of a supplier bank account looks for changes of the supplier's contacts "
            "(contact name, e-mail, phone and the contacts by role). The approver calls the supplier back on a known "
            "number; a number planted shortly before would let a forged account be confirmed.",
            "Raise it when contact details are rarely edited and a longer look-back costs nothing.",
            "Lower it when contacts change often for good reasons and approvals wait for no gain; the call-back stays.",
            "Change it when the routine for checking supplier contact changes changes.",
            "Seeded default 30 days, proposed with the call-back control (the administrator group may change it).",
        ),
        (
            "Khi duyệt một tài khoản ngân hàng của nhà cung cấp, hệ thống xem lại các thay đổi thông tin liên hệ của "
            "nhà cung cấp (tên, e-mail, điện thoại và liên hệ theo vai trò) trong bấy nhiêu ngày. Người duyệt gọi lại "
            "nhà cung cấp qua một số đã biết; một số vừa bị cài vào không lâu trước đó có thể giúp xác nhận một tài "
            "khoản giả.",
            "Tăng khi thông tin liên hệ hiếm khi được sửa và việc xem lại lâu hơn không tốn gì.",
            "Giảm khi thông tin liên hệ thay đổi thường xuyên vì lý do chính đáng và việc duyệt phải chờ vô ích; bước "
            "gọi lại xác nhận vẫn giữ.",
            "Đổi khi quy trình kiểm tra thay đổi liên hệ của nhà cung cấp thay đổi.",
            "Giá trị ban đầu là 30 ngày, đề xuất cùng bước gọi lại xác nhận (nhóm quản trị có thể đổi).",
        ),
    ),
}
