"""The operator's guide of every pricing policy parameter (AA-1096 l23; tech lead S279: "each setting needs an
explanation or definition so I know when to change it").

One guide per key of :data:`app.pricing.policy_params.PARAMS`, in English and Vietnamese, in the shape every settings
page of the platform uses (AA-CIS-App CONTEXT.md, "Settings registry: every key ships its guide"):

    {"en": {"what", "raise", "lower", "when", "example"}, "vi": {...the same five parts}}

``what``: what the value controls and where the effect shows in Booking; ``raise`` / ``lower``: the effect and risk of
each direction (a yes/no value: switch it on / off; a list, a map or a date: the stricter / the looser choice, which
the text names); ``when``: the signal to change it; ``example``: the approved starting value (bk-050 / bk-056) in plain
terms. The page shows the guide under the label, behind an Explain toggle and in the change drawer
(frontend Pricing > Policy). ``tests/unit/test_aa1096_policy_guides.py`` fails when a parameter has no complete guide.

Text only, no logic: the definitions (types, ranges, approvers) stay in ``policy_params``.
"""

from __future__ import annotations

PARTS = ("what", "raise", "lower", "when", "example")


def _g(en: tuple[str, str, str, str, str], vi: tuple[str, str, str, str, str]) -> dict[str, dict[str, str]]:
    return {"en": dict(zip(PARTS, en, strict=True)), "vi": dict(zip(PARTS, vi, strict=True))}


GUIDES: dict[str, dict[str, dict[str, str]]] = {
    "tariff_year_start": _g(
        (
            "The first day of the agency tariff year. Agency prices are versioned per tariff year, which runs from "
            "this day to the day before it next year.",
            "A later start day moves the whole tariff year later: this year's prices stay in force longer.",
            "An earlier start day moves the tariff year earlier: next year's prices take over sooner.",
            "Change it only when the company changes its contract year with agencies, before the next year is built.",
            "Today 11-01: the tariff year runs from 1 November to 31 October.",
        ),
        (
            "Ngày đầu năm biểu giá cho đại lý. Giá cho đại lý được đánh phiên bản theo năm biểu giá, chạy từ ngày này "
            "tới ngày trước nó của năm sau.",
            "Ngày bắt đầu muộn hơn đẩy cả năm biểu giá lùi lại: giá năm nay được dùng lâu hơn.",
            "Ngày bắt đầu sớm hơn đẩy năm biểu giá lên sớm: giá năm sau được áp dụng sớm hơn.",
            "Chỉ đổi khi công ty đổi năm hợp đồng với đại lý, trước khi dựng năm kế tiếp.",
            "Hiện là 11-01: năm biểu giá chạy từ 1 tháng 11 tới 31 tháng 10.",
        ),
    ),
    "tariff_next_publish_by": _g(
        (
            "The day by which the next tariff year should be published to agencies. A reminder date only: it blocks "
            "nothing.",
            "A later date gives the pricing team more time, and agencies see next year's prices later.",
            "An earlier date gets next year's prices to agencies sooner, with less time to prepare them.",
            "Change it when agencies ask for next year's prices earlier, or the pricing calendar moves.",
            "Today 08-31: the next tariff year should be out by 31 August.",
        ),
        (
            "Ngày mà năm biểu giá kế tiếp nên được công bố cho đại lý. Chỉ là ngày nhắc, không chặn việc gì.",
            "Ngày muộn hơn cho đội giá thêm thời gian, đại lý thấy giá năm sau muộn hơn.",
            "Ngày sớm hơn đưa giá năm sau tới đại lý sớm hơn, nhưng ít thời gian chuẩn bị hơn.",
            "Đổi khi đại lý muốn có giá năm sau sớm hơn, hoặc lịch làm giá thay đổi.",
            "Hiện là 08-31: năm biểu giá kế tiếp nên công bố trước 31 tháng 8.",
        ),
    ),
    "quote_validity_days": _g(
        (
            "How many days a B2B quote stays valid after it is sent. After that the agency must ask for a new quote "
            "at the prices then in force.",
            "Agencies have longer to confirm; the company carries the risk of a price or FX move for longer.",
            "Quotes expire sooner, so prices stay closer to costs; agencies must confirm faster or re-quote.",
            "Change it when agencies often re-quote only because time ran out (raise it), or when costs move fast "
            "(lower it).",
            "Today 14 days.",
        ),
        (
            "Một báo giá B2B còn hiệu lực bao nhiêu ngày sau khi gửi. Quá hạn, đại lý phải xin báo giá mới theo giá "
            "đang áp dụng lúc đó.",
            "Đại lý có thêm thời gian xác nhận; công ty chịu rủi ro giá hoặc tỷ giá biến động lâu hơn.",
            "Báo giá hết hạn sớm hơn nên giá sát chi phí hơn; đại lý phải xác nhận nhanh hơn hoặc xin báo giá lại.",
            "Đổi khi đại lý thường phải xin lại báo giá chỉ vì hết hạn (tăng), hoặc khi chi phí biến động nhanh "
            "(giảm).",
            "Hiện là 14 ngày.",
        ),
    ),
    "quote_validity_festive_days": _g(
        (
            "The validity of a quote that touches a festive period of the season calendar (Tet, Christmas, New Year). "
            "It may not be longer than the normal quote validity.",
            "Festive quotes hold longer, while festive capacity is scarce and suppliers may not honour the price.",
            "Festive quotes expire sooner, so seats and rooms are confirmed or released quickly.",
            "Change it when festive quotes are often confirmed too late to hold the space (lower it).",
            "Today 3 days.",
        ),
        (
            "Thời hạn của báo giá có dính tới mùa lễ trong lịch mùa (Tết, Giáng sinh, Năm mới). Không được dài hơn "
            "thời hạn báo giá thường.",
            "Báo giá mùa lễ giữ lâu hơn, trong khi chỗ mùa lễ khan hiếm và nhà cung cấp có thể không giữ giá.",
            "Báo giá mùa lễ hết hạn sớm hơn, nên chỗ được xác nhận hoặc trả lại nhanh.",
            "Đổi khi báo giá mùa lễ thường được xác nhận quá muộn để giữ chỗ (giảm).",
            "Hiện là 3 ngày.",
        ),
    ),
    "surcharge_causes": _g(
        (
            "The only reasons that may add a surcharge line to an already confirmed booking (fuel, FX, tax). Empty "
            "means a confirmed price never changes.",
            "More causes (the looser choice for the company): more cost changes can be passed on to agencies.",
            "Fewer causes (the stricter choice): agencies get firmer prices and the company absorbs the other changes.",
            "Change it when the contract terms with agencies change which costs may be passed on.",
            "Today fx and tax: an FX move or a tax change may add a surcharge.",
        ),
        (
            "Những lý do duy nhất được phép thêm dòng phụ thu vào một booking đã xác nhận (nhiên liệu, tỷ giá, thuế). "
            "Để trống nghĩa là giá đã xác nhận không bao giờ đổi.",
            "Thêm lý do (lựa chọn nới hơn cho công ty): nhiều biến động chi phí được chuyển sang đại lý hơn.",
            "Bớt lý do (lựa chọn chặt hơn): đại lý có giá chắc hơn, công ty tự chịu các biến động khác.",
            "Đổi khi điều khoản hợp đồng với đại lý đổi loại chi phí được chuyển sang.",
            "Hiện là fx và tax: biến động tỷ giá hoặc thay đổi thuế được phép thêm phụ thu.",
        ),
    ),
    "surcharge_fx_threshold_pct": _g(
        (
            "An FX move only allows a surcharge when the rate has moved more than this percentage against the rate "
            "locked on the booking.",
            "Only large FX moves are passed on; the company absorbs more of the smaller ones.",
            "Smaller FX moves can be passed on; agencies see more surcharges.",
            "Change it when the currency becomes more or less volatile, or the agency terms name another threshold.",
            "Today 5 %.",
        ),
        (
            "Biến động tỷ giá chỉ được phép phụ thu khi tỷ giá lệch quá phần trăm này so với tỷ giá đã chốt trên "
            "booking.",
            "Chỉ biến động lớn mới được chuyển sang đại lý; công ty chịu phần lớn biến động nhỏ.",
            "Biến động nhỏ hơn cũng được chuyển sang; đại lý gặp phụ thu nhiều hơn.",
            "Đổi khi đồng tiền biến động nhiều hay ít hơn, hoặc điều khoản đại lý nêu ngưỡng khác.",
            "Hiện là 5 %.",
        ),
    ),
    "surcharge_notice_days": _g(
        (
            "How many days before departure an agency must be told of a surcharge at the latest. The law sets 20 days "
            "as the minimum.",
            "Agencies are warned earlier; late cost changes can no longer be passed on.",
            "Surcharges can be announced closer to departure, never under the legal 20 days.",
            "Change it when the agency terms promise a longer notice.",
            "Today 20 days, the legal minimum.",
        ),
        (
            "Chậm nhất bao nhiêu ngày trước khởi hành phải báo đại lý về phụ thu. Luật đặt tối thiểu 20 ngày.",
            "Đại lý được báo sớm hơn; biến động chi phí muộn không còn được chuyển sang.",
            "Phụ thu được báo sát ngày khởi hành hơn, nhưng không dưới 20 ngày theo luật.",
            "Đổi khi điều khoản với đại lý hứa thời gian báo trước dài hơn.",
            "Hiện là 20 ngày, mức tối thiểu theo luật.",
        ),
    ),
    "surcharge_free_cancel_pct": _g(
        (
            "If a surcharge is more than this share of the booking price, the agency may cancel free of charge. The "
            "law sets 8 % as the maximum.",
            "Agencies must accept bigger surcharges before they may cancel free (never above the legal 8 %).",
            "Agencies may cancel free after a smaller surcharge; more cancellations after a cost rise.",
            "Change it when the agency terms promise a lower threshold.",
            "Today 8 %, the legal maximum.",
        ),
        (
            "Nếu phụ thu lớn hơn tỷ lệ này của giá booking, đại lý được huỷ miễn phí. Luật đặt tối đa 8 %.",
            "Đại lý phải chấp nhận phụ thu lớn hơn mới được huỷ miễn phí (không vượt 8 % theo luật).",
            "Đại lý được huỷ miễn phí sau phụ thu nhỏ hơn; nhiều huỷ hơn khi chi phí tăng.",
            "Đổi khi điều khoản với đại lý hứa ngưỡng thấp hơn.",
            "Hiện là 8 %, mức tối đa theo luật.",
        ),
    ),
    "recompute_auto_publish_pct": _g(
        (
            "When prices are recomputed (new costs, new FX), a new price version publishes by itself if every row "
            "moves less than this percentage; a bigger move waits for an approver. 0 = always wait.",
            "More recomputes publish without a person checking; a wrong cost slips through more easily.",
            "More recomputes wait for an approver; prices are checked more, published later.",
            "Change it when approvers only rubber-stamp small moves (raise it), or after a wrong price went out "
            "unchecked (lower it).",
            "Today 3 %.",
        ),
        (
            "Khi giá được tính lại (chi phí mới, tỷ giá mới), phiên bản giá mới tự công bố nếu mọi dòng thay đổi dưới "
            "phần trăm này; thay đổi lớn hơn chờ người duyệt. 0 = luôn chờ.",
            "Nhiều lần tính lại được công bố mà không ai kiểm; giá sai dễ lọt hơn.",
            "Nhiều lần tính lại phải chờ người duyệt; giá được kiểm kỹ hơn, công bố muộn hơn.",
            "Đổi khi người duyệt chỉ bấm duyệt cho các thay đổi nhỏ (tăng), hoặc sau khi một giá sai đã được công bố "
            "mà không ai kiểm (giảm).",
            "Hiện là 3 %.",
        ),
    ),
    "diff_highlight_pct": _g(
        (
            "In a price change waiting for approval, rows that move more than this percentage are highlighted for the "
            "approver.",
            "Only big moves stand out; the approver may miss a medium change.",
            "More rows are highlighted; big moves are harder to spot among many highlights.",
            "Change it when approvers say the highlight marks too many or too few rows.",
            "Today 10 %.",
        ),
        (
            "Trong một thay đổi giá đang chờ duyệt, các dòng thay đổi quá phần trăm này được tô nổi cho người duyệt.",
            "Chỉ thay đổi lớn mới nổi bật; người duyệt có thể bỏ sót thay đổi vừa.",
            "Nhiều dòng được tô hơn; thay đổi lớn khó nhận ra giữa nhiều dòng tô.",
            "Đổi khi người duyệt thấy phần tô nổi đánh dấu quá nhiều hoặc quá ít dòng.",
            "Hiện là 10 %.",
        ),
    ),
    "provisional_uplift_pct": _g(
        (
            "Before next year's real prices are in, a provisional price is last year's price of the same season plus "
            "this percentage.",
            "Provisional prices are safer against cost rises, and look more expensive to agencies.",
            "Provisional prices look cheaper, with more risk of being under the real cost later.",
            "Change it when supplier costs for next year are expected to rise more or less than usual.",
            "Today 5 %.",
        ),
        (
            "Trước khi có giá thật của năm sau, giá tạm tính bằng giá cùng mùa năm trước cộng thêm phần trăm này.",
            "Giá tạm an toàn hơn trước việc chi phí tăng, nhưng trông đắt hơn với đại lý.",
            "Giá tạm trông rẻ hơn, nhưng dễ thấp hơn chi phí thật về sau.",
            "Đổi khi dự kiến chi phí nhà cung cấp năm sau tăng nhiều hay ít hơn bình thường.",
            "Hiện là 5 %.",
        ),
    ),
    "provisional_cis_ref_quotable": _g(
        (
            "Whether a provisional price taken from the CIS reference price may be quoted to agencies. No: it is "
            "shown for information only.",
            "Yes: agencies can get a quote from the reference price before a real price exists; it may be wrong.",
            "No: only real or last-year-based prices are quoted; the reference stays display only.",
            "Switch it on only when the reference prices have been checked against real costs.",
            "Today off: the CIS reference price is display only.",
        ),
        (
            "Có cho phép báo giá cho đại lý theo giá tạm lấy từ giá tham khảo của CIS không. Không: chỉ hiển thị để "
            "tham khảo.",
            "Có: đại lý nhận được báo giá từ giá tham khảo trước khi có giá thật; giá đó có thể sai.",
            "Không: chỉ báo giá theo giá thật hoặc giá dựa trên năm trước; giá tham khảo chỉ để xem.",
            "Chỉ bật khi giá tham khảo đã được đối chiếu với chi phí thật.",
            "Hiện đang tắt (off): giá tham khảo của CIS chỉ để xem.",
        ),
    ),
    "group_on_request_min_pax": _g(
        (
            "A tour line with this many travellers or more is quoted on request: the desk checks space and prices by "
            "hand instead of quoting at once.",
            "Bigger groups still get an instant quote; more risk that a large group does not fit the space.",
            "More groups go to the desk; safer for space and price, slower for the agency.",
            "Change it when the desk is flooded with groups it always accepts (raise it), or instant quotes for groups "
            "fail on space (lower it).",
            "Today 9 travellers.",
        ),
        (
            "Một dòng tour có từ chừng này khách trở lên sẽ được báo giá theo yêu cầu: bộ phận điều hành kiểm chỗ và "
            "giá bằng tay thay vì báo giá ngay.",
            "Nhóm lớn hơn vẫn được báo giá ngay; rủi ro nhóm lớn không đủ chỗ cao hơn.",
            "Nhiều nhóm chuyển sang điều hành hơn; an toàn hơn về chỗ và giá, nhưng chậm hơn cho đại lý.",
            "Đổi khi bộ phận điều hành ngập các nhóm luôn được chấp nhận (tăng), hoặc báo giá ngay cho nhóm hay hỏng "
            "vì thiếu chỗ (giảm).",
            "Hiện là 9 khách.",
        ),
    ),
    "foc_one_per_pax": _g(
        (
            "When a supplier contract names no free places (FOC), one free place is given per this many travellers.",
            "Fewer free places: better margin, a less generous offer to groups.",
            "More free places: groups get more free spots, the margin is lower.",
            "Change it when the usual supplier terms or the market practice for groups change.",
            "Today one free place per 15 travellers.",
        ),
        (
            "Khi hợp đồng nhà cung cấp không nêu suất miễn phí (FOC), cứ chừng này khách thì có một suất miễn phí.",
            "Ít suất miễn phí hơn: biên lợi nhuận tốt hơn, ưu đãi cho nhóm kém hơn.",
            "Nhiều suất miễn phí hơn: nhóm được thêm suất miễn phí, biên lợi nhuận thấp hơn.",
            "Đổi khi điều khoản thường gặp của nhà cung cấp hoặc thông lệ thị trường cho nhóm thay đổi.",
            "Hiện là một suất miễn phí cho mỗi 15 khách.",
        ),
    ),
    "fit_full_payment_days": _g(
        (
            "How many days before departure an individual (FIT) booking must be paid in full.",
            "Payment is due earlier: less risk of an unpaid trip, agencies pay sooner.",
            "Payment is due closer to departure: easier for agencies, more risk for the company.",
            "Change it when unpaid trips near departure become a problem (raise it), or agency terms are eased "
            "(lower it).",
            "Today 30 days before departure.",
        ),
        (
            "Booking khách lẻ (FIT) phải thanh toán đủ trước khởi hành bao nhiêu ngày.",
            "Hạn thanh toán sớm hơn: ít rủi ro chuyến chưa trả tiền, đại lý phải trả sớm hơn.",
            "Hạn thanh toán sát ngày khởi hành hơn: dễ cho đại lý, rủi ro cho công ty cao hơn.",
            "Đổi khi chuyến chưa thanh toán gần ngày khởi hành thành vấn đề (tăng), hoặc nới điều khoản đại lý (giảm).",
            "Hiện là 30 ngày trước khởi hành.",
        ),
    ),
    "group_deposit_pct": _g(
        (
            "The deposit a group booking pays, as a percentage of its price.",
            "A bigger deposit covers more of the supplier commitments; agencies pay more up front.",
            "A smaller deposit is easier for agencies; the company carries more if the group cancels.",
            "Change it when supplier deposits for groups change, or group cancellations cost too much.",
            "Today 25 % of the booking price.",
        ),
        (
            "Khoản cọc một booking đoàn phải trả, tính theo phần trăm giá.",
            "Cọc lớn hơn bù được nhiều cam kết với nhà cung cấp hơn; đại lý phải trả trước nhiều hơn.",
            "Cọc nhỏ hơn dễ cho đại lý; công ty chịu nhiều hơn nếu đoàn huỷ.",
            "Đổi khi tiền cọc của nhà cung cấp cho đoàn thay đổi, hoặc đoàn huỷ gây tốn kém quá nhiều.",
            "Hiện là 25 % giá booking.",
        ),
    ),
    "group_deposit_days": _g(
        (
            "How many days before departure the group deposit is due.",
            "The deposit is due earlier, which secures the group sooner.",
            "The deposit is due later; the company holds space longer without money.",
            "Change it when suppliers ask for group deposits on another schedule.",
            "Today 56 days before departure.",
        ),
        (
            "Tiền cọc của đoàn đến hạn trước khởi hành bao nhiêu ngày.",
            "Hạn cọc sớm hơn, đoàn được chốt sớm hơn.",
            "Hạn cọc muộn hơn; công ty giữ chỗ lâu hơn mà chưa có tiền.",
            "Đổi khi nhà cung cấp yêu cầu cọc cho đoàn theo lịch khác.",
            "Hiện là 56 ngày trước khởi hành.",
        ),
    ),
    "festive_non_refundable_days": _g(
        (
            "A booking in a festive period is non-refundable within this many days of departure.",
            "The non-refundable window starts earlier: more protection for festive commitments, stricter for agencies.",
            "Agencies may cancel festive trips with a refund closer to departure; more risk for the company.",
            "Change it when festive suppliers change their own cancellation terms.",
            "Today 30 days before departure.",
        ),
        (
            "Booking trong mùa lễ không được hoàn tiền nếu còn chừng này ngày tới khởi hành.",
            "Khoảng không hoàn tiền bắt đầu sớm hơn: bảo vệ cam kết mùa lễ tốt hơn, chặt hơn với đại lý.",
            "Đại lý được huỷ có hoàn tiền sát ngày khởi hành hơn; rủi ro cho công ty cao hơn.",
            "Đổi khi nhà cung cấp mùa lễ đổi điều khoản huỷ của họ.",
            "Hiện là 30 ngày trước khởi hành.",
        ),
    ),
    "gala_dinner_days": _g(
        (
            "The days on which a compulsory gala dinner is charged, at cost plus the standard margin.",
            "More days (the stricter choice for agencies): more trips carry a compulsory dinner charge.",
            "Fewer days (the looser choice): fewer compulsory charges; the hotels' own gala fees may go unbilled.",
            "Change it when hotels announce compulsory gala dinners on other days.",
            "Today 12-24 and 12-31 (24 and 31 December).",
        ),
        (
            "Những ngày có tính phí ăn tối gala bắt buộc, theo giá vốn cộng biên lợi nhuận chuẩn.",
            "Thêm ngày (lựa chọn chặt hơn với đại lý): nhiều chuyến có phí ăn tối bắt buộc hơn.",
            "Bớt ngày (lựa chọn nới hơn): ít phí bắt buộc hơn; phí gala của khách sạn có thể không được tính.",
            "Đổi khi khách sạn thông báo tiệc gala bắt buộc vào ngày khác.",
            "Hiện là 12-24 và 12-31 (24 và 31 tháng 12).",
        ),
    ),
    "agency_currency_by_country": _g(
        (
            "The currency an agency is quoted in, by the agency's country; * covers every other country.",
            "More countries with their own currency (the stricter choice): agencies pay in local money, the company "
            "carries more FX work.",
            "Fewer entries (the looser choice): more agencies are quoted in the default currency.",
            "Change it when agencies of a country ask to be invoiced in their own currency.",
            "Today VN: VND, every other country: USD.",
        ),
        (
            "Đồng tiền báo giá cho đại lý theo quốc gia của đại lý; * là mọi quốc gia còn lại.",
            "Thêm quốc gia có đồng tiền riêng (lựa chọn chặt hơn): đại lý trả bằng tiền địa phương, công ty xử lý "
            "tỷ giá nhiều hơn.",
            "Bớt mục (lựa chọn nới hơn): nhiều đại lý được báo giá bằng đồng tiền mặc định hơn.",
            "Đổi khi đại lý của một nước muốn nhận hoá đơn bằng đồng tiền của họ.",
            "Hiện là VN: VND, mọi nước khác: USD.",
        ),
    ),
    "b2b_hold_hours": _g(
        (
            "How long a B2B hold keeps its seats on a departure before they are released, never past the departure "
            "start.",
            "Agencies have longer to confirm; held seats are blocked for other agencies longer.",
            "Seats come back to sale sooner; agencies must confirm faster.",
            "Change it when many holds expire just before agencies confirm (raise it), or popular departures are "
            "blocked by idle holds (lower it).",
            "Today 48 hours.",
        ),
        (
            "Một lượt giữ chỗ B2B giữ ghế trên một chuyến khởi hành bao lâu trước khi trả lại, không quá giờ khởi "
            "hành.",
            "Đại lý có thêm thời gian xác nhận; ghế bị giữ khoá với đại lý khác lâu hơn.",
            "Ghế được mở bán lại sớm hơn; đại lý phải xác nhận nhanh hơn.",
            "Đổi khi nhiều lượt giữ chỗ hết hạn ngay trước khi đại lý xác nhận (tăng), hoặc chuyến đông khách bị khoá "
            "bởi các lượt giữ bỏ không (giảm).",
            "Hiện là 48 giờ.",
        ),
    ),
    "b2b_hold_extend_hours": _g(
        (
            "How many hours a hold gains when the agency extends it, never past the departure start.",
            "An extension buys more time; seats stay blocked longer.",
            "An extension buys less time; seats return sooner.",
            "Change it when agencies extend and still run out of time (raise it).",
            "Today 72 hours per extension.",
        ),
        (
            "Một lượt giữ chỗ được thêm bao nhiêu giờ khi đại lý gia hạn, không quá giờ khởi hành.",
            "Mỗi lần gia hạn được thêm nhiều thời gian hơn; ghế bị khoá lâu hơn.",
            "Mỗi lần gia hạn được ít thời gian hơn; ghế được trả lại sớm hơn.",
            "Đổi khi đại lý đã gia hạn mà vẫn không kịp (tăng).",
            "Hiện là 72 giờ mỗi lần gia hạn.",
        ),
    ),
    "b2b_hold_max_extensions": _g(
        (
            "How many times one hold may be extended. 0 = never; the database allows at most 1.",
            "1: an agency may extend a hold once.",
            "0: holds cannot be extended; seats return at the end of the first hold.",
            "Set 0 when extensions keep popular departures blocked; keep 1 otherwise.",
            "Today 1 extension per hold.",
        ),
        (
            "Một lượt giữ chỗ được gia hạn tối đa mấy lần. 0 = không bao giờ; cơ sở dữ liệu cho phép tối đa 1.",
            "1: đại lý được gia hạn một lần.",
            "0: không được gia hạn; ghế được trả lại khi hết lượt giữ đầu tiên.",
            "Đặt 0 khi việc gia hạn làm khoá các chuyến đông khách; nếu không thì giữ 1.",
            "Hiện là 1 lần gia hạn mỗi lượt giữ.",
        ),
    ),
    "b2b_rehold_limit_per_day": _g(
        (
            "How many times per UTC day an agency may hold the same proposal and departure again after a hold ended. "
            "Past it the next hold is refused.",
            "Agencies can keep re-holding; seats can be blocked in a loop.",
            "Re-holding is refused sooner; protects seats, may annoy an agency still deciding.",
            "Change it when an agency blocks seats by re-holding all day (lower it).",
            "Today 3 re-holds per day.",
        ),
        (
            "Mỗi ngày (giờ UTC) đại lý được giữ chỗ lại cùng một đề xuất và chuyến khởi hành sau khi lượt giữ trước "
            "kết thúc tối đa bao nhiêu lần. Vượt quá, lượt giữ tiếp theo bị từ chối.",
            "Đại lý giữ lại được nhiều lần; ghế có thể bị khoá liên tục.",
            "Việc giữ lại bị từ chối sớm hơn; bảo vệ ghế, có thể làm phiền đại lý còn đang cân nhắc.",
            "Đổi khi một đại lý khoá ghế bằng cách giữ lại suốt ngày (giảm).",
            "Hiện là 3 lần giữ lại mỗi ngày.",
        ),
    ),
    "b2b_on_request_answer_business_days": _g(
        (
            "The business days (in the operations time zone) the desk has to accept or decline an on-request line "
            "of a booking.",
            "The desk has more time; agencies wait longer for an answer.",
            "Agencies get an answer sooner; the desk must answer faster.",
            "Change it when the desk often misses the deadline (raise it), or agencies complain about waiting "
            "(lower it).",
            "Today 1 business day.",
        ),
        (
            "Số ngày làm việc (theo múi giờ điều hành) bộ phận điều hành có để nhận hoặc từ chối một dòng theo yêu "
            "cầu của booking.",
            "Bộ phận điều hành có thêm thời gian; đại lý chờ câu trả lời lâu hơn.",
            "Đại lý nhận câu trả lời sớm hơn; điều hành phải trả lời nhanh hơn.",
            "Đổi khi điều hành thường trễ hạn (tăng), hoặc đại lý phàn nàn phải chờ (giảm).",
            "Hiện là 1 ngày làm việc.",
        ),
    ),
    "b2b_on_request_due_soon_hours": _g(
        (
            "An on-request line shows as due soon on the desk this many hours before its answer deadline.",
            "The warning comes earlier; more lines show as due soon at once.",
            "The warning comes later; the desk has less time once it appears.",
            "Change it when the desk misses deadlines although lines were marked (raise it).",
            "Today 4 hours before the deadline.",
        ),
        (
            "Một dòng theo yêu cầu hiện là sắp đến hạn trên màn hình điều hành trước hạn trả lời chừng này giờ.",
            "Cảnh báo đến sớm hơn; nhiều dòng cùng hiện sắp đến hạn hơn.",
            "Cảnh báo đến muộn hơn; điều hành có ít thời gian hơn khi cảnh báo hiện.",
            "Đổi khi điều hành vẫn trễ hạn dù dòng đã được đánh dấu (tăng).",
            "Hiện là 4 giờ trước hạn.",
        ),
    ),
    "b2b_child_age_max": _g(
        (
            "The oldest age charged at the child price when a trip plan sets no age of its own. Must stay above the "
            "infant age.",
            "Older children get the child price; cheaper for families, lower margin.",
            "Children pay the adult price from a younger age.",
            "Change it when suppliers change their child age bands.",
            "Today 11 years.",
        ),
        (
            "Tuổi lớn nhất được tính giá trẻ em khi lịch trình không tự đặt tuổi. Phải lớn hơn tuổi em bé.",
            "Trẻ lớn hơn vẫn được giá trẻ em; rẻ hơn cho gia đình, biên lợi nhuận thấp hơn.",
            "Trẻ phải trả giá người lớn từ tuổi nhỏ hơn.",
            "Đổi khi nhà cung cấp đổi khung tuổi trẻ em.",
            "Hiện là 11 tuổi.",
        ),
    ),
    "b2b_infant_age_max": _g(
        (
            "The oldest age charged at the infant price when a trip plan sets no age of its own. Must stay below the "
            "child age.",
            "Older toddlers get the infant price.",
            "Toddlers pay the child price from a younger age.",
            "Change it when suppliers change their infant age band.",
            "Today 1 year.",
        ),
        (
            "Tuổi lớn nhất được tính giá em bé khi lịch trình không tự đặt tuổi. Phải nhỏ hơn tuổi trẻ em.",
            "Bé lớn hơn vẫn được giá em bé.",
            "Bé phải trả giá trẻ em từ tuổi nhỏ hơn.",
            "Đổi khi nhà cung cấp đổi khung tuổi em bé.",
            "Hiện là 1 tuổi.",
        ),
    ),
    "departure_decision_days_default": _g(
        (
            "A new departure decides whether it runs or is cancelled this many days before it starts, unless staff "
            "set another day on it.",
            "The go / cancel decision comes earlier: travellers know sooner, fewer late bookings can save a departure.",
            "The decision comes later: more time to fill the departure, less notice if it is cancelled.",
            "Change it when departures are cancelled too late for travellers to rearrange (raise it), or would have "
            "filled with a few more days (lower it).",
            "Today 21 days before the start.",
        ),
        (
            "Một chuyến khởi hành mới quyết định chạy hay huỷ trước ngày bắt đầu chừng này ngày, trừ khi nhân viên đặt "
            "ngày khác cho chuyến đó.",
            "Quyết định chạy / huỷ đến sớm hơn: khách biết sớm hơn, ít cơ hội để booking muộn cứu chuyến.",
            "Quyết định đến muộn hơn: thêm thời gian bán cho đủ khách, báo huỷ cho khách muộn hơn.",
            "Đổi khi chuyến bị huỷ quá muộn để khách kịp xếp lại (tăng), hoặc thêm vài ngày là đủ khách (giảm).",
            "Hiện là 21 ngày trước ngày bắt đầu.",
        ),
    ),
    "pre_departure_days": _g(
        (
            "How many days before the start a fully booked trip case moves to pre-departure, which also sets when its "
            "standard trip checks are due.",
            "Pre-departure work (final documents, checks) starts earlier.",
            "Pre-departure starts later; less time for the final checks.",
            "Change it when final checks are often rushed (raise it).",
            "Today 14 days before the start.",
        ),
        (
            "Hồ sơ chuyến đã đặt đủ chuyển sang giai đoạn trước khởi hành trước ngày bắt đầu bao nhiêu ngày; số này "
            "cũng quyết định hạn của các bước kiểm tra chuẩn của chuyến.",
            "Việc trước khởi hành (giấy tờ cuối, kiểm tra) bắt đầu sớm hơn.",
            "Giai đoạn trước khởi hành bắt đầu muộn hơn; ít thời gian cho các bước kiểm tra cuối.",
            "Đổi khi các bước kiểm tra cuối thường bị gấp (tăng).",
            "Hiện là 14 ngày trước ngày bắt đầu.",
        ),
    ),
    "supplier_answer_days": _g(
        (
            "How many days a requested supplier booking may wait for the supplier's answer before staff are reminded.",
            "Staff are reminded later; slow suppliers are chased less.",
            "Staff chase suppliers sooner; more reminders.",
            "Change it when supplier answers are often late without anyone noticing (lower it).",
            "Today 2 days.",
        ),
        (
            "Một yêu cầu đặt dịch vụ với nhà cung cấp được chờ trả lời bao nhiêu ngày trước khi nhắc nhân viên.",
            "Nhân viên được nhắc muộn hơn; ít đôn đốc nhà cung cấp chậm hơn.",
            "Nhân viên đôn đốc nhà cung cấp sớm hơn; nhiều lời nhắc hơn.",
            "Đổi khi nhà cung cấp thường trả lời trễ mà không ai để ý (giảm).",
            "Hiện là 2 ngày.",
        ),
    ),
    "trip_check_reminder_days": _g(
        (
            "How many days before its due date an open trip check reminds its owner.",
            "Owners are reminded earlier.",
            "Owners are reminded closer to the due date; fewer early reminders.",
            "Change it when trip checks are often finished late (raise it).",
            "Today 3 days before the due date.",
        ),
        (
            "Một bước kiểm tra chuyến chưa xong nhắc người phụ trách trước hạn bao nhiêu ngày.",
            "Người phụ trách được nhắc sớm hơn.",
            "Nhắc sát hạn hơn; ít lời nhắc sớm hơn.",
            "Đổi khi các bước kiểm tra thường xong trễ (tăng).",
            "Hiện là 3 ngày trước hạn.",
        ),
    ),
    "supplier_bill_due_soon_days": _g(
        (
            "How many days before its due date an unpaid supplier bill is announced to staff.",
            "Finance sees bills earlier and can plan payments.",
            "Bills are announced closer to the due date; more risk of paying a supplier late.",
            "Change it when supplier bills are paid late (raise it).",
            "Today 3 days before the due date.",
        ),
        (
            "Hoá đơn nhà cung cấp chưa trả được báo cho nhân viên trước hạn bao nhiêu ngày.",
            "Kế toán thấy hoá đơn sớm hơn và lên kế hoạch thanh toán được.",
            "Hoá đơn được báo sát hạn hơn; dễ trả nhà cung cấp trễ hơn.",
            "Đổi khi hoá đơn nhà cung cấp bị trả trễ (tăng).",
            "Hiện là 3 ngày trước hạn.",
        ),
    ),
    "agency_reminder_steps_days": _g(
        (
            "The days after the due date on which an unpaid agency invoice is reminded by e-mail, one e-mail per step.",
            "More or earlier steps (the stricter choice): agencies are chased more often.",
            "Fewer or later steps (the looser choice): fewer e-mails, slower collection.",
            "Change it when overdue invoices are paid late although reminded, or agencies find the reminders too many.",
            "Today 1, 7 and 14 days after the due date.",
        ),
        (
            "Những ngày sau hạn mà hoá đơn đại lý chưa thanh toán được nhắc qua e-mail, mỗi bước một e-mail.",
            "Thêm bước hoặc bước sớm hơn (lựa chọn chặt hơn): đại lý bị nhắc thường xuyên hơn.",
            "Bớt bước hoặc bước muộn hơn (lựa chọn nới hơn): ít e-mail hơn, thu tiền chậm hơn.",
            "Đổi khi hoá đơn quá hạn vẫn trả trễ dù đã nhắc, hoặc đại lý thấy bị nhắc quá nhiều.",
            "Hiện là 1, 7 và 14 ngày sau hạn.",
        ),
    ),
    "rounding_increment_by_currency": _g(
        (
            "Customer-facing totals are rounded up to this amount in each selling currency.",
            "A bigger increment (the looser choice): rounder prices, a little more margin per booking.",
            "A smaller increment (the stricter choice): prices closer to the computed total.",
            "Change it when sales asks for rounder or more exact prices in a currency.",
            "Today 5 in every selling currency (AUD, EUR, GBP, SGD, USD): 1,231 becomes 1,235.",
        ),
        (
            "Tổng tiền hiển thị cho khách được làm tròn lên tới bội số này của mỗi đồng tiền bán.",
            "Bội số lớn hơn (lựa chọn nới hơn): giá tròn hơn, thêm chút biên lợi nhuận mỗi booking.",
            "Bội số nhỏ hơn (lựa chọn chặt hơn): giá sát với tổng đã tính hơn.",
            "Đổi khi bộ phận bán hàng muốn giá tròn hơn hoặc chính xác hơn cho một đồng tiền.",
            "Hiện là 5 cho mọi đồng tiền bán (AUD, EUR, GBP, SGD, USD): 1.231 thành 1.235.",
        ),
    ),
    "retention_job_succeeded_days": _g(
        (
            "Days a finished background job is kept before it is archived and deleted. Must not be longer than the "
            "failed job window.",
            "Finished jobs stay visible longer for checks; the job table grows.",
            "The job table stays small; finished jobs disappear sooner.",
            "Change it when you need to look back further at finished jobs.",
            "Today 30 days.",
        ),
        (
            "Số ngày một job nền đã xong được giữ trước khi lưu trữ và xoá. Không được dài hơn thời hạn giữ job lỗi.",
            "Job đã xong được xem lại lâu hơn; bảng job lớn dần.",
            "Bảng job gọn; job đã xong biến mất sớm hơn.",
            "Đổi khi cần xem lại job đã xong lâu hơn.",
            "Hiện là 30 ngày.",
        ),
    ),
    "retention_job_failed_days": _g(
        (
            "Days a failed or cancelled background job is kept before it is archived and deleted. Must not be shorter "
            "than the finished job window.",
            "Failures stay available longer for investigation.",
            "Failures leave sooner; older ones are only in the archive.",
            "Change it when investigations need older failures.",
            "Today 90 days.",
        ),
        (
            "Số ngày một job nền lỗi hoặc bị huỷ được giữ trước khi lưu trữ và xoá. Không được ngắn hơn thời hạn giữ "
            "job đã xong.",
            "Job lỗi được giữ lâu hơn để điều tra.",
            "Job lỗi bị gỡ sớm hơn; job cũ chỉ còn trong kho lưu trữ.",
            "Đổi khi việc điều tra cần job lỗi cũ hơn.",
            "Hiện là 90 ngày.",
        ),
    ),
    "retention_inbox_redact_days": _g(
        (
            "Days after receipt that personal data in received inquiry payloads is redacted (names, e-mails, phone "
            "numbers).",
            "Staff can read the original inquiry longer; personal data is kept longer.",
            "Personal data is removed sooner; old inquiries can no longer be read in full.",
            "Change it when the privacy policy names another window.",
            "Today 90 days.",
        ),
        (
            "Sau khi nhận bao nhiêu ngày thì dữ liệu cá nhân trong yêu cầu gửi đến bị che (tên, e-mail, số điện "
            "thoại).",
            "Nhân viên đọc được yêu cầu gốc lâu hơn; dữ liệu cá nhân bị giữ lâu hơn.",
            "Dữ liệu cá nhân bị xoá sớm hơn; yêu cầu cũ không còn đọc được đầy đủ.",
            "Đổi khi chính sách quyền riêng tư nêu thời hạn khác.",
            "Hiện là 90 ngày.",
        ),
    ),
    "emergency_contact_keep_days": _g(
        (
            "Days after the trip ends that the travellers' emergency contacts and the trip check notes are kept, then "
            "cleared.",
            "Contacts stay available longer after a trip (for a late claim); personal data is kept longer.",
            "Contacts are cleared sooner after the trip.",
            "Change it when the privacy policy or insurance terms name another window.",
            "Today 90 days after the trip ends.",
        ),
        (
            "Số ngày sau khi chuyến kết thúc mà liên hệ khẩn cấp của khách và ghi chú kiểm tra chuyến được giữ, sau "
            "đó bị xoá.",
            "Liên hệ được giữ lâu hơn sau chuyến (khi có khiếu nại muộn); dữ liệu cá nhân bị giữ lâu hơn.",
            "Liên hệ bị xoá sớm hơn sau chuyến.",
            "Đổi khi chính sách quyền riêng tư hoặc điều khoản bảo hiểm nêu thời hạn khác.",
            "Hiện là 90 ngày sau khi chuyến kết thúc.",
        ),
    ),
}


def guide(key: str) -> dict[str, dict[str, str]] | None:
    """The guide of ``key`` (None when it has none: the unit guard fails on that)."""
    return GUIDES.get(key)
