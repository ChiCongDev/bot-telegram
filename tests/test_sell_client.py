import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import sell_client as sell_client_module
from app.config import Settings
from app.models import ProductDraft
from app.sell_client import SellClient


def make_settings() -> Settings:
    return Settings(
        telegram_bot_token="tok",
        sell_base_url="https://sell-5jurbrak.on-forge.com",
        sell_internal_token="internal-token",
    )


class FakeJsonResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeJsonResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


class RequestJsonUserAgentTest(unittest.TestCase):
    """Bug report: Cloudflare's default Bot Fight Mode returned Error 1010
    (browser_signature_banned) for every call, because the stdlib's default
    User-Agent ("Python-urllib/3.x") is a known automated-client fingerprint."""

    def test_preview_product_sets_browser_user_agent(self):
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return FakeJsonResponse({"thanh_cong": True, "xem_truoc": {"ten": "Ao"}})

        with patch.object(
            sell_client_module.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            client = SellClient(make_settings())
            client.preview_product("123", ProductDraft(ten="Ao", maSKU="ABC"))

        # urllib.request.Request normalizes header keys with .capitalize()
        # on write (add_header) but NOT on read (get_header) — the stored
        # key is "User-agent", so the lookup must match that exact casing.
        user_agent = captured["request"].get_header("User-agent")
        self.assertIsNotNone(user_agent)
        self.assertIn("Mozilla", user_agent)
        self.assertNotIn("python", user_agent.lower())


class FakeHTTPResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body


class FakeConnection:
    instances: list["FakeConnection"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.headers: dict[str, str] = {}
        self.sent: list[bytes] = []
        FakeConnection.instances.append(self)

    def putrequest(self, method, path) -> None:
        self.method = method
        self.path = path

    def putheader(self, name, value) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def getresponse(self) -> FakeHTTPResponse:
        return FakeHTTPResponse(200, {"thanh_cong": True})

    def close(self) -> None:
        pass


class RequestMultipartUserAgentTest(unittest.TestCase):
    def test_create_product_with_images_sets_browser_user_agent(self):
        FakeConnection.instances = []
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "photo.jpg"
            image_path.write_bytes(b"fake-image-bytes")

            draft = ProductDraft(ten="Ao", maSKU="ABC")
            draft.anhChung = [str(image_path)]

            with patch.object(
                sell_client_module.http.client, "HTTPSConnection", FakeConnection
            ):
                client = SellClient(make_settings())
                client.create_product("123", draft)

        self.assertEqual(len(FakeConnection.instances), 1)
        conn = FakeConnection.instances[0]
        self.assertIn("User-Agent", conn.headers)
        self.assertIn("Mozilla", conn.headers["User-Agent"])
        self.assertNotIn("python", conn.headers["User-Agent"].lower())


if __name__ == "__main__":
    unittest.main()
