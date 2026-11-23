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
}
