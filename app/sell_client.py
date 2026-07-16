from __future__ import annotations

import http.client
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ProductDraft


class SellApiError(RuntimeError):
    pass


class StockConfirmNeeded(SellApiError):
    """Order create returned HTTP 409 (can_confirm_stock): some products still
    have stock, so Sell wants an explicit xac_nhan_ton_kho before committing.
    Carries the affected products so the bot can ask the user to confirm."""

    def __init__(self, message: str, san_phams: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.san_phams = san_phams


class SessionExpired(SellApiError):
    """A token-authenticated order call returned 401: the login session is gone
    or expired, so the bot must ask the user to log in again."""


# Cloudflare's default Bot Fight Mode blocks the stdlib's plain User-Agent
# ("Python-urllib/3.x") with Error 1010 (browser_signature_banned) even for
# legitimate internal calls carrying a valid bearer token. A normal desktop
# browser UA avoids that check without touching the sell project itself.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SellClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview_product(
        self,
        telegram_user_id: int | str,
        draft: ProductDraft,
    ) -> dict[str, Any]:
        data = self._request_json(
            self.settings.sell_product_v2_preview_url,
            draft.to_sell_v2_payload(telegram_user_id),
        )
        preview = data.get("xem_truoc")
        if not data.get("thanh_cong") or not isinstance(preview, dict):
            raise SellApiError(str(data.get("thong_bao") or "Sell API rejected preview"))
        return preview

    def create_product(
        self,
        telegram_user_id: int | str,
        draft: ProductDraft,
    ) -> dict[str, Any]:
        payload = draft.to_sell_v2_payload(telegram_user_id)
        files = self._collect_files(draft)

        if files:
            data = self._request_multipart(
                self.settings.sell_product_v2_create_url,
                payload,
                files,
            )
        else:
            data = self._request_json(
                self.settings.sell_product_v2_create_url,
                payload,
                timeout=60,
            )

        if not data.get("thanh_cong"):
            raise SellApiError(str(data.get("thong_bao") or "Sell API rejected product"))
        return data

    # ---- Đơn order (order-order) --------------------------------------

    def dang_nhap(self, email: str, mat_khau: str) -> dict[str, Any]:
        """Verify employee credentials. Returns {token, nhan_vien{...}} on
        success; raises SellApiError (with Sell's message) on wrong password or
        insufficient permission."""
        data = self._request_json(
            self.settings.sell_order_login_url,
            {"email": email, "mat_khau": mat_khau},
        )
        if not data.get("success") or not data.get("token"):
            raise SellApiError(str(data.get("message") or "Đăng nhập thất bại"))
        return data

    def dang_xuat(self, session_token: str) -> None:
        try:
            self._request_json(self.settings.sell_order_logout_url, {"session_token": session_token})
        except SellApiError:
            pass

    def list_customers(self, session_token: str, trang: int = 1) -> dict[str, Any]:
        data = self._order_get(
            self.settings.sell_order_customers_url,
            {"session_token": session_token, "trang": trang},
        )
        return data

    def list_employees(self, session_token: str, khach_hang_id: int | str) -> list[dict[str, Any]]:
        data = self._order_get(
            self.settings.sell_order_employees_url,
            {"session_token": session_token, "khach_hang_id": khach_hang_id},
        )
        result = data.get("data")
        return result if isinstance(result, list) else []

    def search_products(self, session_token: str, tu_khoa: str) -> list[dict[str, Any]]:
        data = self._order_get(
            self.settings.sell_order_search_product_url,
            {"session_token": session_token, "tu_khoa": tu_khoa, "per_page": 8},
        )
        result = data.get("data")
        return result if isinstance(result, list) else []

    def create_order(self, session_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the order. Returns the created-order data on success. Raises
        StockConfirmNeeded on HTTP 409 (products still in stock) so the caller
        can re-submit with xac_nhan_ton_kho=true; SessionExpired on 401;
        SellApiError otherwise."""
        body = dict(payload)
        body["session_token"] = session_token

        request = urllib.request.Request(
            self.settings.sell_order_create_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.sell_internal_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = self._decode_response(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 409:
                parsed = self._safe_json(raw)
                if isinstance(parsed, dict) and parsed.get("can_confirm_stock"):
                    san_phams = (((parsed.get("data") or {}).get("san_phams")) or [])
                    raise StockConfirmNeeded(
                        str(parsed.get("message") or "Có sản phẩm còn tồn kho"),
                        san_phams if isinstance(san_phams, list) else [],
                    ) from exc
            if exc.code == 401:
                raise SessionExpired(self._extract_error(raw) or "Phiên đăng nhập hết hạn") from exc
            raise SellApiError(
                self._extract_error(raw) or f"Sell API error {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SellApiError(f"Không thể kết nối sell API: {exc.reason}") from exc

        if not data.get("success"):
            raise SellApiError(str(data.get("message") or "Sell API từ chối đơn order"))
        result = data.get("data")
        return result if isinstance(result, dict) else {}

    def fetch_product_image(self, anh_chinh: str) -> bytes | None:
        """Download a product's main image bytes from Sell's public storage so
        the bot can re-upload it to Telegram (see TelegramGateway.send_photo).
        Best-effort: returns None on a missing image or any fetch error."""
        ten = str(anh_chinh or "").strip()
        if not ten:
            return None
        url = (
            self.settings.sell_base_url.rstrip("/")
            + "/storage/uploads/sanpham/"
            + urllib.parse.quote(ten)
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read()
            return data or None
        except Exception:
            return None

    def _order_get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET wrapper for token-authenticated order endpoints: surfaces an
        expired session as SessionExpired so the bot can re-prompt login."""
        try:
            data = self._request_get_json(url, params)
        except SellApiError as exc:
            raise
        if not data.get("success"):
            if data.get("phien_het_han"):
                raise SessionExpired(str(data.get("message") or "Phiên đăng nhập hết hạn"))
            raise SellApiError(str(data.get("message") or "Sell API lỗi"))
        return data

    def _request_get_json(
        self,
        url: str,
        params: dict[str, Any],
        timeout: int = 30,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url + ("&" if "?" in url else "?") + query,
            headers={
                "Authorization": f"Bearer {self.settings.sell_internal_token}",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return self._decode_response(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 401:
                raise SessionExpired(self._extract_error(raw) or "Phiên đăng nhập hết hạn") from exc
            raise SellApiError(
                self._extract_error(raw) or f"Sell API error {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SellApiError(f"Không thể kết nối sell API: {exc.reason}") from exc

    @staticmethod
    def _safe_json(body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _collect_files(self, draft: ProductDraft) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        for raw_path in draft.anhChung:
            path = Path(raw_path)
            if not path.is_file():
                raise SellApiError(f"Không tìm thấy ảnh chung: {path.name}")
            files.append(("anhChung[]", path))

        variant_indexes = {
            str(variant.get("maSKU") or "").strip().casefold(): index
            for index, variant in enumerate(draft.variants)
            if str(variant.get("maSKU") or "").strip()
        }
        for sku, raw_path in draft.anhPhienBan.items():
            index = variant_indexes.get(sku.strip().casefold())
            if index is None:
                raise SellApiError(f"Ảnh riêng không khớp phiên bản SKU {sku}")
            path = Path(raw_path)
            if not path.is_file():
                raise SellApiError(f"Không tìm thấy ảnh phiên bản: {path.name}")
            files.append((f"anhPhienBan[{index}]", path))

        return files

    def _request_json(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: int = 30,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.sell_internal_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return self._decode_response(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise SellApiError(
                self._extract_error(body) or f"Sell API error {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SellApiError(f"Không thể kết nối sell API: {exc.reason}") from exc

    def _request_multipart(
        self,
        url: str,
        payload: dict[str, Any],
        files: list[tuple[str, Path]],
    ) -> dict[str, Any]:
        boundary = "----TelegramProductBot" + uuid.uuid4().hex
        parts = self._multipart_parts(boundary, payload, files)
        content_length = sum(
            len(prefix) + (path.stat().st_size if path else 0)
            for prefix, path in parts
        )
        target = urllib.parse.urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname:
            raise SellApiError("SELL_BASE_URL không hợp lệ")

        connection_class = (
            http.client.HTTPSConnection
            if target.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            target.hostname,
            target.port,
            timeout=60,
        )
        path = target.path or "/"
        if target.query:
            path += "?" + target.query

        try:
            connection.putrequest("POST", path)
            connection.putheader(
                "Authorization",
                f"Bearer {self.settings.sell_internal_token}",
            )
            connection.putheader(
                "Content-Type",
                f"multipart/form-data; boundary={boundary}",
            )
            connection.putheader("Content-Length", str(content_length))
            connection.putheader("Accept", "application/json")
            connection.putheader("User-Agent", _USER_AGENT)
            connection.endheaders()

            for prefix, file_path in parts:
                connection.send(prefix)
                if file_path:
                    with file_path.open("rb") as image:
                        while True:
                            chunk = image.read(64 * 1024)
                            if not chunk:
                                break
                            connection.send(chunk)

            response = connection.getresponse()
            body = response.read()
            if response.status < 200 or response.status >= 300:
                raise SellApiError(
                    self._extract_error(body) or f"Sell API error {response.status}"
                )
            return self._decode_response(body)
        except SellApiError:
            raise
        except OSError as exc:
            raise SellApiError(f"Không thể kết nối sell API: {exc}") from exc
        finally:
            connection.close()

    def _multipart_parts(
        self,
        boundary: str,
        payload: dict[str, Any],
        files: list[tuple[str, Path]],
    ) -> list[tuple[bytes, Path | None]]:
        json_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        parts: list[tuple[bytes, Path | None]] = [
            (
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="payload"\r\n'
                    "Content-Type: application/json; charset=utf-8\r\n\r\n"
                ).encode("ascii")
                + json_body
                + b"\r\n",
                None,
            )
        ]

        for field_name, file_path in files:
            filename = file_path.name.replace('"', "")
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            prefix = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
            parts.append((prefix, file_path))
            parts.append((b"\r\n", None))

        parts.append((f"--{boundary}--\r\n".encode("ascii"), None))
        return parts

    def _decode_response(self, body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SellApiError("Sell API trả về dữ liệu không hợp lệ") from exc
        if not isinstance(data, dict):
            raise SellApiError("Sell API trả về dữ liệu không hợp lệ")
        return data

    def _extract_error(self, body: bytes | str) -> str:
        raw = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:500]

        message = str(data.get("thong_bao") or data.get("message") or "").strip()
        errors = data.get("loi")
        details: list[str] = []
        if isinstance(errors, dict):
            for values in errors.values():
                if isinstance(values, list):
                    details.extend(str(value) for value in values)
                elif values:
                    details.append(str(values))
                if len(details) >= 3:
                    break
        if details:
            return (message + ": " if message else "") + "; ".join(details[:3])
        return message or raw[:500]
