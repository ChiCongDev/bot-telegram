import unittest

from app.models import ProductDraft
from app.nlp_parser import parse_with_rules


class MergeFromWeightUnitTest(unittest.TestCase):
    def test_followup_message_without_weight_keeps_previous_unit(self):
        draft = parse_with_rules(
            "Ten Ao Adidas, SKU ADI-001, khoi luong 2kg, gia ban le 100000"
        )
        self.assertEqual(draft.khoiLuong, 2.0)
        self.assertEqual(draft.donVi, "kg")

        followup = parse_with_rules("gia ban le 150000")
        draft.merge_from(followup)

        self.assertEqual(draft.khoiLuong, 2.0)
        self.assertEqual(draft.donVi, "kg")
        self.assertEqual(draft.giaBanLe, 150000)

    def test_new_weight_message_updates_value_and_unit_together(self):
        draft = parse_with_rules("Ten Ao Adidas, SKU ADI-001, khoi luong 2kg")
        followup = parse_with_rules("khoi luong 500g")
        draft.merge_from(followup)

        self.assertEqual(draft.khoiLuong, 500.0)
        self.assertEqual(draft.donVi, "g")

    def test_merge_into_fresh_draft_keeps_default_unit(self):
        draft = ProductDraft()
        patch = parse_with_rules("Ten Ao Adidas, SKU ADI-001, gia ban le 100000")
        draft.merge_from(patch)

        self.assertEqual(draft.khoiLuong, 0.0)
        self.assertEqual(draft.donVi, "g")


class MergeFromStockCascadeTest(unittest.TestCase):
    """Bug report: variants baked by an earlier /xem froze tonKho=0, and a
    later 'ton 5' only updated the product-level field, not the variants —
    so Laravel created every variant with 0 stock regardless."""

    def test_stock_update_cascades_to_variants_still_on_old_default(self):
        draft = ProductDraft(ten="giay", maSKU="ABC")
        draft.variants = [
            {"maSKU": "ABC-44", "attributes": ["44"], "tonKho": 0},
            {"maSKU": "ABC-46", "attributes": ["46"], "tonKho": 0},
        ]
        draft.merge_from(parse_with_rules("ton 5"))

        self.assertEqual(draft.tonKhoBanDau, 5)
        self.assertEqual([v["tonKho"] for v in draft.variants], [5, 5])

    def test_variant_with_deliberately_different_stock_is_not_overwritten(self):
        draft = ProductDraft(ten="giay", maSKU="ABC")
        draft.variants = [
            {"maSKU": "ABC-44", "attributes": ["44"], "tonKho": 0},
            {"maSKU": "ABC-46", "attributes": ["46"], "tonKho": 10},  # user-set
        ]
        draft.merge_from(parse_with_rules("ton 5"))

        self.assertEqual([v["tonKho"] for v in draft.variants], [5, 10])

    def test_second_stock_update_cascades_from_new_baseline(self):
        draft = ProductDraft(ten="giay", maSKU="ABC")
        draft.variants = [{"maSKU": "ABC-44", "attributes": ["44"], "tonKho": 0}]

        draft.merge_from(parse_with_rules("ton 5"))
        self.assertEqual(draft.variants[0]["tonKho"], 5)

        draft.merge_from(parse_with_rules("ton 8"))
        self.assertEqual(draft.tonKhoBanDau, 8)
        self.assertEqual(draft.variants[0]["tonKho"], 8)

    def test_no_stock_in_message_leaves_variants_untouched(self):
        draft = ProductDraft(ten="giay", maSKU="ABC")
        draft.variants = [{"maSKU": "ABC-44", "attributes": ["44"], "tonKho": 3}]
        draft.merge_from(parse_with_rules("gia ban le 150000"))

        self.assertEqual(draft.variants[0]["tonKho"], 3)


class MergeFromPriceWeightLocationCascadeTest(unittest.TestCase):
    """Same class of bug as stock: variants baked by an earlier /xem froze
    price/khoiLuong/viTri at 0/empty, and a later message only updated the
    product-level field — Laravel then created every variant with those
    stale (0/empty) values regardless of what was just entered."""

    def _baked_variants(self):
        return [
            {
                "maSKU": "90033-xanh", "attributes": ["xanh"],
                "giaBanLe": 0, "giaBanBuon": 0, "giaNhap": 0,
                "khoiLuong": 0, "donVi": "g", "viTri": "", "tonKho": 0,
            },
            {
                "maSKU": "90033-do", "attributes": ["do"],
                "giaBanLe": 0, "giaBanBuon": 0, "giaNhap": 0,
                "khoiLuong": 0, "donVi": "g", "viTri": "", "tonKho": 0,
            },
        ]

    def test_prices_cascade_to_variants_still_on_old_default(self):
        draft = ProductDraft(ten="mu hh", maSKU="90033")
        draft.variants = self._baked_variants()
        draft.merge_from(parse_with_rules("gia ban le 10, gia ban buon 11, gia nhap 990"))

        self.assertEqual(draft.giaBanLe, 10)
        self.assertEqual([v["giaBanLe"] for v in draft.variants], [10, 10])
        self.assertEqual([v["giaBanBuon"] for v in draft.variants], [11, 11])
        self.assertEqual([v["giaNhap"] for v in draft.variants], [990, 990])

    def test_weight_and_unit_cascade_together_to_variants(self):
        draft = ProductDraft(ten="mu hh", maSKU="90033")
        draft.variants = self._baked_variants()
        draft.merge_from(parse_with_rules("khoi luong 80kg"))

        self.assertEqual(draft.khoiLuong, 80)
        self.assertEqual(draft.donVi, "kg")
        for v in draft.variants:
            self.assertEqual(v["khoiLuong"], 80)
            self.assertEqual(v["donVi"], "kg")   # unit must travel WITH the number

    def test_location_cascades_to_variants(self):
        draft = ProductDraft(ten="mu hh", maSKU="90033")
        draft.variants = self._baked_variants()
        draft.merge_from(parse_with_rules("vi tri vinh tuong"))

        self.assertEqual(draft.viTri, "vinh tuong")
        self.assertEqual([v["viTri"] for v in draft.variants], ["vinh tuong"] * 2)

    def test_variant_with_deliberately_different_price_is_not_overwritten(self):
        draft = ProductDraft(ten="mu hh", maSKU="90033")
        draft.variants = self._baked_variants()
        draft.variants[1]["giaBanLe"] = 25   # user-set override before this message
        draft.merge_from(parse_with_rules("gia ban le 10"))

        self.assertEqual([v["giaBanLe"] for v in draft.variants], [10, 25])

    def test_full_reported_scenario_all_fields_cascade_together(self):
        draft = ProductDraft(ten="mu hh", maSKU="90033")
        draft.variants = self._baked_variants()
        # Original field order from the report — safe now that the
        # extract_money "990, khoi..." multiplier-collision bug is fixed too.
        draft.merge_from(parse_with_rules(
            "gia ban le 10, gia ban buon 11, gia nhap 990, "
            "khoi luong 80kg, vi tri vinh tuong, ton 20"
        ))

        for v in draft.variants:
            self.assertEqual(v["giaBanLe"], 10)
            self.assertEqual(v["giaBanBuon"], 11)
            self.assertEqual(v["giaNhap"], 990)
            self.assertEqual(v["khoiLuong"], 80)
            self.assertEqual(v["donVi"], "kg")
            self.assertEqual(v["viTri"], "vinh tuong")
            self.assertEqual(v["tonKho"], 20)


if __name__ == "__main__":
    unittest.main()
