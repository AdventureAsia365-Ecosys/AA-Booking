"""The operator's guides of the job check parameters (AA-1171, bk-063; :mod:`app.pricing.policy_params_ops`), in the
shape of :mod:`app.pricing.policy_guides` (split out of it: file size limit). Text only."""

from __future__ import annotations

PARTS = ("what", "raise", "lower", "when", "example")


def _g(en: tuple[str, str, str, str, str], vi: tuple[str, str, str, str, str]) -> dict[str, dict[str, str]]:
    return {"en": dict(zip(PARTS, en, strict=True)), "vi": dict(zip(PARTS, vi, strict=True))}


OPS_GUIDES: dict[str, dict[str, dict[str, str]]] = {
    "ops_jobs_stuck_after_s": _g(
        (
            "How long a due background job may wait while no healthy job is running before the job queue alert "
            "e-mails the platform alerts topic.",
            "The alert comes later: a stopped worker or a job kind no worker takes is noticed later.",
            "The alert comes sooner, also when the worker only finishes a long job a little late (more alerts).",
            "Raise it when long but normal waits (a large retention run) alert; lower it when jobs must start fast.",
            "Starting value 900 seconds (15 minutes).",
        ),
        (
            "Một job nền đến hạn được chờ bao lâu khi không có job nào đang chạy ổn trước khi cảnh báo hàng đợi job "
            "gửi e-mail lên topic cảnh báo của nền tảng.",
            "Cảnh báo tới muộn hơn: worker dừng hay một loại job không ai nhận bị phát hiện muộn hơn.",
            "Cảnh báo tới sớm hơn, kể cả khi worker chỉ xong một job dài hơi trễ (nhiều cảnh báo hơn).",
            "Tăng khi những lần chờ dài nhưng bình thường (một lượt dọn dữ liệu lớn) gây cảnh báo; giảm khi job cần "
            "chạy nhanh.",
            "Giá trị ban đầu 900 giây (15 phút).",
        ),
    ),
    "ops_jobs_overdue_factor": _g(
        (
            "How many times its kind's expected time a running background job may take before it counts as overdue; "
            "the job check alerts on any overdue job, because the serial worker waits behind it.",
            "Slow but healthy jobs stop alerting; a hung job holds the worker longer before anyone hears of it.",
            "A hung job is reported sooner, but a job that is only slow alerts too.",
            "Raise it when slow but finishing jobs alert often; lower it when a hung job must be caught quickly.",
            "Starting value 2: a kind expected to take 5 minutes is overdue after 10 minutes.",
        ),
        (
            "Một job nền đang chạy được phép chạy gấp bao nhiêu lần thời gian dự kiến của loại job đó trước khi bị coi "
            "là quá hạn; kiểm tra job cảnh báo mọi job quá hạn vì worker chạy tuần tự phải chờ nó.",
            "Job chậm nhưng vẫn khoẻ không còn gây cảnh báo; job bị treo giữ worker lâu hơn trước khi có người biết.",
            "Job bị treo được báo sớm hơn, nhưng job chỉ chậm cũng gây cảnh báo.",
            "Tăng khi job chậm nhưng vẫn xong thường xuyên gây cảnh báo; giảm khi cần bắt job treo thật nhanh.",
            "Giá trị ban đầu 2: một loại job dự kiến 5 phút bị coi là quá hạn sau 10 phút.",
        ),
    ),
}
