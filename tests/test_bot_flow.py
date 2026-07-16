import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.bot import ProductBot
from app.config import Settings
from app.draft_store import DraftStore
from app.models import ProductDraft
from app.order_flow import OrderFlow, OrderStore, SessionStore


def make_settings() -> Settings:
    # Empty Anthropic key → deterministic rule parser, never touches the network.
    return Settings(
        telegram_bot_token="tok",
        sell_base_url="http://127.0.0.1:8000",
        sell_internal_token="internal",
        anthropic_api_key="",
        anthropic_model="claude-haiku-4-5",
    )


class BotTextFlowTest(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows SQLite keeps the file handle until GC,
        # so the temp dir may not delete immediately — harmless for the test.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bot = ProductBot.__new__(ProductBot)
        self.bot.settings = make_settings()
        self.bot.telegram = Mock()
        self.bot.sell = Mock()          # preview will fail gracefully; draft is saved before preview
        self.bot.drafts = DraftStore(Path(self._tmp.name))
        self.bot.offset = None
        self.bot.orders = OrderFlow(
            self.bot.telegram,
            self.bot.sell,
            OrderStore(Path(self._tmp.name)),
            SessionStore(Path(self._tmp.name)),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, text: str, chat_id: int = 555):
        self.bot.handle_update(
            {"message": {"text": text, "chat": {"id": chat_id}, "from": {"id": 999}}}
        )

    def test_second_message_merges_into_existing_draft(self):
        self._send("Ten Ao Adidas, SKU ADI-1, gia ban le 100000")

        draft = self.bot.drafts.get(555)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.ten, "Ao Adidas")
        self.assertEqual(draft.maSKU, "ADI-1")
        self.assertEqual(draft.giaBanLe, 100000)

        # Follow-up only changes the price — name and SKU must be preserved (context merge).
        self._send("gia ban le 150000")

        draft = self.bot.drafts.get(555)
        self.assertEqual(draft.ten, "Ao Adidas")
        self.assertEqual(draft.maSKU, "ADI-1")
        self.assertEqual(draft.giaBanLe, 150000)

    def test_weight_unit_survives_price_only_followup(self):
        # Regression guard for the merge_from donVi bug, exercised through the real bot flow.
        self._send("Ten Ao, SKU ADI-2, khoi luong 2kg")
        draft = self.bot.drafts.get(555)
        self.assertEqual(draft.khoiLuong, 2.0)
        self.assertEqual(draft.donVi, "kg")

        self._send("gia ban le 150000")
        draft = self.bot.drafts.get(555)
        self.assertEqual(draft.khoiLuong, 2.0)
        self.assertEqual(draft.donVi, "kg")


class BotImageCaptionFlowTest(unittest.TestCase):
    """Photo + caption in one message: description captions must be understood,
    while the pre-existing variant-SKU-attach caption behavior stays unchanged."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bot = ProductBot.__new__(ProductBot)
        self.bot.settings = make_settings()
        self.bot.telegram = Mock()
        self.bot.telegram.download_file.return_value = None
        self.bot.sell = Mock()
        self.bot.drafts = DraftStore(Path(self._tmp.name))
        self.bot.offset = None
        self.bot.orders = OrderFlow(
            self.bot.telegram,
            self.bot.sell,
            OrderStore(Path(self._tmp.name)),
            SessionStore(Path(self._tmp.name)),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _photo_update(self, caption: str | None, chat_id: int = 1, file_id: str = "f1"):
        message = {
            "photo": [{"file_id": file_id, "file_size": 1000}],
            "chat": {"id": chat_id},
            "from": {"id": 9},
        }
        if caption is not None:
            message["caption"] = caption
        self.bot.handle_update({"message": message})

    def test_description_caption_is_parsed_merged_and_shows_preview(self):
        self.bot.sell.preview_product.return_value = {
            "so_phien_ban": 3,
            "san_pham": {"ten": "adioooo", "maSKU": "34343"},
        }
        caption = (
            "tạo sản phẩm với nhiều phiên bản có tên là adioooo, sku là 34343, "
            "size 34, 43, 54, có giá bán lẻ là 4545454"
        )
        self._photo_update(caption)

        draft = self.bot.drafts.get(1)
        self.assertEqual(draft.ten, "adioooo")
        self.assertEqual(draft.maSKU, "34343")
        self.assertEqual(len(draft.anhChung), 1)   # image still saved
        self.bot.sell.preview_product.assert_called_once()
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("Bản xem trước", text)
        self.assertIn("Xác nhận tạo", str(self.bot.telegram.send_message.call_args))

    def test_no_caption_still_behaves_as_plain_common_image(self):
        self._photo_update(None)

        draft = self.bot.drafts.get(1)
        self.assertEqual(draft.ten, "")
        self.assertEqual(len(draft.anhChung), 1)
        self.bot.sell.preview_product.assert_not_called()
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("Đã thêm ảnh chung", text)

    def test_caption_matching_existing_variant_sku_still_attaches_directly(self):
        draft = ProductDraft(ten="Ao", maSKU="ADI-1")
        draft.variants = [{"maSKU": "ADI-1-S", "attributes": ["S"]}]
        self.bot.drafts.save(1, draft)

        self._photo_update("SKU: ADI-1-S")

        self.bot.sell.preview_product.assert_not_called()   # unchanged direct-attach path
        saved = self.bot.drafts.get(1)
        self.assertIn("ADI-1-S", saved.anhPhienBan)
        self.assertEqual(saved.ten, "Ao")   # description path must NOT have run

    def test_unmatched_bare_sku_caption_still_errors_and_skips_download(self):
        draft = ProductDraft(ten="Ao", maSKU="ADI-1")
        draft.variants = [{"maSKU": "ADI-1-S", "attributes": ["S"]}]
        self.bot.drafts.save(1, draft)

        self._photo_update("SKU: ADI-1-ZZZ")   # typo, no product name in caption

        self.bot.telegram.download_file.assert_not_called()
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("Không tìm thấy SKU phiên bản", text)
        saved = self.bot.drafts.get(1)
        self.assertEqual(saved.maSKU, "ADI-1")   # draft untouched, no corruption

    def test_caption_with_substantial_data_but_no_name_asks_for_ten_not_sku_error(self):
        # Reported bug: a caption with SKU + prices + vi tri + thuoc tinh but
        # NO product name was misread as a bogus variant-SKU reference and
        # got "Khong tim thay SKU phien ban" instead of being understood.
        caption = (
            "tạo sản phẩm với mã sku là : 393939jdj, giá bán lẻ: 99, "
            "giá bán buôn:44, vị trí: thổ tang, loại adias, "
            "màu sắc : vàng, xanh đỏ, size 44, 46"
        )
        self._photo_update(caption)

        self.bot.sell.preview_product.assert_not_called()   # missing 'ten' -> ask, don't preview
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("Bản nháp còn thiếu: tên sản phẩm", text)
        self.assertNotIn("Không tìm thấy SKU phiên bản", text)

        draft = self.bot.drafts.get(1)
        self.assertEqual(draft.maSKU, "393939jdj")
        self.assertEqual(draft.giaBanLe, 99)
        self.assertEqual(draft.viTri, "thổ tang")
        self.assertEqual(len(draft.anhChung), 1)   # image still saved
        colors = next(a for a in draft.thuocTinhs if a["ten"] == "màu sắc")
        self.assertEqual(colors["giaTri"], ["vàng", "xanh đỏ"])

    def test_bare_sku_only_caption_still_treated_as_variant_reference_not_description(self):
        # A caption resolving to ONLY maSKU (nothing else) must still be
        # judged as a possible variant-SKU reference, not a description —
        # otherwise a real typo'd SKU silently creates an empty draft.
        self._photo_update("SKU: ABC123")   # no existing draft/variants at all

        self.bot.sell.preview_product.assert_not_called()
        text = self.bot.telegram.send_message.call_args[0][1]
        self.assertIn("Không tìm thấy SKU phiên bản", text)
        self.assertIsNone(self.bot.drafts.get(1))   # nothing was ever saved


class BotImageResetOnNewProductTest(unittest.TestCase):
    """Bug report: a leftover draft's old images kept accumulating with a
    new product's images ("gui 2 anh ma lay tan 5 anh"). Option B: a photo
    caption naming a NEW product must wipe the old draft (text + real files
    on disk) before starting the new one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bot = ProductBot.__new__(ProductBot)
        self.bot.settings = dataclasses.replace(
            make_settings(), bot_data_dir=Path(self._tmp.name)
        )
        self.bot.telegram = Mock()
        self.bot.sell = Mock()
        self.bot.drafts = DraftStore(Path(self._tmp.name))
        self.bot.offset = None
        self.bot.orders = OrderFlow(
            self.bot.telegram,
            self.bot.sell,
            OrderStore(Path(self._tmp.name)),
            SessionStore(Path(self._tmp.name)),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_old_draft_with_real_files(self, chat_id: int, count: int) -> ProductDraft:
        """Simulates leftover images from an earlier, unconfirmed attempt —
        real files on disk, referenced by a saved draft, exactly like a
        real bot session that never hit /taomoi, /huy, or a successful create."""
        draft = ProductDraft(ten="San pham cu", maSKU="OLD-SKU")
        media_dir = self.bot.settings.media_dir / str(chat_id) / draft.draft_id
        media_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            path = media_dir / f"old_{i}.jpg"
            path.write_bytes(b"fake-jpg")
            draft.anhChung.append(str(path))
        self.bot.drafts.save(chat_id, draft)
        return draft

    def _download_creates_file(self, file_id, destination, max_bytes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"fake-jpg")

    def _photo_update(self, caption: str, chat_id: int, file_id: str = "new1"):
        self.bot.handle_update(
            {
                "message": {
                    "photo": [{"file_id": file_id, "file_size": 1000}],
                    "caption": caption,
                    "chat": {"id": chat_id},
                    "from": {"id": 9},
                }
            }
        )

    def test_new_product_caption_resets_old_images_not_accumulates(self):
        old_draft = self._fake_old_draft_with_real_files(chat_id=1, count=2)
        old_paths = [Path(p) for p in old_draft.anhChung]
        for p in old_paths:
            self.assertTrue(p.is_file())   # sanity check before the fix runs

        self.bot.telegram.download_file.side_effect = self._download_creates_file
        self.bot.sell.preview_product.return_value = {
            "so_phien_ban": 1,
            "san_pham": {"ten": "San pham moi", "maSKU": "NEW-SKU"},
        }

        caption = "Ten San pham moi, SKU NEW-SKU, gia ban le 100000"
        self._photo_update(caption, chat_id=1)

        draft = self.bot.drafts.get(1)
        self.assertEqual(draft.ten, "San pham moi")
        self.assertEqual(draft.maSKU, "NEW-SKU")
        # Only the ONE new image — old ones must not accumulate.
        self.assertEqual(len(draft.anhChung), 1)
        self.assertNotEqual(draft.draft_id, old_draft.draft_id)
        # The old files must be physically deleted from disk, not just unlinked from the draft.
        for p in old_paths:
            self.assertFalse(p.exists(), f"stale file not cleaned up: {p}")

    def test_two_photos_with_description_on_first_yields_exactly_two_images(self):
        # Reproduces the reported scenario directly: send 2 photos, caption
        # (naming a new product) on the first one only -> exactly 2 images.
        self._fake_old_draft_with_real_files(chat_id=1, count=3)  # leftover from a prior attempt
        self.bot.telegram.download_file.side_effect = self._download_creates_file
        self.bot.sell.preview_product.return_value = {
            "so_phien_ban": 1,
            "san_pham": {"ten": "Ao moi", "maSKU": "AO-1"},
        }

        self._photo_update("Ten Ao moi, SKU AO-1, gia ban le 50000", chat_id=1, file_id="p1")
        # Second photo of the same album: no caption (Telegram convention).
        self.bot.handle_update(
            {
                "message": {
                    "photo": [{"file_id": "p2", "file_size": 1000}],
                    "chat": {"id": 1},
                    "from": {"id": 9},
                }
            }
        )

        draft = self.bot.drafts.get(1)
        self.assertEqual(len(draft.anhChung), 2)

    def test_failed_download_does_not_destroy_old_draft(self):
        old_draft = self._fake_old_draft_with_real_files(chat_id=1, count=2)
        old_paths = [Path(p) for p in old_draft.anhChung]

        self.bot.telegram.download_file.side_effect = RuntimeError("network down")
        caption = "Ten San pham moi, SKU NEW-SKU, gia ban le 100000"
        self._photo_update(caption, chat_id=1)

        # Old draft and its real files must be fully intact — download failed
        # BEFORE any cleanup, so nothing should have been touched.
        draft = self.bot.drafts.get(1)
        self.assertEqual(draft.ten, "San pham cu")
        self.assertEqual(draft.maSKU, "OLD-SKU")
        self.assertEqual(len(draft.anhChung), 2)
        for p in old_paths:
            self.assertTrue(p.is_file(), f"old file wrongly deleted on failed download: {p}")

    def test_reset_does_not_trigger_without_a_product_name_in_caption(self):
        # A caption with no name (e.g. just a price note) must NOT wipe the
        # old draft — only a genuine new-product description should.
        old_draft = self._fake_old_draft_with_real_files(chat_id=1, count=2)
        self.bot.telegram.download_file.side_effect = self._download_creates_file

        self._photo_update("gia ban le 999999", chat_id=1)

        draft = self.bot.drafts.get(1)
        self.assertEqual(draft.ten, "San pham cu")          # old draft preserved
        self.assertEqual(draft.draft_id, old_draft.draft_id)
        self.assertEqual(len(draft.anhChung), 3)             # 2 old + 1 new, accumulated as before


if __name__ == "__main__":
    unittest.main()
