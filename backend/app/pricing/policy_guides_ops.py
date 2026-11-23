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
    "operable_exception_default_days": _g(
        (
            "How long a sale exception lasts when the administrator lets agencies buy a tour that cannot be operated "
            "yet (a night without a hotel, a supplier not active) and picks no other length.",
            "Exceptions last longer: fewer renewals, but a tour stays on sale longer with a gap ops must fill by hand.",
            "Exceptions end sooner: the gap is looked at again sooner, with more renewals.",
            "Raise it when supplier contracts usually take weeks; lower it when gaps are usually fixed within days.",
            "Starting value 14 days.",
        ),
        (
            "Một ngoại lệ bán kéo dài bao lâu khi quản trị viên cho đại lý mua một tour chưa vận hành được (một đêm "
            "chưa có khách sạn, một nhà cung cấp chưa hoạt động) mà không chọn thời hạn khác.",
            "Ngoại lệ kéo dài hơn: ít phải gia hạn, nhưng tour còn chỗ trống mà vận hành phải tự đặt tay lâu hơn.",
            "Ngoại lệ hết sớm hơn: chỗ trống được xem lại sớm hơn, phải gia hạn nhiều hơn.",
            "Tăng khi hợp đồng nhà cung cấp thường mất vài tuần; giảm khi chỗ trống thường được lấp trong vài ngày.",
            "Giá trị ban đầu 14 ngày.",
        ),
    ),
    "operable_exception_max_days": _g(
        (
            "The longest sale exception the administrator may set for a tour that cannot be operated yet.",
            "Longer exceptions are allowed: a gap may stay open on sale for longer.",
            "Every exception is short: the gap must be fixed or the exception renewed sooner.",
            "Raise it only when a known gap takes long to close; keep it short so every gap is looked at again.",
            "Starting value 30 days.",
        ),
        (
            "Thời hạn dài nhất quản trị viên được đặt cho một ngoại lệ bán của tour chưa vận hành được.",
            "Cho phép ngoại lệ dài hơn: một chỗ trống có thể còn mở bán lâu hơn.",
            "Mọi ngoại lệ đều ngắn: chỗ trống phải được lấp hoặc gia hạn ngoại lệ sớm hơn.",
            "Chỉ tăng khi một chỗ trống đã biết cần lâu mới đóng được; giữ ngắn để mọi chỗ trống được xem lại.",
            "Giá trị ban đầu 30 ngày.",
        ),
    ),
    "addon_days_ahead": _g(
        (
            "How many days ahead the optional activities (add-ons) of a place are listed for travellers and agencies; "
            "each listed day still needs a price in force.",
            "Travellers and agencies see add-ons further ahead (longer trips planned early); the list is longer.",
            "Only near dates show add-ons; trips planned far ahead see none until the date comes closer.",
            "Raise it when agencies plan trips more than a year ahead; lower it if far dates show prices not agreed.",
            "Starting value 365 days.",
        ),
        (
            "Hoạt động tuỳ chọn (add-on) của một địa điểm được liệt kê trước bao nhiêu ngày cho khách và đại lý; "
            "mỗi ngày được liệt kê vẫn phải có giá đang hiệu lực.",
            "Khách và đại lý thấy add-on xa hơn (chuyến lên kế hoạch sớm); danh sách dài hơn.",
            "Chỉ ngày gần mới hiện add-on; chuyến lên kế hoạch xa không thấy gì cho tới khi ngày đến gần.",
            "Tăng khi đại lý lên kế hoạch chuyến trước hơn một năm; giảm nếu ngày xa hiện giá chưa thoả thuận.",
            "Giá trị ban đầu 365 ngày.",
        ),
    ),
    "contract_expiry_alert_days": _g(
        (
            "How many days before a published supplier contract ends its staff are told it is expiring (a "
            "notification once per contract and end date, and the warning on the Add-ons page).",
            "The warning comes earlier: more time to agree next year's rates, more contracts listed at once.",
            "The warning comes later: fewer contracts listed, less time to renew before what they price stops selling.",
            "Raise it when suppliers take long to send new rates; lower it when the list is too long to act on.",
            "Starting value 60 days.",
        ),
        (
            "Hợp đồng nhà cung cấp đã xuất bản được báo sắp hết hạn trước ngày kết thúc bao nhiêu ngày (một thông báo "
            "cho mỗi hợp đồng và ngày kết thúc, và cảnh báo trên trang Add-on).",
            "Cảnh báo tới sớm hơn: nhiều thời gian thoả thuận giá năm sau hơn, nhiều hợp đồng hiện cùng lúc hơn.",
            "Cảnh báo tới muộn hơn: ít hợp đồng hơn, ít thời gian gia hạn trước khi những gì nó định giá ngừng bán.",
            "Tăng khi nhà cung cấp gửi giá mới chậm; giảm khi danh sách quá dài để xử lý.",
            "Giá trị ban đầu 60 ngày.",
        ),
    ),
}
