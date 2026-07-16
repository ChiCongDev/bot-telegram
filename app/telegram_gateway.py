from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
import urllib.request
from typing import Any


class TelegramGateway:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        data = self._request("getUpdates", params)
        if not data.get("ok"):
            return []
        return data.get("result", [])

    def send_message(self, chat_id: int | str, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._request("sendMessage", payload)

    def send_photo(
        self,
        chat_id: int | str,
        image_bytes: bytes,
        filename: str,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Upload photo bytes so it renders inline in the chat (no click). Bytes
        are uploaded (not a URL) because Telegram's servers cannot reach a local
        Sell instance. Returns False on any failure so the caller can fall back
        to a plain text message."""
        boundary = "----TgBotPhoto" + uuid.uuid4().hex
        fields: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup)

        body = bytearray()
        for name, value in fields.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode("utf-8")
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        body += bytes(image_bytes)
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")

        request = urllib.request.Request(
            self.base_url + "/sendPhoto",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            return bool(data.get("ok"))
        except Exception:
            return False

    def delete_message(self, chat_id: int | str, message_id: int | str) -> None:
        """Best-effort delete (used to wipe a password message the user typed).
        Telegram lets a bot delete incoming messages in private chats; any
        failure (too old, no rights) is swallowed so the flow keeps going."""
        try:
            self._request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._request("answerCallbackQuery", payload)

    def get_file(self, file_id: str) -> dict[str, Any]:
        data = self._request("getFile", {"file_id": file_id})
        result = data.get("result")
        if not data.get("ok") or not isinstance(result, dict) or not result.get("file_path"):
            raise RuntimeError("Telegram không trả về đường dẫn tệp")
        return result

    def download_file(self, file_id: str, destination: Path, max_bytes: int) -> Path:
        file_info = self.get_file(file_id)
        file_path = str(file_info["file_path"]).lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")

        try:
            with urllib.request.urlopen(
                self.file_base_url + "/" + file_path,
                timeout=30,
            ) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise ValueError("Ảnh vượt quá dung lượng cho phép")

                total = 0
                with partial.open("wb") as output:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Ảnh vượt quá dung lượng cho phép")
                        output.write(chunk)

            if total == 0:
                raise ValueError("Tệp ảnh rỗng")
            partial.replace(destination)
            return destination
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + "/" + method
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=payload.get("timeout", 30) + 10) as response:
            return json.loads(response.read().decode("utf-8"))
