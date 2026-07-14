import json
import unittest
from unittest.mock import patch

from app import nlp_parser
from app.config import Settings
from app.models import ProductDraft
from app.nlp_parser import (
    PRODUCT_SCHEMA,
    parse_product_message,
    parse_with_claude,
)


def make_settings(api_key: str = "sk-ant-test", model: str = "claude-haiku-4-5") -> Settings:
    return Settings(
        telegram_bot_token="tok",
        sell_base_url="http://127.0.0.1:8000",
        sell_internal_token="internal",
        anthropic_api_key=api_key,
        anthropic_model=model,
    )


class FakeResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, product_payload: dict):
        outer = {"content": [{"type": "text", "text": json.dumps(product_payload)}]}
        self._body = json.dumps(outer).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


def capture_urlopen(captured: dict, product_payload: dict):
    def _urlopen(request, timeout=None):
        captured["request"] = request
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(product_payload)

    return _urlopen


def assert_strict(schema: dict) -> None:
    """Structured-output strict mode: every object lists all properties in required + no extras."""
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        assert schema.get("additionalProperties") is False, "additionalProperties must be False"
        assert set(schema.get("required", [])) == set(props), (
            "required must list exactly the properties"
        )
        for child in props.values():
            assert_strict(child)
    if schema.get("type") == "array":
        assert_strict(schema["items"])


class SchemaTest(unittest.TestCase):
    def test_product_schema_is_strict_compliant(self):
        assert_strict(PRODUCT_SCHEMA)

    def test_variants_present_in_schema(self):
        self.assertIn("variants", PRODUCT_SCHEMA["properties"])
        self.assertIn("variants", PRODUCT_SCHEMA["required"])
        variant_item = PRODUCT_SCHEMA["properties"]["variants"]["items"]
        self.assertIn("attributes", variant_item["properties"])


class ClaudeRequestTest(unittest.TestCase):
    def test_request_shape_and_headers(self):
        captured: dict = {}
        payload = {"ten": "Ao", "maSKU": "ABC"}
        with patch.object(
            nlp_parser.urllib.request, "urlopen", side_effect=capture_urlopen(captured, payload)
        ):
            draft = parse_with_claude("Ten Ao, SKU ABC", make_settings())

        self.assertEqual(draft.ten, "Ao")
        self.assertEqual(draft.maSKU, "ABC")
        body = captured["body"]
        self.assertEqual(body["model"], "claude-haiku-4-5")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertIn("variants", body["output_config"]["format"]["schema"]["properties"])
        self.assertEqual(body["system"], nlp_parser.SYSTEM_PROMPT)
        # No current draft → the user content is exactly the message.
        user_msg = body["messages"][0]["content"]
        self.assertNotIn("Ban nhap hien tai", user_msg)
        self.assertIn("Ten Ao, SKU ABC", user_msg)

        request = captured["request"]
        self.assertEqual(request.get_header("X-api-key"), "sk-ant-test")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(request.get_full_url(), "https://api.anthropic.com/v1/messages")

    def test_request_includes_current_draft_context(self):
        captured: dict = {}
        payload = {"ten": "Ao Adidas", "maSKU": "ADI-1", "giaBanLe": 5000}
        current = ProductDraft(ten="Ao Adidas", maSKU="ADI-1", giaBanLe=120000)
        with patch.object(
            nlp_parser.urllib.request, "urlopen", side_effect=capture_urlopen(captured, payload)
        ):
            parse_with_claude("doi gia ban le thanh 5000", make_settings(), current)

        user_msg = captured["body"]["messages"][0]["content"]
        self.assertIn("Ban nhap hien tai", user_msg)
        self.assertIn("ADI-1", user_msg)          # draft context present
        self.assertIn("doi gia ban le thanh 5000", user_msg)  # new message present

    def test_variants_from_response_populate_draft(self):
        captured: dict = {}
        payload = {
            "ten": "Ao Thun",
            "maSKU": "AT",
            "thuocTinhs": [{"ten": "Size", "giaTri": ["S", "M"]}],
            "variants": [
                {"attributes": ["S"], "maSKU": "AT-S", "barcode": "", "giaBanLe": 0, "tonKho": 3},
                {"attributes": ["M"], "maSKU": "AT-M", "barcode": "", "giaBanLe": 0, "tonKho": 4},
            ],
        }
        with patch.object(
            nlp_parser.urllib.request, "urlopen", side_effect=capture_urlopen(captured, payload)
        ):
            draft = parse_with_claude("Ao thun size S M", make_settings())

        self.assertEqual(len(draft.variants), 2)
        self.assertEqual(draft.variants[0]["maSKU"], "AT-S")
        self.assertEqual(draft.variants[0]["attributes"], ["S"])
        self.assertEqual(draft.thuocTinhs, [{"ten": "Size", "giaTri": ["S", "M"]}])


class DispatchTest(unittest.TestCase):
    def test_no_api_key_uses_rule_parser(self):
        # Empty key must never hit the network.
        with patch.object(nlp_parser.urllib.request, "urlopen") as mocked:
            draft = parse_product_message(
                "Ten Ao Test, SKU ABC-1, gia ban 100000", make_settings(api_key="")
            )
        mocked.assert_not_called()
        self.assertEqual(draft.ten, "Ao Test")
        self.assertEqual(draft.maSKU, "ABC-1")

    def test_claude_failure_falls_back_and_logs_warning(self):
        with patch.object(nlp_parser, "parse_with_claude", side_effect=RuntimeError("boom")):
            with self.assertLogs("app.nlp_parser", level="WARNING") as logs:
                draft = parse_product_message(
                    "Ten Ao Test, SKU ABC-1, gia ban 100000", make_settings()
                )
        self.assertTrue(any("falling back" in line for line in logs.output))
        self.assertEqual(draft.ten, "Ao Test")     # came from rule parser
        self.assertEqual(draft.maSKU, "ABC-1")

    def test_current_draft_flows_through_dispatch(self):
        captured: dict = {}
        payload = {"ten": "Ao", "maSKU": "ABC", "giaBanLe": 5000}
        current = ProductDraft(ten="Ao", maSKU="ABC", giaBanLe=1000)
        with patch.object(
            nlp_parser.urllib.request, "urlopen", side_effect=capture_urlopen(captured, payload)
        ):
            parse_product_message("doi gia 5000", make_settings(), current)
        self.assertIn("Ban nhap hien tai", captured["body"]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
