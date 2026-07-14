import unittest
from unittest.mock import Mock

from app.bot import ProductBot
from app.models import ProductDraft
from app.nlp_parser import SYSTEM_PROMPT, extract_custom_prices, parse_with_rules


class DiacriticsTest(unittest.TestCase):
    """Viec 1: bot replies must use proper Vietnamese diacritics."""

    def setUp(self):
        self.bot = ProductBot.__new__(ProductBot)
        self.bot.telegram = Mock()
        self.bot.sell = Mock()
        self.bot.drafts = Mock()

    def test_start_command_has_diacritics(self):
        self.bot.handle_update(
            {"message": {"text": "/start", "chat": {"id": 1}, "from": {"id": 2}}}
        )
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("Gửi mô tả sản phẩm", text)
        self.assertIn("Thuộc tính", text)
        self.assertNotIn("Gui mo ta", text)

    def test_missing_fields_message_translates_field_keys(self):
        self.bot.drafts.get.return_value = None
        draft = ProductDraft()  # missing both ten and maSKU
        self.bot._send_missing(1, draft)
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("tên sản phẩm", text)
        self.assertIn("mã SKU", text)
        self.assertNotIn(": ten,", text)   # raw field key must not leak into the message


class SummaryDisplayTest(unittest.TestCase):
    """Viec 2: giaCongTacVien hidden from summary() but still sent to Laravel."""

    def test_summary_has_diacritics_and_hides_old_ctv_price(self):
        draft = ProductDraft(
            ten="Ao Test", maSKU="SKU-1", giaBanLe=1000, giaCongTacVien=999
        )
        text = draft.summary()
        self.assertIn("Tên: Ao Test", text)
        self.assertIn("Giá bán lẻ:", text)
        self.assertNotIn("cộng tác viên", text.lower())
        self.assertNotIn("cong tac vien", text.lower())

    def test_payload_still_carries_gia_cong_tac_vien(self):
        draft = ProductDraft(ten="Ao Test", maSKU="SKU-1", giaCongTacVien=999)
        payload = draft.to_product_payload()
        self.assertEqual(payload["giaCongTacVien"], 999)

    def test_summary_shows_vi_tri_khoi_luong_don_vi_tinh_when_present(self):
        draft = ProductDraft(
            ten="Ao Test",
            maSKU="SKU-1",
            viTri="Ke A1",
            khoiLuong=2,
            donVi="kg",
            donViTinh="cai",
        )
        text = draft.summary()
        self.assertIn("Vị trí: Ke A1", text)
        self.assertIn("Khối lượng: 2 kg", text)
        self.assertIn("Đơn vị tính: cai", text)

    def test_summary_shows_dash_for_empty_vi_tri_khoi_luong_don_vi_tinh(self):
        draft = ProductDraft(ten="Ao Test", maSKU="SKU-1")
        text = draft.summary()
        self.assertIn("Vị trí: -", text)
        self.assertIn("Khối lượng: -", text)
        self.assertIn("Đơn vị tính: -", text)


class PreviewMessageTest(unittest.TestCase):
    """Bo chu 'Laravel' (chi tiet ky thuat noi bo) khoi tin nhan xem truoc."""

    def setUp(self):
        self.bot = ProductBot.__new__(ProductBot)
        self.bot.telegram = Mock()
        self.bot.sell = Mock()
        self.bot.drafts = Mock()

    def test_preview_message_does_not_mention_laravel(self):
        self.bot.sell.preview_product.return_value = {
            "so_phien_ban": 1,
            "san_pham": {"ten": "Ao Test", "maSKU": "SKU-1"},
        }
        draft = ProductDraft(ten="Ao Test", maSKU="SKU-1")
        self.bot._preview_and_show(1, 9, draft)

        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertNotIn("Laravel", text)
        self.assertIn("Bản xem trước", text)


class SkuAutoGenPromptTest(unittest.TestCase):
    """Viec 3: system prompt must instruct AI to leave variant SKU blank by default."""

    def test_prompt_documents_blank_sku_means_autogenerate(self):
        self.assertIn("auto-generates a variant's SKU", SYSTEM_PROMPT)
        self.assertIn("NEVER invent, guess, or copy the base SKU", SYSTEM_PROMPT)
        self.assertIn("maSKU: ''", SYSTEM_PROMPT)


class BareSkuVariantParsingTest(unittest.TestCase):
    """Viec 4: rule-based parser recognizes 'phien ban co sku la: A, B, C'."""

    def test_recognizes_bare_sku_list_from_screenshot_message(self):
        text = (
            "tao san pham voi gia ban le la 343434, gia ban buon la 3333, "
            "gia cong tac vien: 36666, gia order le: 999, voi cac phien ban "
            "co sku la : uuuu3, uuu2, uuu8, va co anh chung la"
        )
        draft = parse_with_rules(text)

        self.assertEqual(draft.giaBanLe, 343434)
        self.assertEqual(draft.giaBanBuon, 3333)
        self.assertEqual(draft.giaOrder, 999)
        self.assertEqual(len(draft.variants), 3)
        skus = [v["maSKU"] for v in draft.variants]
        self.assertEqual(skus, ["uuuu3", "uuu2", "uuu8"])
        self.assertEqual(draft.thuocTinhs, [{"ten": "Phiên bản", "giaTri": skus}])
        # The trailing unrelated clause must NOT leak into a SKU token.
        for sku in skus:
            self.assertNotIn("va", sku)
            self.assertNotIn("anh", sku)

    def test_preserves_original_casing_of_sku_tokens(self):
        text = "phien ban co sku la: ADI-S, adi-m"
        draft = parse_with_rules(text)
        skus = [v["maSKU"] for v in draft.variants]
        self.assertEqual(skus, ["ADI-S", "adi-m"])

    def test_single_bare_token_is_not_treated_as_variant_list(self):
        # Too ambiguous: could just be a normal SKU mention, not a list.
        text = "phien ban co sku la: ONLYONE"
        draft = parse_with_rules(text)
        self.assertEqual(draft.variants, [])

    def test_does_not_override_structured_phien_ban_syntax(self):
        # When the explicit "Phien ban: attr=value" syntax is present, the
        # bare-list fallback must not run (structured syntax takes priority).
        text = (
            "Ten Ao, SKU ADI-1\n"
            "Thuoc tinh Size: S | M\n"
            "Phien ban: Size=S | SKU: ADI-1-S\n"
            "Phien ban: Size=M | SKU: ADI-1-M"
        )
        draft = parse_with_rules(text)
        self.assertEqual(len(draft.variants), 2)
        self.assertEqual(draft.variants[0]["maSKU"], "ADI-1-S")
        self.assertEqual(draft.variants[1]["maSKU"], "ADI-1-M")

    def test_no_match_when_phien_ban_keyword_absent(self):
        text = "gia ban le 100000, sku la: A, B, C"
        draft = parse_with_rules(text)
        self.assertEqual(draft.variants, [])


class InlineAttributeListTest(unittest.TestCase):
    """Loi 1 (offset) + Loi 2 (comma-separated attribute values)."""

    def test_screenshot_message_size_list_becomes_three_values(self):
        text = (
            "tạo sản phẩm với nhiều phiên bản có tên là adioooo, sku là 34343, "
            "size 34, 43, 54, có giá bán lẻ là 4545454"
        )
        draft = parse_with_rules(text)

        self.assertEqual(draft.ten, "adioooo")
        self.assertEqual(draft.maSKU, "34343")
        self.assertEqual(draft.giaBanLe, 4545454)
        # Loi 1: keyword not corrupted to "siz", value not corrupted to "3".
        self.assertEqual(len(draft.thuocTinhs), 1)
        self.assertEqual(draft.thuocTinhs[0]["ten"], "size")
        # Loi 2: all three sizes captured, not just the first.
        self.assertEqual(draft.thuocTinhs[0]["giaTri"], ["34", "43", "54"])

    def test_size_letter_list(self):
        draft = parse_with_rules("Ten Ao, SKU ADI-1, size S, M, L, gia ban le 100000")
        self.assertEqual(draft.thuocTinhs, [{"ten": "size", "giaTri": ["S", "M", "L"]}])

    def test_color_list_preserves_vietnamese_diacritics(self):
        draft = parse_with_rules("Ten Vay, SKU V-1, mau Đen, Trắng, Xanh, gia nhap 50000")
        self.assertEqual(draft.thuocTinhs, [{"ten": "mau", "giaTri": ["Đen", "Trắng", "Xanh"]}])

    def test_mau_sac_keyword_not_shadowed_by_shorter_mau(self):
        # Regression: alternation tried "mau" before "mau sac" and always won,
        # leaving "sac" stuck as part of the first value.
        text = "Ten Ao, SKU A-1, mau sac : vang, xanh do, gia ban le 100000"
        draft = parse_with_rules(text)
        self.assertEqual(
            draft.thuocTinhs, [{"ten": "mau sac", "giaTri": ["vang", "xanh do"]}]
        )

    def test_mau_sac_screenshot_message_with_diacritics(self):
        text = (
            "tạo sản phẩm với mã sku là : 393939jdj, giá bán lẻ: 99, "
            "màu sắc : vàng, xanh đỏ, size 44, 46"
        )
        draft = parse_with_rules(text)
        self.assertEqual(draft.maSKU, "393939jdj")
        self.assertEqual(draft.giaBanLe, 99)
        colors = next(a for a in draft.thuocTinhs if a["ten"] == "màu sắc")
        self.assertEqual(colors["giaTri"], ["vàng", "xanh đỏ"])

    def test_colon_glued_label_ends_previous_attribute_value_list(self):
        # Bug report: "size:" (no space before colon) wasn't recognized as a
        # new field boundary, so it (and "loai: nnn" after it) got swallowed
        # as trailing values of "mau sac", producing 1 attribute with 7 junk
        # values instead of 2 clean attributes (+ loaiSanPham handled separately).
        text = (
            "Ten mu hh, SKU 90033, mau sac: xanh, đỏ, tím, "
            "size: 21, 22, 23, loại: nnn"
        )
        draft = parse_with_rules(text)

        self.assertEqual(len(draft.thuocTinhs), 2)
        colors = next(a for a in draft.thuocTinhs if a["ten"] == "mau sac")
        sizes = next(a for a in draft.thuocTinhs if a["ten"] == "size")
        self.assertEqual(colors["giaTri"], ["xanh", "đỏ", "tím"])
        self.assertEqual(sizes["giaTri"], ["21", "22", "23"])
        # "loai:" is the product-level field, not a 3rd attribute.
        self.assertEqual(draft.loaiSanPham, "nnn")
        self.assertEqual(draft.ten, "mu hh")


class PriceMultiplierCollisionTest(unittest.TestCase):
    """Newly discovered while testing Loi B: a price's number-matching regex
    let a trailing ", " (field separator) bridge into the FIRST LETTERS of the
    next clause, then misread a leading "k" or "tr" there as the x1,000 /
    x1,000,000 shorthand suffix — silently inflating the price by 1000x or
    1,000,000x with no error shown."""

    def test_k_from_next_word_no_longer_multiplies_price(self):
        draft = parse_with_rules("gia nhap 990, khoi luong 80kg")
        self.assertEqual(draft.giaNhap, 990)

    def test_k_from_kich_thuoc_no_longer_multiplies_price(self):
        draft = parse_with_rules("gia ban le 500, kich thuoc 10cm")
        self.assertEqual(draft.giaBanLe, 500)

    def test_k_from_ke_no_longer_multiplies_price(self):
        draft = parse_with_rules("gia ban le 500, ke A1")
        self.assertEqual(draft.giaBanLe, 500)

    def test_tr_from_trang_no_longer_multiplies_price_by_a_million(self):
        draft = parse_with_rules("gia nhap 200, trang phuc dep")
        self.assertEqual(draft.giaNhap, 200)

    def test_glued_k_shorthand_still_multiplies_by_1000(self):
        draft = parse_with_rules("gia ban le 100k")
        self.assertEqual(draft.giaBanLe, 100000)

    def test_glued_tr_shorthand_still_multiplies_by_1_000_000(self):
        draft = parse_with_rules("gia ban le 5tr")
        self.assertEqual(draft.giaBanLe, 5000000)

    def test_dot_grouped_thousands_still_parses_correctly(self):
        draft = parse_with_rules("gia nhap 1.000.000")
        self.assertEqual(draft.giaNhap, 1000000)

    def test_stock_number_unaffected_by_a_following_k_word(self):
        draft = parse_with_rules("ton 20, khoi luong 80kg")
        self.assertEqual(draft.tonKhoBanDau, 20)

    def test_custom_price_policy_unaffected_by_a_following_k_word(self):
        prices = extract_custom_prices("gia chinh sach VIP: 990, khoi luong 80kg")
        self.assertEqual(prices, [{"ten": "VIP", "gia": 990}])


class WeightLabelNotMistakenForNameTest(unittest.TestCase):
    """Bug report: 'khoi luong san pham la 99' (no unit attached) was not
    recognized as a weight label, so the whole sentence fell through and got
    swallowed as the product NAME, overwriting whatever name was already set."""

    def test_weight_label_without_unit_is_parsed(self):
        draft = parse_with_rules("khối lượng sản phẩm là 99")
        self.assertEqual(draft.khoiLuong, 99.0)
        self.assertEqual(draft.donVi, "g")   # default unit, none was given
        self.assertEqual(draft.ten, "")      # must NOT become the name

    def test_weight_label_with_colon_is_parsed(self):
        draft = parse_with_rules("khối lượng sản phẩm là: 99")
        self.assertEqual(draft.khoiLuong, 99.0)
        self.assertEqual(draft.ten, "")

    def test_weight_label_does_not_overwrite_existing_name_on_merge(self):
        draft = ProductDraft(ten="giayu", maSKU="ABC")
        draft.merge_from(parse_with_rules("khối lượng sản phẩm là 99"))

        self.assertEqual(draft.ten, "giayu")   # must survive
        self.assertEqual(draft.khoiLuong, 99.0)

    def test_direct_unit_form_still_works_unaffected(self):
        # Regression guard: the original "99g"/"2kg" attached-unit path.
        draft = parse_with_rules("khoi luong 2kg")
        self.assertEqual(draft.khoiLuong, 2.0)
        self.assertEqual(draft.donVi, "kg")

    def test_other_field_labels_also_excluded_from_becoming_the_name(self):
        for text, field, expected in [
            ("vi tri la A1", "viTri", "A1"),
            ("don vi tinh la cai", "donViTinh", "cai"),
            ("nhan hieu la Adidas", "nhanHieu", "Adidas"),
        ]:
            draft = parse_with_rules(text)
            self.assertEqual(draft.ten, "", msg=f"{text!r} wrongly became the name")
            self.assertEqual(getattr(draft, field), expected)

    def test_value_collection_stops_before_next_field(self):
        # The trailing price clause must NOT be swallowed as an attribute value.
        draft = parse_with_rules("Ten Ao, SKU A-1, size 34, 43, gia ban le 999")
        self.assertEqual(draft.thuocTinhs, [{"ten": "size", "giaTri": ["34", "43"]}])
        self.assertEqual(draft.giaBanLe, 999)
        for value in draft.thuocTinhs[0]["giaTri"]:
            self.assertNotIn("gia", value)

    def test_space_separated_single_segment_still_works(self):
        # Regression guard: existing whitespace-split path must keep working.
        draft = parse_with_rules("Ten Ao, SKU A-1, size 34 43 54")
        self.assertEqual(draft.thuocTinhs, [{"ten": "size", "giaTri": ["34", "43", "54"]}])

    def test_explicit_thuoc_tinh_name_not_corrupted_by_leading_space(self):
        # Loi 1 also affected the "Thuoc tinh Name: values" path with a leading space.
        draft = parse_with_rules("  Thuoc tinh Size: S | M | L")
        self.assertEqual(draft.thuocTinhs, [{"ten": "Size", "giaTri": ["S", "M", "L"]}])


if __name__ == "__main__":
    unittest.main()
