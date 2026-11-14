"""Page hints of the pricing policy parameters on Pricing > Policy (AA-1096, S280 settings UI audit).

Two things the staff page needs and :mod:`app.pricing.policy_params` does not say in a form a page can show:

- ``cross_bound``: the bound a parameter's value has against **another** parameter's value in force
  (``policy_params.Param.cross_check``), as ``{"relation": "lt" | "lte" | "gt" | "gte" | "ne", "key": <other key>}``.
  The page writes it in the reader's language with the other parameter's label and value ("At most Quote validity
  (14 days)") and checks it before sending, instead of the API's English message with raw keys. The API still decides
  (``normalise`` runs the same check).
- ``not_enforced_reason_vi``: the Vietnamese text of ``Param.pending`` (the "Not enforced yet" reason), so a page in
  Vietnamese never mixes in the English reason. The English text stays the single source in ``policy_params``.

The unit test ``test_policy_hints`` keeps both maps complete: every parameter with a ``cross_check`` has its bound here
and every ``pending`` text has its Vietnamese text.
"""

from __future__ import annotations

from typing import Any

from app.pricing import policy_params

RELATIONS = ("lt", "lte", "gt", "gte", "ne")

# key -> (relation, other key): the value of ``key`` must be <relation> the value of ``other key`` in force.
CROSS_BOUNDS: dict[str, tuple[str, str]] = {
    "tariff_next_publish_by": ("ne", "tariff_year_start"),
    "quote_validity_days": ("gte", "quote_validity_festive_days"),
    "quote_validity_festive_days": ("lte", "quote_validity_days"),
    "b2b_child_age_max": ("gt", "b2b_infant_age_max"),
    "b2b_infant_age_max": ("lt", "b2b_child_age_max"),
    "retention_job_succeeded_days": ("lte", "retention_job_failed_days"),
    "retention_job_failed_days": ("gte", "retention_job_succeeded_days"),
}

# English ``Param.pending`` text -> its Vietnamese text.
PENDING_VI: dict[str, str] = {
    "No reminder of the next tariff publication is built yet.": "Chưa có nhắc việc công bố bảng giá năm sau.",
    policy_params.SURCHARGES_PENDING: "Phụ thu sau xác nhận chưa được xây dựng.",
    "A recompute stores the computed prices directly; there is no approval step to skip yet.": (
        "Khi tính lại, giá tính toán được lưu ngay; chưa có bước duyệt nào để bỏ qua."
    ),
    "Provisional prices from last year's price are not built yet (the workbench shows the CIS price).": (
        "Giá tạm tính từ giá năm trước chưa được xây dựng (bàn làm giá hiển thị giá CIS)."
    ),
    "The CIS reference price is display only; quoting it is not built.": (
        "Giá tham khảo CIS chỉ để hiển thị; chưa thể báo giá theo giá này."
    ),
    "Free places (FOC) are not priced yet.": "Chỗ miễn phí (FOC) chưa được tính giá.",
    policy_params.TERMS_PENDING: (
        "Hoá đơn đại lý hiện theo điều khoản đặt cọc và thanh toán phần còn lại ở trang Hoá đơn đại lý."
    ),
    "The cancellation fee follows the cancellation fee schedule only today.": "Phí huỷ hiện chỉ theo bảng phí huỷ.",
    "Gala dinner lines are not added to quotes yet.": "Dòng gala bắt buộc chưa được thêm vào báo giá.",
    "New agencies are billed in USD; the CIS tenant account carries no country yet.": (
        "Đại lý mới được tính tiền bằng USD; tài khoản tenant trong CIS chưa có quốc gia."
    ),
}


def hints(param: policy_params.Param) -> dict[str, Any]:
    """The page hints of one parameter (merged into its row of ``GET /booking/pricing/policy``)."""
    bound = CROSS_BOUNDS.get(param.key)
    return {
        "cross_bound": {"relation": bound[0], "key": bound[1]} if bound else None,
        "not_enforced_reason_vi": PENDING_VI.get(param.pending) if param.pending else None,
    }
