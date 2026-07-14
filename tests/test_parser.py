import unittest

from app.models import ProductDraft
from app.nlp_parser import parse_with_rules


class ParserTest(unittest.TestCase):
    def test_parse_basic_vietnamese_message(self):
        draft = parse_with_rules(
            "Ten Ao Adidas 3 Stripes, SKU JW4643-XS, barcode BC001, "
            "nhan hieu Adidas, loai Ao, gia ban 11111, gia nhap 7000, ton 5"
        )

        self.assertEqual(draft.ten, "Ao Adidas 3 Stripes")
        self.assertEqual(draft.maSKU, "JW4643-XS")
        self.assertEqual(draft.maVach, "BC001")
        self.assertEqual(draft.nhanHieu, "Adidas")
        self.assertEqual(draft.loaiSanPham, "Ao")
        self.assertEqual(draft.giaBanLe, 11111)
        self.assertEqual(draft.giaNhap, 7000)
        self.assertEqual(draft.tonKhoBanDau, 5)
        self.assertTrue(draft.suDungKhoHang)

    def test_required_fields(self):
        draft = ProductDraft(ten="Ao test")

        self.assertEqual(draft.missing_required(), ["maSKU"])

    def test_sell_payload_contains_idempotency_key(self):
        draft = ProductDraft(ten="Ao test", maSKU="SKU-001")
        payload = draft.to_sell_payload(telegram_user_id=123)

        self.assertEqual(payload["telegram_user_id"], "123")
        self.assertEqual(payload["san_pham"]["maSKU"], "SKU-001")
        self.assertTrue(payload["idempotency_key"])


if __name__ == "__main__":
    unittest.main()
