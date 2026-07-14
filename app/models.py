from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def normalize_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()
    multiplier = 1.0
    if text.endswith("k"):
        multiplier = 1000.0
        text = text[:-1]
    elif text.endswith("tr"):
        multiplier = 1_000_000.0
        text = text[:-2]

    text = text.replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def new_draft_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ProductDraft:
    ten: str = ""
    maSKU: str = ""
    maVach: str = ""
    khoiLuong: float = 0.0
    donVi: str = "g"
    donViTinh: str = ""
    giaBanLe: float = 0.0
    giaBanBuon: float = 0.0
    giaCongTacVien: float = 0.0
    giaOrder: float = 0.0
    giaOrderBuonCtv: float = 0.0
    giaNhap: float = 0.0
    viTri: str = ""
    nhanHieu: str = ""
    loaiSanPham: str = ""
    suDungKhoHang: bool = False
    tonKhoBanDau: int = 0
    thuocTinhs: list[dict[str, Any]] = field(default_factory=list)
    variants: list[dict[str, Any]] = field(default_factory=list)
    chinhSachGia: list[dict[str, Any]] = field(default_factory=list)
    anhChung: list[str] = field(default_factory=list)
    anhPhienBan: dict[str, str] = field(default_factory=dict)
    draft_id: str = field(default_factory=new_draft_id)
    revision: int = 1
    source_text: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductDraft":
        draft = cls()
        for key in asdict(draft).keys():
            if key in data and data[key] is not None:
                setattr(draft, key, data[key])
        draft.clean()
        return draft

    def clean(self) -> None:
        for key in [
            "ten",
            "maSKU",
            "maVach",
            "donViTinh",
            "viTri",
            "nhanHieu",
            "loaiSanPham",
            "source_text",
        ]:
            setattr(self, key, str(getattr(self, key, "") or "").strip())

        for key in [
            "khoiLuong",
            "giaBanLe",
            "giaBanBuon",
            "giaCongTacVien",
            "giaOrder",
            "giaOrderBuonCtv",
            "giaNhap",
        ]:
            setattr(self, key, max(0.0, normalize_number(getattr(self, key, 0))))

        try:
            self.tonKhoBanDau = max(0, int(float(self.tonKhoBanDau or 0)))
        except (TypeError, ValueError):
            self.tonKhoBanDau = 0

        self.suDungKhoHang = bool(self.suDungKhoHang or self.tonKhoBanDau > 0)
        self.thuocTinhs = self._clean_attributes(self.thuocTinhs)
        self.variants = [dict(item) for item in self.variants if isinstance(item, dict)]
        self.chinhSachGia = [dict(item) for item in self.chinhSachGia if isinstance(item, dict)]
        self.anhChung = list(dict.fromkeys(str(path) for path in self.anhChung if path))
        self.anhPhienBan = {
            str(sku).strip(): str(path)
            for sku, path in dict(self.anhPhienBan or {}).items()
            if str(sku).strip() and path
        }

        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(self.draft_id or "")):
            self.draft_id = new_draft_id()
        try:
            self.revision = max(1, int(self.revision))
        except (TypeError, ValueError):
            self.revision = 1

    def merge_from(self, patch: "ProductDraft") -> None:
        for key in [
            "ten",
            "maSKU",
            "maVach",
            "donViTinh",
            "nhanHieu",
            "loaiSanPham",
        ]:
            value = str(getattr(patch, key, "") or "").strip()
            if value:
                setattr(self, key, value)

        if patch.viTri:
            old_vi_tri = self.viTri
            self.viTri = patch.viTri.strip()
            self._cascade_text_field("viTri", old_vi_tri, self.viTri)

        if patch.khoiLuong > 0:
            old_khoi_luong = self.khoiLuong
            old_don_vi = self.donVi
            self.khoiLuong = normalize_number(patch.khoiLuong)
            if patch.donVi:
                self.donVi = patch.donVi
            self._cascade_weight_field(old_khoi_luong, old_don_vi)

        for key in [
            "giaBanLe",
            "giaBanBuon",
            "giaCongTacVien",
            "giaOrder",
            "giaOrderBuonCtv",
            "giaNhap",
        ]:
            value = normalize_number(getattr(patch, key, 0))
            if value > 0:
                old_value = getattr(self, key)
                setattr(self, key, value)
                self._cascade_numeric_field(key, old_value, value)

        if patch.tonKhoBanDau > 0:
            old_ton = self.tonKhoBanDau
            self.tonKhoBanDau = patch.tonKhoBanDau
            self.suDungKhoHang = True
            self._cascade_numeric_field("tonKho", old_ton, self.tonKhoBanDau, as_int=True)
        if patch.thuocTinhs:
            self.thuocTinhs = patch.thuocTinhs
            self.variants = []
        if patch.variants:
            self.variants = patch.variants
        if patch.chinhSachGia:
            self.chinhSachGia = patch.chinhSachGia

        if patch.source_text:
            self.source_text = "\n".join(filter(None, [self.source_text, patch.source_text]))
        self.revision += 1
        self.clean()

    def _cascade_numeric_field(
        self, field: str, old_value: float, new_value: float, as_int: bool = False
    ) -> None:
        """A product-level field update must reach variants still on the OLD
        value — they were baked by an earlier /xem preview, and once baked,
        Laravel trusts whatever explicit per-variant value is already in the
        payload instead of falling back to the (now-changed) product default.
        Variants a user deliberately set to something ELSE are left alone."""
        cast = (lambda v: int(float(v or 0))) if as_int else (lambda v: float(v or 0))
        old_cast = cast(old_value)
        for variant in self.variants:
            if cast(variant.get(field, 0)) == old_cast:
                variant[field] = new_value

    def _cascade_text_field(self, field: str, old_value: str, new_value: str) -> None:
        old_norm = str(old_value or "")
        for variant in self.variants:
            if str(variant.get(field, "") or "") == old_norm:
                variant[field] = new_value

    def _cascade_weight_field(self, old_khoi_luong: float, old_don_vi: str) -> None:
        # khoiLuong and donVi travel together — updating one without the
        # other would leave a variant showing the new number with the stale unit.
        old_khoi_luong = float(old_khoi_luong or 0)
        for variant in self.variants:
            if float(variant.get("khoiLuong", 0) or 0) == old_khoi_luong:
                variant["khoiLuong"] = self.khoiLuong
                variant["donVi"] = self.donVi

    def apply_preview(self, canonical_product: dict[str, Any]) -> None:
        local_images = list(self.anhChung)
        variant_images = dict(self.anhPhienBan)
        metadata = (self.draft_id, self.revision, self.source_text)

        refreshed = ProductDraft.from_dict(canonical_product)
        for key in self.product_field_names():
            setattr(self, key, getattr(refreshed, key))
        self.anhChung = local_images
        self.anhPhienBan = variant_images
        self.draft_id, self.revision, self.source_text = metadata
        self.clean()

    def missing_required(self) -> list[str]:
        missing = []
        if not self.ten:
            missing.append("ten")
        if not self.maSKU:
            missing.append("maSKU")
        return missing

    def idempotency_hash(self, telegram_user_id: int | str) -> str:
        payload = json.dumps(self.to_product_payload(), ensure_ascii=False, sort_keys=True)
        raw = f"{telegram_user_id}|{payload}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_product_payload(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.product_field_names()}

    def to_storage_payload(self) -> dict[str, Any]:
        return asdict(self)

    def to_sell_payload(self, telegram_user_id: int | str) -> dict[str, Any]:
        return {
            "telegram_user_id": str(telegram_user_id),
            "idempotency_key": self.idempotency_hash(telegram_user_id),
            "san_pham": self.to_product_payload(),
        }

    def to_sell_v2_payload(self, telegram_user_id: int | str) -> dict[str, Any]:
        return {
            "telegram_user_id": str(telegram_user_id),
            "draft_id": self.draft_id,
            "revision": self.revision,
            "san_pham": self.to_product_payload(),
        }

    def summary(self) -> str:
        # giaCongTacVien is intentionally NOT shown here: the current sell web UI
        # (form /taoSanPham) no longer has that field, only the 5 rows below.
        # It still round-trips in to_product_payload() so nothing sent to Laravel changes.
        rows = [
            ("Tên", self.ten or "-"),
            ("SKU gốc", self.maSKU or "-"),
            ("Barcode", self.maVach or "-"),
            ("Nhãn hiệu", self.nhanHieu or "-"),
            ("Loại", self.loaiSanPham or "-"),
            ("Vị trí", self.viTri or "-"),
            ("Khối lượng", f"{self.khoiLuong:,.0f} {self.donVi}" if self.khoiLuong else "-"),
            ("Đơn vị tính", self.donViTinh or "-"),
            ("Giá bán lẻ", f"{self.giaBanLe:,.0f}"),
            ("Giá bán buôn - CTV", f"{self.giaBanBuon:,.0f}"),
            ("Giá order lẻ", f"{self.giaOrder:,.0f}"),
            ("Giá order buôn - CTV", f"{self.giaOrderBuonCtv:,.0f}"),
            ("Giá nhập", f"{self.giaNhap:,.0f}"),
            ("Tồn ban đầu", str(self.tonKhoBanDau)),
            ("Ảnh chung", str(len(self.anhChung))),
            ("Ảnh riêng phiên bản", str(len(self.anhPhienBan))),
        ]
        lines = [f"{label}: {value}" for label, value in rows]

        if self.thuocTinhs:
            lines.append("Thuộc tính:")
            for attribute in self.thuocTinhs:
                lines.append(f"- {attribute['ten']}: {', '.join(attribute['giaTri'])}")

        if self.variants:
            lines.append(f"Phiên bản ({len(self.variants)}):")
            for variant in self.variants[:10]:
                values = " / ".join(str(value) for value in variant.get("attributes", []))
                lines.append(
                    f"- {variant.get('maSKU', '-')}: {values or variant.get('name', '-')}"
                    f" | giá {float(variant.get('giaBanLe', 0)):,.0f}"
                    f" | tồn {int(float(variant.get('tonKho', 0) or 0))}"
                )
            if len(self.variants) > 10:
                lines.append(f"- ... và {len(self.variants) - 10} phiên bản khác")

        return "\n".join(lines)[:3800]

    @staticmethod
    def product_field_names() -> list[str]:
        return [
            "ten",
            "maSKU",
            "maVach",
            "khoiLuong",
            "donVi",
            "donViTinh",
            "giaBanLe",
            "giaBanBuon",
            "giaCongTacVien",
            "giaOrder",
            "giaOrderBuonCtv",
            "giaNhap",
            "viTri",
            "nhanHieu",
            "loaiSanPham",
            "suDungKhoHang",
            "tonKhoBanDau",
            "thuocTinhs",
            "variants",
            "chinhSachGia",
        ]

    @staticmethod
    def _clean_attributes(raw: Any) -> list[dict[str, Any]]:
        result = []
        if not isinstance(raw, list):
            return result
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("ten") or "").strip()
            values = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in item.get("giaTri", [])
                    if str(value).strip()
                )
            )
            if name and values:
                result.append({"ten": name, "giaTri": values})
        return result
