from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .nlp_parser import remove_accents
from .sell_client import SellApiError, SellClient, SessionExpired, StockConfirmNeeded

# Conversation steps for building one order (đơn order).
BUOC_CHON_TAI_KHOAN = "chon_tai_khoan"  # already logged in: continue or switch account
BUOC_LOGIN_EMAIL = "login_email"
BUOC_LOGIN_MATKHAU = "login_matkhau"
BUOC_CHON_KHACH = "chon_khach"      # numbered customer page shown, waiting for a number
BUOC_CHON_NHANVIEN = "chon_nhanvien"  # numbered employee list shown
BUOC_NHAP_SP = "nhap_sp"            # entering products ("tên/SKU số_lượng [giá]")
BUOC_CHON_SP = "chon_sp"            # multiple product matches -> waiting for a number
BUOC_XAC_NHAN = "xac_nhan"          # draft shown, waiting for confirm/cancel

_TU_HUY = {"huy"}
_TU_XONG = {"xong", "tao", "done"}
_TU_SAU = {"sau", "trang sau", ">"}
_TU_TRUOC = {"truoc", "trang truoc", "<"}


def _tien(value: Any) -> str:
    try:
        return f"{float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _chuan(text: str) -> str:
    return remove_accents(str(text or "")).strip()


@dataclass
class OrderDraft:
    buoc: str = BUOC_CHON_KHACH
    email: str = ""
    khach_hang_id: int | None = None
    khach_ten: str = ""
    khach_sdt: str = ""
    kh_trang: int = 1
    kh_tong_trang: int = 1
    kh_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    nhan_vien_id: int | None = None
    nhan_vien_ten: str = ""
    nv_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    lines: list[dict[str, Any]] = field(default_factory=list)
    sp_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_sl: int = 0
    pending_gia: float | None = None
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderDraft":
        draft = cls()
        for key in asdict(draft).keys():
            if key in data and data[key] is not None:
                setattr(draft, key, data[key])
        return draft

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def tong_tien(self) -> float:
        return sum(
            float(l.get("gia", 0) or 0) * int(l.get("so_luong", 0) or 0) for l in self.lines
        )


class _SqliteStore:
    def __init__(self, data_dir: Path, table: str, ddl: str) -> None:
        self.path = data_dir / "drafts.sqlite3"
        self.table = table
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(ddl)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout = 5000")
        return db


class OrderStore(_SqliteStore):
    """Per-chat in-progress order draft. Separate table from the product
    DraftStore so the two flows never collide."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(
            data_dir,
            "order_drafts",
            "CREATE TABLE IF NOT EXISTS order_drafts ("
            "chat_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)",
        )

    def get(self, chat_id: int | str) -> OrderDraft | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM order_drafts WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        return OrderDraft.from_dict(payload) if isinstance(payload, dict) else None

    def save(self, chat_id: int | str, draft: OrderDraft) -> None:
        payload = json.dumps(draft.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                "INSERT INTO order_drafts (chat_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (str(chat_id), payload, datetime.now(timezone.utc).isoformat()),
            )

    def delete(self, chat_id: int | str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM order_drafts WHERE chat_id = ?", (str(chat_id),))


class SessionStore(_SqliteStore):
    """Remembers a logged-in employee's session token per chat, so the user
    does not re-enter the password on every /order until it expires or logout."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(
            data_dir,
            "order_sessions",
            "CREATE TABLE IF NOT EXISTS order_sessions ("
            "chat_id TEXT PRIMARY KEY, token TEXT NOT NULL, ten TEXT NOT NULL, updated_at TEXT NOT NULL)",
        )

    def get(self, chat_id: int | str) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT token, ten FROM order_sessions WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
        return {"token": row[0], "ten": row[1]} if row else None

    def save(self, chat_id: int | str, token: str, ten: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO order_sessions (chat_id, token, ten, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET token = excluded.token, ten = excluded.ten, "
                "updated_at = excluded.updated_at",
                (str(chat_id), token, ten, datetime.now(timezone.utc).isoformat()),
            )

    def delete(self, chat_id: int | str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM order_sessions WHERE chat_id = ?", (str(chat_id),))


class OrderFlow:
    """State machine that drives order creation over Telegram: login (with a
    remembered session), numbered paginated customer pick, numbered employee
    pick, product entry by name/SKU + quantity, draft confirmation, and a
    'hủy' keyword that aborts at any step. bot.py only forwards text and
    `ord:` callbacks here."""

    def __init__(self, telegram: Any, sell: SellClient, store: OrderStore, sessions: SessionStore) -> None:
        self.telegram = telegram
        self.sell = sell
        self.store = store
        self.sessions = sessions

    # ---- entry points called from bot.py ------------------------------

    def is_active(self, chat_id: int | str) -> bool:
        return self.store.get(chat_id) is not None

    def start(self, chat_id: int | str) -> None:
        session = self.sessions.get(chat_id)
        if session:
            # Already logged in -> let the user continue or switch account,
            # so switching (e.g. admin -> thủ kho) is discoverable in-flow.
            self.store.save(chat_id, OrderDraft(buoc=BUOC_CHON_TAI_KHOAN))
            self.telegram.send_message(
                chat_id,
                f"Đang đăng nhập: {session.get('ten') or '?'}.\nBạn muốn?",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Tiếp tục tạo đơn", "callback_data": "ord:tieptuc"},
                    {"text": "🔁 Đổi tài khoản", "callback_data": "ord:doitk"},
                ]]},
            )
        else:
            self._hoi_dang_nhap(chat_id)

    def _hoi_dang_nhap(self, chat_id: int | str) -> None:
        self.store.save(chat_id, OrderDraft(buoc=BUOC_LOGIN_EMAIL))
        self.telegram.send_message(
            chat_id,
            "🔐 Đăng nhập để tạo đơn order.\nNhập email của bạn (gõ 'hủy' để thoát):",
        )

    def cancel(self, chat_id: int | str) -> None:
        existed = self.store.get(chat_id) is not None
        self.store.delete(chat_id)
        self.telegram.send_message(
            chat_id, "Đã hủy đơn order." if existed else "Không có đơn order nào đang tạo."
        )

    def _doi_tai_khoan(self, chat_id: int | str) -> None:
        session = self.sessions.get(chat_id)
        if session:
            self.sell.dang_xuat(session["token"])
        self.sessions.delete(chat_id)
        self.store.save(chat_id, OrderDraft(buoc=BUOC_LOGIN_EMAIL))
        self.telegram.send_message(
            chat_id, "Đã đăng xuất. Nhập email tài khoản mới (gõ 'hủy' để thoát):"
        )

    def logout(self, chat_id: int | str) -> None:
        session = self.sessions.get(chat_id)
        if session:
            self.sell.dang_xuat(session["token"])
        self.sessions.delete(chat_id)
        self.store.delete(chat_id)
        self.telegram.send_message(chat_id, "Đã đăng xuất khỏi tạo đơn order." if session else "Bạn chưa đăng nhập.")

    def handle_text(
        self, chat_id: int | str, telegram_user_id: int | str, text: str, message_id: int | str | None = None
    ) -> bool:
        draft = self.store.get(chat_id)
        if draft is None:
            return False

        # "hủy" aborts from any step (except while typing the password, where
        # the whole message is deleted and treated as the password).
        if draft.buoc != BUOC_LOGIN_MATKHAU and _chuan(text) in _TU_HUY:
            self.cancel(chat_id)
            return True

        if draft.buoc == BUOC_CHON_TAI_KHOAN:
            self.telegram.send_message(
                chat_id, "Hãy bấm nút: '✅ Tiếp tục tạo đơn' hoặc '🔁 Đổi tài khoản'."
            )
        elif draft.buoc == BUOC_LOGIN_EMAIL:
            self._nhan_email(chat_id, draft, text)
        elif draft.buoc == BUOC_LOGIN_MATKHAU:
            self._nhan_mat_khau(chat_id, draft, text, message_id)
        elif draft.buoc == BUOC_CHON_KHACH:
            self._chon_khach(chat_id, draft, text)
        elif draft.buoc == BUOC_CHON_NHANVIEN:
            self._chon_nhan_vien(chat_id, draft, text)
        elif draft.buoc == BUOC_NHAP_SP:
            self._nhap_san_pham(chat_id, draft, text)
        elif draft.buoc == BUOC_CHON_SP:
            self._chon_san_pham_trung(chat_id, draft, text)
        elif draft.buoc == BUOC_XAC_NHAN:
            self._xac_nhan_text(chat_id, telegram_user_id, draft, text)
        return True

    def handle_callback(self, chat_id: int | str, telegram_user_id: int | str, data: str) -> bool:
        if not str(data).startswith("ord:"):
            return False
        draft = self.store.get(chat_id)
        if draft is None:
            self.telegram.send_message(chat_id, "Không có đơn order nào đang tạo. Dùng /order để bắt đầu.")
            return True

        action = str(data).split(":", 2)[1] if ":" in str(data) else ""
        if action == "tieptuc":
            self._bat_dau_don(chat_id)
        elif action == "doitk":
            self._doi_tai_khoan(chat_id)
        elif action == "xong":
            self._ket_thuc_nhap(chat_id, draft)
        elif action == "them":
            draft.buoc = BUOC_NHAP_SP
            self.store.save(chat_id, draft)
            self.telegram.send_message(chat_id, "Nhập sản phẩm: tên hoặc SKU + số lượng (vd: giày adi33 2).")
        elif action == "tao":
            self._tao_don(chat_id, draft, xac_nhan_ton_kho=False)
        elif action == "taoton":
            self._tao_don(chat_id, draft, xac_nhan_ton_kho=True)
        elif action == "huy":
            self.cancel(chat_id)
        return True

    # ---- login --------------------------------------------------------

    def _nhan_email(self, chat_id, draft: OrderDraft, text: str) -> None:
        draft.email = text.strip()
        draft.buoc = BUOC_LOGIN_MATKHAU
        self.store.save(chat_id, draft)
        self.telegram.send_message(chat_id, "Nhập mật khẩu (tin nhắn sẽ được xoá ngay sau khi kiểm tra):")

    def _nhan_mat_khau(self, chat_id, draft: OrderDraft, text: str, message_id) -> None:
        # Wipe the password message immediately, regardless of the outcome.
        if message_id is not None:
            self.telegram.delete_message(chat_id, message_id)
        try:
            ket_qua = self.sell.dang_nhap(draft.email, text)
        except SellApiError as exc:
            draft.buoc = BUOC_LOGIN_EMAIL
            self.store.save(chat_id, draft)
            self.telegram.send_message(chat_id, "❌ " + str(exc) + "\nNhập lại email (hoặc 'hủy'):")
            return
        nv = ket_qua.get("nhan_vien") or {}
        self.sessions.save(chat_id, str(ket_qua.get("token")), str(nv.get("ten") or ""))
        self.telegram.send_message(chat_id, f"✅ Xin chào {nv.get('ten', '')}.")
        self._bat_dau_don(chat_id)

    # ---- customer -----------------------------------------------------

    def _bat_dau_don(self, chat_id) -> None:
        draft = OrderDraft(buoc=BUOC_CHON_KHACH)
        self.store.save(chat_id, draft)
        self._tai_khach(chat_id, draft, 1)

    def _tai_khach(self, chat_id, draft: OrderDraft, trang: int) -> None:
        token = self._token(chat_id)
        if token is None:
            return
        try:
            data = self.sell.list_customers(token, trang)
        except SessionExpired:
            self._het_phien(chat_id)
            return
        except SellApiError as exc:
            self.telegram.send_message(chat_id, "Lỗi tải khách hàng: " + str(exc))
            return

        ds = data.get("data") or []
        if not ds:
            self.telegram.send_message(chat_id, "Chưa có khách hàng nào trong phạm vi của bạn.")
            return

        draft.kh_trang = int(data.get("trang", trang))
        draft.kh_tong_trang = int(data.get("tong_trang", 1))
        draft.kh_map = {}
        dong = [f"👥 Chọn khách hàng (trang {draft.kh_trang}/{draft.kh_tong_trang}):"]
        for i, kh in enumerate(ds, 1):
            # sdt may be JSON null -> fall back to mã khách hàng, never "None".
            dinh_danh = kh.get("sdt") or kh.get("ma_khach_hang") or ""
            draft.kh_map[str(i)] = {"id": kh.get("id"), "ten": kh.get("ten") or "", "sdt": dinh_danh}
            nhan = kh.get("ten") or "?"
            dong.append(f"{i}. {nhan}" + (f" — {dinh_danh}" if dinh_danh else ""))
        dong.append("\nGõ SỐ để chọn. 'sau'/'truoc' để đổi trang. 'hủy' để thoát.")
        draft.buoc = BUOC_CHON_KHACH
        self.store.save(chat_id, draft)
        self.telegram.send_message(chat_id, "\n".join(dong))

    def _chon_khach(self, chat_id, draft: OrderDraft, text: str) -> None:
        key = _chuan(text)
        if key in _TU_SAU:
            if draft.kh_trang < draft.kh_tong_trang:
                self._tai_khach(chat_id, draft, draft.kh_trang + 1)
            else:
                self.telegram.send_message(chat_id, "Đã ở trang cuối.")
            return
        if key in _TU_TRUOC:
            if draft.kh_trang > 1:
                self._tai_khach(chat_id, draft, draft.kh_trang - 1)
            else:
                self.telegram.send_message(chat_id, "Đã ở trang đầu.")
            return

        kh = draft.kh_map.get(text.strip())
        if not kh:
            self.telegram.send_message(chat_id, "Số không hợp lệ. Gõ số trong danh sách, hoặc 'sau'/'truoc'.")
            return
        draft.khach_hang_id = int(kh["id"])
        draft.khach_ten = kh.get("ten", "")
        draft.khach_sdt = kh.get("sdt", "")
        self._tai_nhan_vien(chat_id, draft)

    # ---- employee -----------------------------------------------------

    def _tai_nhan_vien(self, chat_id, draft: OrderDraft) -> None:
        token = self._token(chat_id)
        if token is None:
            return
        try:
            ds = self.sell.list_employees(token, draft.khach_hang_id)
        except SessionExpired:
            self._het_phien(chat_id)
            return
        except SellApiError as exc:
            self.telegram.send_message(chat_id, "Lỗi tải nhân viên: " + str(exc))
            return

        if not ds:
            # Mirrors the web rule: a customer with no assigned sales staff
            # (cấp 1/2) cannot have an order created. Stay on customer selection.
            self.telegram.send_message(
                chat_id,
                f"⚠ Khách '{draft.khach_ten}' chưa được gán nhân viên bán hàng, "
                "không thể tạo đơn order.\n"
                "Hãy gán nhân viên bán hàng cho khách này trong hệ thống rồi thử lại. "
                "Gõ SỐ để chọn khách khác, hoặc 'hủy'.",
            )
            return

        draft.nv_map = {}
        dong = [f"Khách: {draft.khach_ten} ({draft.khach_sdt})", "", "👤 Chọn nhân viên phụ trách:"]
        for i, nv in enumerate(ds, 1):
            draft.nv_map[str(i)] = {"id": nv.get("id"), "ten": nv.get("ten") or ""}
            dong.append(f"{i}. {nv.get('ten') or '?'}")
        dong.append("\nGõ SỐ để chọn.")
        draft.buoc = BUOC_CHON_NHANVIEN
        self.store.save(chat_id, draft)
        self.telegram.send_message(chat_id, "\n".join(dong))

    def _chon_nhan_vien(self, chat_id, draft: OrderDraft, text: str) -> None:
        nv = draft.nv_map.get(text.strip())
        if not nv:
            self.telegram.send_message(chat_id, "Số không hợp lệ. Gõ số trong danh sách.")
            return
        draft.nhan_vien_id = int(nv["id"])
        draft.nhan_vien_ten = nv.get("ten", "")
        draft.buoc = BUOC_NHAP_SP
        self.store.save(chat_id, draft)
        self.telegram.send_message(
            chat_id,
            f"Nhân viên: {draft.nhan_vien_ten}.\n\n"
            "Nhập sản phẩm theo dạng: TÊN hoặc MÃ SKU + SỐ LƯỢNG.\n"
            "Ví dụ: giày adi33 2   hoặc   3333-trang-20 2\n"
            "Gõ 'xong' khi đã đủ, 'hủy' để thoát.",
        )

    # ---- products -----------------------------------------------------

    def _nhap_san_pham(self, chat_id, draft: OrderDraft, text: str) -> None:
        if _chuan(text) in _TU_XONG:
            self._ket_thuc_nhap(chat_id, draft)
            return

        parsed = self._tach_dong_sp(text)
        if parsed is None:
            self.telegram.send_message(
                chat_id, "Chưa rõ. Nhập: TÊN/SKU + SỐ LƯỢNG (vd: giày adi33 2). Hoặc 'xong'."
            )
            return
        tu_khoa, so_luong, gia = parsed

        token = self._token(chat_id)
        if token is None:
            return
        try:
            ket_qua = self.sell.search_products(token, tu_khoa)
        except SessionExpired:
            self._het_phien(chat_id)
            return
        except SellApiError as exc:
            self.telegram.send_message(chat_id, "Lỗi tìm sản phẩm: " + str(exc))
            return

        if not ket_qua:
            self.telegram.send_message(chat_id, f"Không tìm thấy sản phẩm cho '{tu_khoa}'. Thử lại.")
            return

        if len(ket_qua) == 1:
            self._them_dong(chat_id, draft, ket_qua[0], so_luong, gia)
            return

        # Multiple matches -> ask the user to pick a number, remember qty/price.
        draft.sp_map = {}
        draft.pending_sl = so_luong
        draft.pending_gia = gia
        dong = [f"Có {len(ket_qua)} sản phẩm khớp '{tu_khoa}', chọn SỐ:"]
        for i, sp in enumerate(ket_qua, 1):
            draft.sp_map[str(i)] = sp
            dong.append(f"{i}. {sp.get('ten', '?')} [{sp.get('ma_sku', '')}] — {_tien(sp.get('gia_order'))}")
        draft.buoc = BUOC_CHON_SP
        self.store.save(chat_id, draft)
        self.telegram.send_message(chat_id, "\n".join(dong))

    def _chon_san_pham_trung(self, chat_id, draft: OrderDraft, text: str) -> None:
        sp = draft.sp_map.get(text.strip())
        if not sp:
            self.telegram.send_message(chat_id, "Số không hợp lệ. Gõ số trong danh sách.")
            return
        so_luong = draft.pending_sl or 1
        gia = draft.pending_gia
        draft.sp_map = {}
        draft.pending_sl = 0
        draft.pending_gia = None
        self._them_dong(chat_id, draft, sp, so_luong, gia)

    def _them_dong(self, chat_id, draft: OrderDraft, sp: dict[str, Any], so_luong: int, gia: float | None) -> None:
        gia_dung = float(gia) if gia is not None else float(sp.get("gia_order", 0) or 0)
        draft.lines.append({
            "san_pham_id": int(sp.get("id")),
            "ten": sp.get("ten", ""),
            "ma_sku": sp.get("ma_sku", ""),
            "so_luong": int(so_luong),
            "gia": gia_dung,
        })
        draft.buoc = BUOC_NHAP_SP
        self.store.save(chat_id, draft)
        thanh_tien = gia_dung * int(so_luong)
        caption = (
            f"✅ Đã thêm: {sp.get('ten')} [{sp.get('ma_sku')}] × {so_luong} = {_tien(thanh_tien)}"
            f"\n(Đơn hiện có {len(draft.lines)} SP.) Nhập SP tiếp, hoặc 'xong'."
        )
        # Show the product's main image inline (visible without a click). Fall
        # back to a plain text confirmation if there is no image or it fails.
        anh_chinh = sp.get("anh_chinh")
        da_gui_anh = False
        if anh_chinh:
            anh_bytes = self.sell.fetch_product_image(anh_chinh)
            if anh_bytes:
                da_gui_anh = self.telegram.send_photo(
                    chat_id, anh_bytes, str(anh_chinh), caption=caption
                )
        if not da_gui_anh:
            self.telegram.send_message(chat_id, caption)

    def _ket_thuc_nhap(self, chat_id, draft: OrderDraft) -> None:
        if not draft.lines:
            self.telegram.send_message(chat_id, "Đơn chưa có sản phẩm nào. Hãy nhập ít nhất 1 sản phẩm.")
            return
        draft.buoc = BUOC_XAC_NHAN
        self.store.save(chat_id, draft)
        self.telegram.send_message(chat_id, self._tom_tat(draft), reply_markup=self._nut_xac_nhan())

    # ---- confirm / create ---------------------------------------------

    def _xac_nhan_text(self, chat_id, telegram_user_id, draft: OrderDraft, text: str) -> None:
        key = _chuan(text)
        if key in {"tao", "xac nhan", "co", "ok", "dong y"}:
            self._tao_don(chat_id, draft, xac_nhan_ton_kho=False)
        elif key in {"them"}:
            draft.buoc = BUOC_NHAP_SP
            self.store.save(chat_id, draft)
            self.telegram.send_message(chat_id, "Nhập sản phẩm: tên hoặc SKU + số lượng.")
        else:
            self.telegram.send_message(chat_id, "Bấm nút, hoặc gõ 'tạo' để tạo, 'hủy' để hủy.")

    def _tao_don(self, chat_id, draft: OrderDraft, xac_nhan_ton_kho: bool) -> None:
        if draft.khach_hang_id is None or not draft.lines:
            self.telegram.send_message(chat_id, "Đơn chưa đủ thông tin (khách hàng và sản phẩm).")
            return
        token = self._token(chat_id)
        if token is None:
            return

        payload = {
            "khach_hang_id": draft.khach_hang_id,
            "nhan_vien_ban_hang_id": draft.nhan_vien_id,
            "san_phams": [
                {
                    "san_pham_id": l["san_pham_id"],
                    "so_luong": int(l["so_luong"]),
                    "gia_ban_du_kien": float(l["gia"]),
                }
                for l in draft.lines
            ],
            "xac_nhan_ton_kho": bool(xac_nhan_ton_kho),
            "idempotency_key": draft.idempotency_key,
        }

        try:
            ket_qua = self.sell.create_order(token, payload)
        except StockConfirmNeeded as exc:
            ten_sp = ", ".join(str(sp.get("ten") or sp.get("ma_sku") or "?") for sp in exc.san_phams[:5])
            self.telegram.send_message(
                chat_id,
                "⚠ Có sản phẩm CÒN TỒN KHO" + (f" ({ten_sp})" if ten_sp else "") + ".\nVẫn tạo đơn order chứ?",
                reply_markup={"inline_keyboard": [[
                    {"text": "Vẫn tạo", "callback_data": "ord:taoton"},
                    {"text": "✖ Hủy", "callback_data": "ord:huy"},
                ]]},
            )
            return
        except SessionExpired:
            self._het_phien(chat_id)
            return
        except SellApiError as exc:
            self.telegram.send_message(chat_id, "Tạo đơn order thất bại: " + str(exc))
            return

        self.store.delete(chat_id)
        dong = "✅ Đã tạo đơn order thành công."
        if ket_qua.get("ma_don_hang"):
            dong += f"\nMã đơn hàng: {ket_qua.get('ma_don_hang')}"
        self.telegram.send_message(chat_id, dong)

    # ---- helpers ------------------------------------------------------

    def _token(self, chat_id) -> str | None:
        session = self.sessions.get(chat_id)
        if not session:
            self._het_phien(chat_id)
            return None
        return session["token"]

    def _het_phien(self, chat_id) -> None:
        self.sessions.delete(chat_id)
        self.store.delete(chat_id)
        self.telegram.send_message(chat_id, "Phiên đăng nhập đã hết hạn. Gõ /order để đăng nhập lại.")

    @staticmethod
    def _tach_dong_sp(text: str) -> tuple[str, int, float | None] | None:
        tokens = text.split()
        nums: list[str] = []
        while tokens and re.fullmatch(r"\d[\d.,]*", tokens[-1]) and len(nums) < 2:
            nums.insert(0, tokens.pop())
        tu_khoa = " ".join(tokens).strip()
        if not tu_khoa or not nums:
            return None
        so_luong = int(nums[0].replace(".", "").replace(",", ""))
        if so_luong < 1:
            return None
        gia = float(nums[1].replace(".", "").replace(",", "")) if len(nums) >= 2 else None
        return tu_khoa, so_luong, gia

    def _tom_tat(self, draft: OrderDraft) -> str:
        dong = [
            f"🧾 Đơn order — Khách: {draft.khach_ten} ({draft.khach_sdt})",
            f"Nhân viên: {draft.nhan_vien_ten}",
            "",
        ]
        for i, l in enumerate(draft.lines, 1):
            tt = float(l.get("gia", 0) or 0) * int(l.get("so_luong", 0) or 0)
            dong.append(
                f"{i}. {l.get('ten')} [{l.get('ma_sku')}] — SL {l.get('so_luong')} × {_tien(l.get('gia'))} = {_tien(tt)}"
            )
        dong.append("")
        dong.append(f"Tổng: {_tien(draft.tong_tien())}")
        return "\n".join(dong)

    def _nut_xac_nhan(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "➕ Thêm sản phẩm", "callback_data": "ord:them"}],
            [
                {"text": "✅ Xác nhận tạo", "callback_data": "ord:tao"},
                {"text": "✖ Hủy", "callback_data": "ord:huy"},
            ],
        ]}
