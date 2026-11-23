"""The operator's guide of the group-size bands of the computed price matrix (AA-1175, bk-066;
:mod:`app.pricing.pax_bands`), in the shape of :mod:`app.pricing.policy_guides` (split out of it: file size limit).
Text only."""

from __future__ import annotations

PARTS = ("what", "raise", "lower", "when", "example")


def _g(en: tuple[str, str, str, str, str], vi: tuple[str, str, str, str, str]) -> dict[str, dict[str, str]]:
    return {"en": dict(zip(PARTS, en, strict=True)), "vi": dict(zip(PARTS, vi, strict=True))}


PRICES_GUIDES: dict[str, dict[str, dict[str, str]]] = {
    "price_pax_bands": _g(
        (
            "The group sizes the computed price matrix of a tour is priced for. Each band gets its own price per "
            "person, worked out for the band's smallest group: fixed costs such as the car and the guide are shared by "
            "the fewest travellers there, so that price covers the whole band. The agency tariff lists one price per "
            "band. An agency quote does not read the bands: it prices the real group from the tour's costs, so it "
            "can be lower than the tariff row of that group's band. Hand prices of a price version or the workbook "
            "may use any group sizes and win over a computed row they cover.",
            "More or narrower bands: each group size gets a price closer to its real cost (4 travellers on their own "
            "pay less per person than in a 3-4 band priced for 3). The matrix holds more rows per date window, so "
            "fewer future date windows fit in its 200 rows (the latest are left out first).",
            "Fewer or wider bands: a shorter matrix with more future date windows; a group at the top of a wide band "
            "pays the price of its smallest group, which is higher per person.",
            "Change it when the business changes the group sizes it quotes. Computed prices change only at the next "
            "recompute (the automatic one after the approval or on the effective day, Recompute prices on Price "
            "setup, or Recompute from rates on a trip plan); until then the matrix keeps the rows of the old bands. "
            "A group of at least the size of Group quoted on request from is still quoted on request, whatever band "
            "holds it.",
            "Starting value 1, 2, 3, 4, 5-7, 8-10, 11+ travellers: the business owner's 2, 4, 5-7 and 8-10 bands, "
            "with 3 on its own and an open top band from 11.",
        ),
        (
            "Các cỡ đoàn mà ma trận giá tính tự động của một tour được tính cho. Mỗi bậc có giá riêng cho mỗi khách, "
            "tính cho đoàn nhỏ nhất của bậc: chi phí cố định như xe và hướng dẫn viên được chia cho ít khách nhất ở "
            "đó, nên giá ấy đủ cho cả bậc. Biểu giá đại lý ghi một giá cho mỗi bậc. Báo giá cho đại lý không đọc các "
            "bậc: nó tính giá cho đúng số khách của đoàn từ chi phí của tour, nên có thể thấp hơn dòng biểu giá của "
            "bậc chứa đoàn đó. Giá nhập tay trong phiên bản giá hoặc file Excel được dùng cỡ đoàn bất kỳ và thắng "
            "dòng tính tự động mà nó phủ.",
            "Nhiều bậc hơn hoặc bậc hẹp hơn: mỗi cỡ đoàn có giá sát chi phí thật hơn (4 khách đứng riêng trả mỗi người "
            "ít hơn so với bậc 3-4 tính cho 3 khách). Ma trận có nhiều dòng hơn cho mỗi khoảng ngày, nên ít khoảng "
            "ngày tương lai vừa 200 dòng của nó hơn (khoảng xa nhất bị bỏ trước).",
            "Ít bậc hơn hoặc bậc rộng hơn: ma trận ngắn hơn, nhiều khoảng ngày tương lai hơn; đoàn ở đầu trên của một "
            "bậc rộng trả giá của đoàn nhỏ nhất bậc đó, tính mỗi người cao hơn.",
            "Đổi khi doanh nghiệp đổi các cỡ đoàn mình báo giá. Giá tính tự động chỉ đổi ở lần tính lại kế tiếp (lần "
            "tự động sau khi duyệt hoặc vào ngày hiệu lực, Tính lại giá ở Thiết lập giá, hoặc Tính lại từ giá trên "
            "một kế hoạch chuyến đi); tới lúc đó ma trận giữ các dòng của bậc cũ. Đoàn từ cỡ Đoàn báo giá theo yêu "
            "cầu từ trở lên vẫn được báo giá theo yêu cầu, dù thuộc bậc nào.",
            "Giá trị ban đầu 1, 2, 3, 4, 5-7, 8-10, 11+ khách: các bậc 2, 4, 5-7 và 8-10 của chủ doanh nghiệp, thêm "
            "bậc 3 riêng và bậc trên cùng để mở từ 11.",
        ),
    ),
}
