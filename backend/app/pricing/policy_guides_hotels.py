"""The operator's guides of the hotel contracting parameters (AA-1153, bk-069; :mod:`app.pricing.policy_params_hotels`),
in the shape of :mod:`app.pricing.policy_guides` (split out of it: file size limit). Text only."""

from __future__ import annotations

PARTS = ("what", "raise", "lower", "when", "example")


def _g(en: tuple[str, str, str, str, str], vi: tuple[str, str, str, str, str]) -> dict[str, dict[str, str]]:
    return {"en": dict(zip(PARTS, en, strict=True)), "vi": dict(zip(PARTS, vi, strict=True))}


HOTELS_GUIDES: dict[str, dict[str, dict[str, str]]] = {
    "hotel_match_block_pct": _g(
        (
            "How similar the cleaned names of two hotels (lower case, no accents, no words such as hotel or resort) "
            "must be for Booking to refuse the new one as the same hotel, when they are also close together.",
            "Fewer hotels are refused: two spellings of one hotel are more likely to become two rows, two CIS texts "
            "and two photo sets.",
            "More hotels are refused: two different hotels with close names in one street may need the "
            "administrator to rename one before it can be saved.",
            "Raise it when real, different hotels are refused; lower it when duplicates still get in.",
            "Starting value 85 %.",
        ),
        (
            "Tên đã làm sạch của hai khách sạn (chữ thường, bỏ dấu, bỏ các từ như hotel, resort) phải giống nhau bao "
            "nhiêu để Booking từ chối khách sạn mới vì trùng, khi hai nơi cũng ở gần nhau.",
            "Ít khách sạn bị từ chối hơn: hai cách viết của một khách sạn dễ thành hai dòng, hai bài CIS "
            "và hai bộ ảnh.",
            "Nhiều khách sạn bị từ chối hơn: hai khách sạn khác nhau tên gần giống trên một con phố có thể phải đổi "
            "tên một bên mới lưu được.",
            "Tăng khi khách sạn thật sự khác nhau bị chặn; giảm khi vẫn lọt khách sạn trùng.",
            "Giá trị ban đầu 85 %.",
        ),
    ),
    "hotel_match_block_m": _g(
        (
            "The distance under which two hotels with very similar names are treated as the same hotel and the new "
            "one is refused.",
            "Hotels further apart are refused too: a chain's two branches in one district may be taken for one.",
            "Only hotels very close together are refused: one hotel entered with rough coordinates may slip in twice.",
            "Raise it when coordinates from supplier sheets are rough; lower it for chains with branches close by.",
            "Starting value 300 metres.",
        ),
        (
            "Khoảng cách dưới mức này thì hai khách sạn có tên rất giống nhau được coi là một và khách sạn mới bị từ "
            "chối.",
            "Cả các khách sạn xa hơn cũng bị chặn: hai chi nhánh của một chuỗi trong một quận có thể bị coi là một.",
            "Chỉ chặn khi rất gần: một khách sạn nhập toạ độ phỏng chừng có thể lọt vào hai lần.",
            "Tăng khi toạ độ từ bảng của nhà cung cấp không chính xác; giảm khi chuỗi khách sạn có chi nhánh gần nhau.",
            "Giá trị ban đầu 300 mét.",
        ),
    ),
    "hotel_match_warn_pct": _g(
        (
            'How similar the cleaned names must be for Booking to ask "is this a different hotel?" with a reason '
            "before it saves a hotel at the same place as another.",
            "Fewer questions: possible duplicates are saved without a second look.",
            "More questions: staff give a reason more often for hotels that are clearly different.",
            "Raise it when the question comes up for obviously different hotels; lower it when duplicates are found "
            "later in the list.",
            "Starting value 60 %.",
        ),
        (
            'Tên đã làm sạch phải giống nhau bao nhiêu để Booking hỏi "đây là khách sạn khác?" kèm lý do trước khi '
            "lưu một khách sạn ở cùng nơi với khách sạn khác.",
            "Ít câu hỏi hơn: khách sạn có thể trùng được lưu mà không ai xem lại.",
            "Nhiều câu hỏi hơn: nhân viên phải ghi lý do nhiều lần cho những khách sạn rõ ràng khác nhau.",
            "Tăng khi câu hỏi hiện ra cho khách sạn hiển nhiên khác nhau; giảm khi sau này vẫn thấy khách sạn trùng "
            "trong danh sách.",
            "Giá trị ban đầu 60 %.",
        ),
    ),
    "hotel_match_warn_m": _g(
        (
            "Two hotels at the same place and closer than this distance ask for a reason before the new one is saved, "
            "whatever their names.",
            "The question comes for hotels further apart: more reasons to type in a dense old town.",
            "Only very close hotels ask: a renamed hotel entered again under its new name may slip through.",
            "Raise it when renamed hotels come in twice; lower it in dense places where many hotels sit side by side.",
            "Starting value 1000 metres.",
        ),
        (
            "Hai khách sạn ở cùng nơi và gần hơn khoảng cách này thì phải ghi lý do trước khi lưu khách sạn mới, bất "
            "kể tên.",
            "Câu hỏi hiện ra cả với khách sạn xa hơn: ở phố cổ đông khách sạn phải ghi lý do nhiều hơn.",
            "Chỉ hỏi khi rất gần: một khách sạn đổi tên được nhập lại bằng tên mới có thể lọt qua.",
            "Tăng khi khách sạn đổi tên bị nhập hai lần; giảm ở nơi đông khách sạn sát nhau.",
            "Giá trị ban đầu 1000 mét.",
        ),
    ),
    "hotel_facts_reconfirm_days": _g(
        (
            "How long after the last confirmation of a hotel's facts (from the supplier's sheet, the hotel's website "
            "or a visit) the hotel shows as due for a new confirmation in the hotel list.",
            "Confirmations are asked less often: a closed or renamed hotel stays unnoticed longer.",
            "Confirmations are asked more often: more work for contracting, fresher facts for the CIS texts.",
            "Lower it when suppliers report many changes; raise it when the facts rarely move and the list is noisy.",
            "Starting value 180 days.",
        ),
        (
            "Bao lâu sau lần xác nhận cuối thông tin một khách sạn (từ bảng của nhà cung cấp, website khách sạn hay "
            "một lần đến tận nơi) thì khách sạn hiện là cần xác nhận lại trong danh sách khách sạn.",
            "Ít phải xác nhận lại hơn: khách sạn đóng cửa hay đổi tên lâu bị phát hiện hơn.",
            "Phải xác nhận lại thường hơn: thêm việc cho nhóm hợp đồng, thông tin cho bài CIS mới hơn.",
            "Giảm khi nhà cung cấp báo nhiều thay đổi; tăng khi thông tin ít đổi mà danh sách báo quá nhiều.",
            "Giá trị ban đầu 180 ngày.",
        ),
    ),
    "rate_sheet_jump_warn_pct": _g(
        (
            "A price in an imported rate sheet this much above the same rate of the supplier's previous contract is "
            "flagged in the import preview and must be acknowledged by the person who publishes the contract.",
            "Only big rises are flagged: a typing error that adds a digit may still be caught, smaller mistakes not.",
            "Normal yearly rises are flagged too: the publisher acknowledges more lines.",
            "Lower it when wrong prices reached quotes; raise it when every new season flags normal increases.",
            "Starting value 60 %.",
        ),
        (
            "Một giá trong bảng giá nhập vào cao hơn cùng giá đó ở hợp đồng trước của nhà cung cấp từ mức này trở lên "
            "thì được đánh dấu khi xem trước và người công bố hợp đồng phải xác nhận đã xem.",
            "Chỉ đánh dấu những lần tăng lớn: lỗi gõ thừa một chữ số vẫn bị bắt, lỗi nhỏ hơn thì không.",
            "Cả mức tăng bình thường hằng năm cũng bị đánh dấu: người công bố phải xác nhận nhiều dòng hơn.",
            "Giảm khi giá sai đã lọt vào báo giá; tăng khi mỗi mùa mới đều đánh dấu những lần tăng bình thường.",
            "Giá trị ban đầu 60 %.",
        ),
    ),
}
