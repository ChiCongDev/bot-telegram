from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .draft_store import DraftStore
from .models import ProductDraft
from .nlp_parser import parse_product_message, remove_accents
from .sell_client import SellApiError, SellClient
from .telegram_gateway import TelegramGateway


class ProductBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.telegram = TelegramGateway(settings.telegram_bot_token)
        self.sell = SellClient(settings)
        self.drafts = DraftStore(settings.bot_data_dir)
        self.offset: int | None = None

    def run_forever(self) -> None:
        while True:
            try:
                for update in self.telegram.get_updates(self.offset):
                    self.offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except Exception as exc:
                print(f"bot loop error: {exc}")
                time.sleep(3)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id")
        telegram_user_id = user.get("id")
        if not chat_id or not telegram_user_id:
            return

        text = (message.get("text") or "").strip()
        command = self._command(text)
        if command:
            self._handle_command(command, chat_id, telegram_user_id)
            return

        if message.get("photo") or message.get("document"):
            self._handle_image(message, chat_id, telegram_user_id)
            return

        if not text:
            return

        existing = self.drafts.get(chat_id)
        patch = parse_product_message(text, self.settings, existing)
        if existing:
            existing.merge_from(patch)
            draft = existing
        else:
            draft = patch
        self.drafts.save(chat_id, draft)

        if draft.missing_required():
            self._send_missing(chat_id, draft)
            return
        self._preview_and_show(chat_id, telegram_user_id, draft)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        data = callback.get("data")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        user = callback.get("from") or {}
        chat_id = chat.get("id")
        telegram_user_id = user.get("id")

        if callback_id:
            self.telegram.answer_callback_query(callback_id)
        if not chat_id or not telegram_user_id:
            return

        if data == "cancel_draft":
            self._cancel_draft(chat_id)
            self.telegram.send_message(chat_id, "Đã hủy bản nháp.")
            return
        if data != "confirm_product":
            return

        draft = self.drafts.get(chat_id)
        if not draft:
            self.telegram.send_message(chat_id, "Không tìm thấy bản nháp để tạo.")
            return
        if draft.missing_required():
            self._send_missing(chat_id, draft)
            return

        try:
            preview = self.sell.preview_product(telegram_user_id, draft)
            canonical = preview.get("san_pham")
            if not isinstance(canonical, dict):
                raise SellApiError("Bản xem trước không có dữ liệu sản phẩm")
            draft.apply_preview(canonical)
            self.drafts.save(chat_id, draft)
            self._ensure_variant_images_match(draft)
            result = self.sell.create_product(telegram_user_id, draft)
        except SellApiError as exc:
            self.telegram.send_message(chat_id, "Tạo sản phẩm thất bại: " + str(exc))
            return

        self._cleanup_media(draft)
        self.drafts.delete(chat_id)
        created = result.get("ket_qua") or {}
        products = created.get("san_phams") or []
        rows = [
            f"- {item.get('ma_sku')}: {item.get('ten')}"
            for item in products[:10]
            if isinstance(item, dict)
        ]
        suffix = ""
        if len(products) > 10:
            suffix = f"\n- ... và {len(products) - 10} sản phẩm khác"
        self.telegram.send_message(
            chat_id,
            "Đã tạo sản phẩm thành công.\n"
            + f"Mã chung: {created.get('ma_chung', '-')}\n"
            + f"Số phiên bản: {created.get('so_phien_ban', len(products))}\n"
            + "\n".join(rows)
            + suffix,
        )

    def _handle_command(
        self,
        command: str,
        chat_id: int | str,
        telegram_user_id: int | str,
    ) -> None:
        if command == "/start":
            self.telegram.send_message(
                chat_id,
                "Gửi mô tả sản phẩm để tạo bản nháp, sau đó dùng /xem.\n"
                "Ví dụ: Tên Áo Adidas, SKU ADI-001, giá bán lẻ 120000, "
                "giá order 110000, giá nhập 70000, tồn 5.\n"
                "Nhiều phiên bản:\n"
                "Thuộc tính Size: S | M | L\n"
                "Thuộc tính Màu: Đen | Trắng\n"
                "Gửi ảnh không caption để làm ảnh chung; caption 'SKU: ADI-001-S' "
                "để gắn ảnh riêng sau khi /xem.\n"
                "Lệnh: /id, /xem, /taomoi, /huy.",
            )
            return
        if command == "/id":
            self.telegram.send_message(
                chat_id,
                f"Telegram User ID của bạn: {telegram_user_id}",
            )
            return
        if command == "/taomoi":
            self._cancel_draft(chat_id)
            draft = ProductDraft()
            self.drafts.save(chat_id, draft)
            self.telegram.send_message(chat_id, "Đã mở bản nháp sản phẩm mới.")
            return
        if command == "/huy":
            self._cancel_draft(chat_id)
            self.telegram.send_message(chat_id, "Đã hủy bản nháp hiện tại.")
            return
        if command in {"/draft", "/xem"}:
            draft = self.drafts.get(chat_id)
            if not draft:
                self.telegram.send_message(chat_id, "Chưa có bản nháp.")
                return
            if draft.missing_required():
                self._send_missing(chat_id, draft)
                return
            self._preview_and_show(chat_id, telegram_user_id, draft)
            return

        self.telegram.send_message(
            chat_id,
            "Lệnh không hợp lệ. Dùng /start để xem hướng dẫn.",
        )

    def _handle_image(
        self,
        message: dict[str, Any],
        chat_id: int | str,
        telegram_user_id: int | str,
    ) -> None:
        try:
            file_id, file_size, extension = self._image_file_info(message)
        except ValueError as exc:
            self.telegram.send_message(chat_id, str(exc))
            return

        if file_size and file_size > self.settings.telegram_max_image_bytes:
            self.telegram.send_message(chat_id, "Ảnh vượt quá dung lượng cho phép.")
            return

        old_draft = self.drafts.get(chat_id)
        caption = (message.get("caption") or "").strip()
        variant_sku = self._variant_sku_from_caption(caption)
        canonical_sku = (
            self._canonical_variant_sku(old_draft, variant_sku)
            if variant_sku and old_draft
            else ""
        )

        # A caption that isn't a match for an existing variant SKU may instead
        # be a full product description sent alongside the photo (e.g. an
        # album with the description as the caption of one photo). Judge the
        # caption on its OWN (no old-draft context) so a real product NAME —
        # or, when the user hasn't typed a name yet, a maSKU plus some OTHER
        # real field (price/vị trí/loại/thuộc tính/...) — is a clean signal
        # "this photo describes a product", not just a bare SKU reference. A
        # caption that resolves to ONLY maSKU (e.g. a typo'd "SKU: X" for an
        # unmatched variant) still falls through to the "SKU not found" error
        # below instead of being misread as a description.
        description_patch: ProductDraft | None = None
        if not canonical_sku and caption:
            candidate = parse_product_message(caption, self.settings)
            if self._looks_like_product_description(candidate):
                description_patch = candidate

        if variant_sku and not canonical_sku and description_patch is None:
            available = [
                str(variant.get("maSKU"))
                for variant in (old_draft.variants if old_draft else [])[:10]
                if variant.get("maSKU")
            ]
            hint = "\nSKU hiện có: " + ", ".join(available) if available else ""
            self.telegram.send_message(
                chat_id,
                "Không tìm thấy SKU phiên bản. Hãy gửi thông tin sản phẩm và /xem trước."
                + hint,
            )
            return

        draft = description_patch if description_patch is not None else (old_draft or ProductDraft())

        if (
            description_patch is None
            and not canonical_sku
            and len(draft.anhChung) >= self.settings.telegram_max_common_images
        ):
            self.telegram.send_message(chat_id, "Đã đạt giới hạn ảnh chung.")
            return

        media_dir = (
            self.settings.media_dir
            / self._safe_component(chat_id)
            / draft.draft_id
        )
        destination = media_dir / f"{uuid.uuid4().hex}{extension}"
        try:
            self.telegram.download_file(
                file_id,
                destination,
                self.settings.telegram_max_image_bytes,
            )
        except Exception as exc:
            self.telegram.send_message(chat_id, "Không tải được ảnh: " + str(exc))
            return

        if description_patch is not None and old_draft is not None:
            # New named product via caption -> start clean: wipe the OLD
            # draft's text fields AND media so counts (ảnh chung, thuộc
            # tính...) never mix with a leftover, not-yet-confirmed draft
            # from an earlier attempt. Done only AFTER the new image
            # downloaded successfully, so a failed download never leaves the
            # saved draft pointing at files we already deleted.
            self._cleanup_media(old_draft)

        if canonical_sku:
            old_path = draft.anhPhienBan.get(canonical_sku)
            draft.anhPhienBan[canonical_sku] = str(destination)
            if old_path:
                self._delete_media_path(Path(old_path))
            label = f"Đã gắn ảnh riêng cho SKU {canonical_sku}."
        else:
            draft.anhChung.append(str(destination))
            label = f"Đã thêm ảnh chung ({len(draft.anhChung)})."

        # description_patch already equals `draft` — nothing to merge into it.
        if description_patch is None:
            draft.revision += 1
        draft.clean()
        self.drafts.save(chat_id, draft)

        if description_patch is not None:
            if draft.missing_required():
                self._send_missing(chat_id, draft)
            else:
                self._preview_and_show(chat_id, telegram_user_id, draft)
            return

        self.telegram.send_message(chat_id, label + " Dùng /xem để kiểm tra bản nháp.")

    def _preview_and_show(
        self,
        chat_id: int | str,
        telegram_user_id: int | str,
        draft: ProductDraft,
    ) -> None:
        try:
            preview = self.sell.preview_product(telegram_user_id, draft)
            canonical = preview.get("san_pham")
            if not isinstance(canonical, dict):
                raise SellApiError("Ban xem truoc khong co du lieu san pham")
            draft.apply_preview(canonical)
            self._ensure_variant_images_match(draft)
            self.drafts.save(chat_id, draft)
        except SellApiError as exc:
            self.telegram.send_message(
                chat_id,
                "Chưa thể xác nhận bản nháp: " + str(exc),
            )
            return

        self.telegram.send_message(
            chat_id,
            "Bản xem trước sản phẩm sẽ tạo "
            + f"({preview.get('so_phien_ban', 1)} sản phẩm):\n\n"
            + draft.summary(),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Xác nhận tạo", "callback_data": "confirm_product"},
                        {"text": "Hủy", "callback_data": "cancel_draft"},
                    ]
                ]
            },
        )

    def _ensure_variant_images_match(self, draft: ProductDraft) -> None:
        valid_skus = {
            str(variant.get("maSKU") or "").strip().casefold()
            for variant in draft.variants
            if str(variant.get("maSKU") or "").strip()
        }
        invalid = [
            sku for sku in draft.anhPhienBan if sku.strip().casefold() not in valid_skus
        ]
        if invalid:
            raise SellApiError(
                "Ảnh riêng không còn khớp SKU phiên bản: " + ", ".join(invalid)
            )

    _MISSING_FIELD_LABELS = {"ten": "tên sản phẩm", "maSKU": "mã SKU"}

    def _send_missing(self, chat_id: int | str, draft: ProductDraft) -> None:
        missing_labels = [
            self._MISSING_FIELD_LABELS.get(field, field)
            for field in draft.missing_required()
        ]
        self.telegram.send_message(
            chat_id,
            "Bản nháp còn thiếu: "
            + ", ".join(missing_labels)
            + "\n\n"
            + draft.summary()
            + "\n\nHãy gửi bổ sung, ít nhất cần tên và SKU.",
        )

    def _cancel_draft(self, chat_id: int | str) -> None:
        draft = self.drafts.get(chat_id)
        if draft:
            self._cleanup_media(draft)
        self.drafts.delete(chat_id)

    def _cleanup_media(self, draft: ProductDraft) -> None:
        for raw_path in [*draft.anhChung, *draft.anhPhienBan.values()]:
            self._delete_media_path(Path(raw_path))

        draft_dir = (
            self.settings.media_dir
            / self._safe_component("")
            / draft.draft_id
        )
        for candidate in {Path(path).parent for path in draft.anhChung} | {
            Path(path).parent for path in draft.anhPhienBan.values()
        }:
            self._remove_empty_media_dirs(candidate)

    def _delete_media_path(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            root = self.settings.media_dir.resolve()
            if resolved.is_relative_to(root) and resolved.is_file():
                resolved.unlink()
        except OSError:
            pass

    def _remove_empty_media_dirs(self, directory: Path) -> None:
        root = self.settings.media_dir.resolve()
        try:
            current = directory.resolve()
            while current != root and current.is_relative_to(root):
                current.rmdir()
                current = current.parent
        except OSError:
            pass

    def _image_file_info(
        self,
        message: dict[str, Any],
    ) -> tuple[str, int, str]:
        photos = message.get("photo") or []
        if photos:
            photo = photos[-1]
            return (
                str(photo["file_id"]),
                int(photo.get("file_size") or 0),
                ".jpg",
            )

        document = message.get("document") or {}
        mime_type = str(document.get("mime_type") or "").lower()
        extension = Path(str(document.get("file_name") or "")).suffix.lower()
        allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
        if not mime_type.startswith("image/") or extension not in allowed_extensions:
            raise ValueError("Chỉ chấp nhận ảnh JPG, JPEG, PNG hoặc WEBP.")
        return (
            str(document["file_id"]),
            int(document.get("file_size") or 0),
            extension,
        )

    def _canonical_variant_sku(
        self,
        draft: ProductDraft,
        requested_sku: str,
    ) -> str:
        requested = requested_sku.strip().casefold()
        for variant in draft.variants:
            sku = str(variant.get("maSKU") or "").strip()
            if sku.casefold() == requested:
                return sku
        return ""

    @staticmethod
    def _variant_sku_from_caption(caption: str) -> str:
        plain = remove_accents(caption)
        match = re.search(
            r"\bsku(?:\s*phien\s*ban)?\s*[:=\-]?\s*([a-z0-9_-]+)",
            plain,
        )
        if not match:
            match = re.search(
                r"\bphien\s*ban\s*[:=\-]?\s*([a-z0-9_-]+)",
                plain,
            )
        return match.group(1) if match else ""

    @staticmethod
    def _looks_like_product_description(candidate: ProductDraft) -> bool:
        """A real product name is always a description. Without a name, a
        maSKU ALONE is too ambiguous (could be a typo'd variant-SKU
        reference) — only count it once some OTHER real field is present too,
        so the user gets a full preview/"missing: ten" instead of a bogus
        "SKU not found" error."""
        if candidate.ten:
            return True
        if not candidate.maSKU:
            return False
        return bool(
            candidate.maVach
            or candidate.viTri
            or candidate.nhanHieu
            or candidate.loaiSanPham
            or candidate.donViTinh
            or candidate.khoiLuong > 0
            or candidate.tonKhoBanDau > 0
            or candidate.thuocTinhs
            or candidate.variants
            or candidate.giaBanLe > 0
            or candidate.giaBanBuon > 0
            or candidate.giaCongTacVien > 0
            or candidate.giaOrder > 0
            or candidate.giaOrderBuonCtv > 0
            or candidate.giaNhap > 0
        )

    @staticmethod
    def _command(text: str) -> str:
        if not text.startswith("/"):
            return ""
        return text.split(maxsplit=1)[0].split("@", 1)[0].lower()

    @staticmethod
    def _safe_component(value: int | str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(value)) or "_"
