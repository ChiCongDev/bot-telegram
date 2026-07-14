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
