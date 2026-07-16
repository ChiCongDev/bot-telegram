from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.request
from typing import Any

from .config import Settings
from .models import ProductDraft, normalize_number

logger = logging.getLogger(__name__)


ATTRIBUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ten": {"type": "string"},
        "giaTri": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ten", "giaTri"],
    "additionalProperties": False,
}

CUSTOM_PRICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "ten": {"type": "string"},
        "gia": {"type": "number"},
    },
    "required": ["code", "ten", "gia"],
    "additionalProperties": False,
}

# A variant in the sell domain ALWAYS derives from thuocTinhs (attributes):
# `attributes` holds one value per declared attribute, in declaration order.
# Every field is required (structured-output strict mode); use "" or 0 when not specified.
VARIANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "attributes": {"type": "array", "items": {"type": "string"}},
        "maSKU": {"type": "string"},
        "barcode": {"type": "string"},
        "giaBanLe": {"type": "number"},
        "tonKho": {"type": "integer"},
    },
    "required": ["attributes", "maSKU", "barcode", "giaBanLe", "tonKho"],
    "additionalProperties": False,
}

PRODUCT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ten": {"type": "string"},
        "maSKU": {"type": "string"},
        "maVach": {"type": "string"},
        "khoiLuong": {"type": "number"},
        "donVi": {"type": "string"},
        "donViTinh": {"type": "string"},
        "giaBanLe": {"type": "number"},
        "giaBanBuon": {"type": "number"},
        "giaCongTacVien": {"type": "number"},
        "giaOrder": {"type": "number"},
        "giaOrderBuonCtv": {"type": "number"},
        "giaNhap": {"type": "number"},
        "viTri": {"type": "string"},
        "nhanHieu": {"type": "string"},
        "loaiSanPham": {"type": "string"},
        "suDungKhoHang": {"type": "boolean"},
        "tonKhoBanDau": {"type": "integer"},
        "thuocTinhs": {"type": "array", "items": ATTRIBUTE_SCHEMA},
        "variants": {"type": "array", "items": VARIANT_SCHEMA},
        "chinhSachGia": {"type": "array", "items": CUSTOM_PRICE_SCHEMA},
    },
    "required": [
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
    ],
    "additionalProperties": False,
}


def remove_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return stripped.replace("\u0111", "d").replace("\u0110", "D").lower()


SYSTEM_PROMPT = (
    "You extract a product draft for a Vietnamese retail inventory system (project 'sell'). "
    "Read the user's Vietnamese message and, when a current draft is given, apply the message "
    "as an edit on top of it. Return the FULL product reflecting the current state plus the "
    "requested change. Only set values that are clearly present or already in the draft; use "
    "empty strings for unknown text and 0 for unknown numbers. Never invent data.\n"
    "\n"
    "Price fields (all Vietnamese dong, integers):\n"
    "- giaBanLe: retail price. giaBanBuon: wholesale/collaborator price. "
    "giaCongTacVien: old collaborator price. giaOrder: order retail price. "
    "giaOrderBuonCtv: order wholesale/collaborator price. giaNhap: purchase (cost) price.\n"
    "\n"
    "Variants rule (IMPORTANT — the domain forbids anything else):\n"
    "- Variants exist ONLY when the product has attributes (thuocTinhs), e.g. Size or Color.\n"
    "- If you output any variants, you MUST also output thuocTinhs whose giaTri cover every "
    "value used by the variants; each variant.attributes lists one value per attribute, in the "
    "SAME ORDER as thuocTinhs.\n"
    "- If the user only lists variant SKUs with no attribute meaning, model them as a single "
    "attribute (e.g. ten='Phien ban') whose giaTri are those labels, and put each label in the "
    "matching variant.attributes.\n"
    "- If there are no attributes, leave both thuocTinhs and variants empty and set product-level "
    "fields only.\n"
    "- Prices and stock are product-level unless the user gives them per variant.\n"
    "\n"
    "Variant SKU rule (IMPORTANT — leaving it blank is the CORRECT, intended behavior, not a "
    "missing value):\n"
    "- The backend auto-generates a variant's SKU from the product's base maSKU plus its "
    "attribute values (e.g. base 'ADI-001' + Size=S -> 'ADI-001-S') WHENEVER variant.maSKU is "
    "left as an empty string. This is the default and should be used for every variant unless "
    "the user gives that specific variant an explicit, distinct SKU.\n"
    "- NEVER invent, guess, or copy the base SKU into a variant's maSKU just to fill the field. "
    "An invented SKU is a bug — an empty string is the correct value when unspecified.\n"
    "- Example: user gives base SKU 'ADI-001' and says 'co 2 phien ban S va M, khong noi gi them "
    "ve sku rieng' -> both variants must have maSKU: '' (let the backend generate 'ADI-001-S' and "
    "'ADI-001-M').\n"
    "- Example: user says 'size S de trong sku' (explicitly asks to leave it blank) -> same "
    "result, maSKU: ''.\n"
    "- Example: user says 'size S la sku ADI-001-DEN' (gives an explicit SKU for that one "
    "variant) -> that variant's maSKU: 'ADI-001-DEN', other variants with no explicit SKU still "
    "get ''.\n"
    "\n"
    "Return custom price policies (chinhSachGia) only when a policy name/code AND its price are "
    "explicit."
)


def parse_product_message(
    text: str,
    settings: Settings,
    current_draft: ProductDraft | None = None,
) -> ProductDraft:
    if settings.anthropic_api_key and settings.anthropic_model:
        try:
            draft = parse_with_claude(text, settings, current_draft)
            if draft.ten or draft.maSKU:
                logger.info("Parsed product message with Claude (%s)", settings.anthropic_model)
                return draft
            logger.info("Claude returned no ten/maSKU; falling back to rule parser")
        except Exception as exc:
            logger.warning("Claude parse failed, falling back to rule parser: %s", exc)
    return parse_with_rules(text)


def parse_with_claude(
    text: str,
    settings: Settings,
    current_draft: ProductDraft | None = None,
) -> ProductDraft:
    user_content = text
    if current_draft is not None:
        context = json.dumps(current_draft.to_product_payload(), ensure_ascii=False)
        user_content = (
            "Ban nhap hien tai (JSON):\n" + context + "\n\nTin nhan moi:\n" + text
        )

    body = {
        "model": settings.anthropic_model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": PRODUCT_SCHEMA,
            }
        },
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    parsed = json.loads(extract_claude_response_text(data))
    parsed["source_text"] = text
    return ProductDraft.from_dict(parsed)


def extract_claude_response_text(data: dict[str, Any]) -> str:
    for block in data.get("content", []):
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    raise ValueError("Claude response does not contain text content")


def parse_with_rules(text: str) -> ProductDraft:
    base_text = "\n".join(
        line
        for line in text.splitlines()
        if not remove_accents(line).lstrip().startswith("phien ban")
    )
    plain = remove_accents(base_text)
    draft = ProductDraft(source_text=text)

    draft.maSKU = extract_label(base_text, plain, [r"ma\s*sku", r"sku"])
    draft.maVach = extract_label(base_text, plain, [r"barcode", r"ma\s*vach"])
    draft.nhanHieu = extract_label(
        base_text,
        plain,
        [r"nhan\s*hieu", r"hang", r"brand"],
    )
    draft.loaiSanPham = extract_label(
        base_text,
        plain,
        [r"loai\s*san\s*pham", r"loai"],
    )
    draft.viTri = extract_label(base_text, plain, [r"vi\s*tri", r"ke"])
    draft.donViTinh = extract_label(
        base_text,
        plain,
        [r"don\s*vi\s*tinh", r"dvt"],
    )

    draft.giaBanLe = extract_money(
        plain,
        [r"gia\s*ban\s*le", r"gia\s*ban", r"gia"],
    )
    draft.giaNhap = extract_money(plain, [r"gia\s*nhap"])
    # The "(?:\s*-?\s*ctv)?" tail lets the label match the exact way these two
    # prices are DISPLAYED back to the user ("Giá bán buôn - CTV", "Giá order
    # buôn - CTV") — people copy that label verbatim, so the "- ctv" between the
    # label and the number must be skipped instead of blocking the value (the
    # old pattern only allowed one separator, so "... buon - ctv: 4555" read 0).
    # The tail is optional, so the bare "gia ban buon: 4555" form still works.
    draft.giaBanBuon = extract_money(
        plain,
        [r"gia\s*ban\s*buon(?:\s*-?\s*ctv)?", r"gia\s*buon"],
    )
    draft.giaCongTacVien = extract_money(
        plain,
        [r"gia\s*cong\s*tac\s*vien", r"gia\s*ctv\s*cu"],
    )
    draft.giaOrderBuonCtv = extract_money(
        plain,
        [r"gia\s*order\s*buon(?:\s*-?\s*ctv)?", r"gia\s*order\s*ctv"],
    )
    draft.giaOrder = extract_money(
        plain,
        [r"gia\s*order\s*le", r"gia\s*order"],
    )

    stock = extract_number(plain, [r"ton\s*kho", r"ton", r"so\s*luong"])
    draft.tonKhoBanDau = int(stock) if stock is not None else 0
    draft.suDungKhoHang = draft.tonKhoBanDau > 0

    weight_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*(kg|g)\b", plain)
    if weight_match:
        draft.khoiLuong = normalize_number(weight_match.group(1))
        draft.donVi = weight_match.group(2)
    else:
        # Label form with no unit attached, e.g. "khoi luong san pham la 99"
        # (donVi keeps ProductDraft's default "g" since no unit was given).
        weight_value = extract_number(
            plain, [r"khoi\s*luong\s*san\s*pham", r"khoi\s*luong"]
        )
        if weight_value is not None and weight_value > 0:
            draft.khoiLuong = weight_value

    draft.ten = extract_name(base_text, plain)
    draft.thuocTinhs = extract_attributes(base_text)
    draft.variants, inferred_attributes = extract_variants(text, draft.thuocTinhs)
    if not draft.thuocTinhs and inferred_attributes:
        draft.thuocTinhs = inferred_attributes
    if not draft.variants:
        bare_variants, bare_attributes = extract_bare_sku_variants(
            text, remove_accents(text)
        )
        if bare_variants:
            draft.variants = bare_variants
            if not draft.thuocTinhs:
                draft.thuocTinhs = bare_attributes
    draft.chinhSachGia = extract_custom_prices(base_text)
    draft.clean()
    return draft


def extract_label(original: str, plain: str, labels: list[str]) -> str:
    for label in labels:
        pattern = (
            r"\b"
            + label
            + r"\b\s*(?:(?:la)\s*)?[:=\-]?\s*([^,;\n|]+)"
        )
        match = re.search(pattern, plain)
        if not match:
            continue
        value_plain = match.group(1).strip()
        start = match.start(1)
        end = start + len(value_plain)
        return original[start:end].strip(" :,-'\"")
    return ""


# A separator (., comma, space) only belongs to the number if MORE DIGITS
# follow it — otherwise it's dangling (e.g. a field separator comma) and must
# not be swallowed. Without this, "gia nhap 990, khoi luong 80kg" let the
# greedy old pattern "[0-9][0-9., ]*" eat straight through ", " up to the "k"
# of "khoi" and misread it as the x1000 shorthand suffix -> 990 became
# 990000 (or x1,000,000 for a word starting with "tr"). Left UNCLOSED here —
# every call site appends its own trailing group (e.g. "(?:k|tr)?)" or ")").
_NUMBER_PATTERN = r"([0-9]+(?:[.,\s][0-9]+)*"


def extract_money(plain: str, labels: list[str]) -> float:
    for label in labels:
        pattern = (
            r"\b"
            + label
            + r"\b\s*(?:(?:la)\s*)?[:=\-]?\s*"
            + _NUMBER_PATTERN
            + r"(?:k|tr)?)"
        )
        match = re.search(pattern, plain)
        if match:
            return normalize_number(match.group(1))
    return 0.0


def extract_number(plain: str, labels: list[str]) -> float | None:
    for label in labels:
        pattern = (
            r"\b"
            + label
            + r"\b\s*(?:(?:la)\s*)?[:=\-]?\s*"
            + _NUMBER_PATTERN
            + r")"
        )
        match = re.search(pattern, plain)
        if match:
            return normalize_number(match.group(1))
    return None


def extract_name(original: str, plain: str) -> str:
    labeled = extract_label(
        original,
        plain,
        [r"ten\s*san\s*pham", r"ten\s*sp", r"ten"],
    )
    if labeled:
        return labeled

    first_part = re.split(r"[,;\n]", original.strip(), maxsplit=1)[0].strip()
    first_part_plain = remove_accents(first_part)
    # A message whose first clause is really another field's label (not a
    # name) must not have that whole clause swallowed as the product name —
    # e.g. "khoi luong san pham la 99" was wrongly becoming the literal ten.
    # Markers kept as multi-word/unambiguous phrases to avoid rejecting a
    # real name that happens to contain a short, common word.
    field_markers = [
        "sku", "barcode", "gia ", "gia:",
        "khoi luong", "vi tri", "don vi tinh", "nhan hieu",
        "loai san pham", "ton kho", "so luong",
    ]
    if any(marker in first_part_plain for marker in field_markers):
        return ""
    return first_part[:255]


# Prefixes that mark the start of a NEW field/clause (accent-stripped, lowercase).
# Used so an inline attribute list like "size 34, 43, 54" stops collecting values
# before the next field ("..., co gia ban le ...").
_NEW_FIELD_PREFIXES = (
    "size", "kich thuoc", "mau sac", "mau", "chat lieu",
    "ma sku", "sku", "ma vach", "barcode", "ten", "nhan hieu",
    "hang", "brand", "loai", "ton", "so luong", "vi tri", "ke",
    "don vi", "dvt", "khoi luong", "gia", "chinh sach",
    "phien ban", "thuoc tinh", "co", "va", "voi",
)


def _is_new_field_segment(plain_part: str) -> bool:
    plain_part = plain_part.strip()
    # A label can be followed by a space ("size 21") OR a separator glued
    # directly to it with no space ("size: 21", "loai: nnn") — both must
    # count as a new field, otherwise the glued form gets swallowed as part
    # of the PREVIOUS attribute's value list.
    return any(
        plain_part == prefix
        or plain_part.startswith(prefix + " ")
        or plain_part.startswith(prefix + ":")
        or plain_part.startswith(prefix + "=")
        or plain_part.startswith(prefix + "-")
        for prefix in _NEW_FIELD_PREFIXES
    )


def extract_attributes(text: str) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    seen: set[str] = set()

    for original_line in text.splitlines():
        # Lỗi 1: strip the ORIGINAL too, so plain/original offsets stay aligned
        # (a leading space used to shift every slice by one -> "size" became "siz").
        stripped_line = original_line.strip()
        plain_line = remove_accents(stripped_line)
        explicit = re.match(r"thuoc\s*tinh\s+([^:]+)\s*:\s*(.+)$", plain_line)
        if explicit:
            name = stripped_line[explicit.start(1):explicit.end(1)].strip()
            raw_values = stripped_line[explicit.start(2):explicit.end(2)].strip()
            _append_attribute(attributes, seen, name, raw_values)
            continue

        parts = re.split(r"[,;]", original_line)
        index = 0
        while index < len(parts):
            part = parts[index].strip()
            plain_part = remove_accents(part)
            # "mau sac" must be tried BEFORE "mau" — regex alternation picks the
            # first alternative that matches, so the shorter "mau" would always
            # win first and leave "sac" stuck at the front of the values.
            compact = re.match(
                r"(size|kich\s*thuoc|mau\s*sac|mau|chat\s*lieu)"
                r"\s*(?:(?:la)\s*)?[:=\-]?\s*(.+)$",
                plain_part,
            )
            if not compact:
                index += 1
                continue

            name = part[compact.start(1):compact.end(1)].strip()
            values = [part[compact.start(2):compact.end(2)].strip()]

            # Lỗi 2: keep pulling in the following comma-separated values
            # ("size 34, 43, 54") until a segment starts a new field/clause.
            look_ahead = index + 1
            while look_ahead < len(parts):
                next_part = parts[look_ahead].strip()
                if not next_part or _is_new_field_segment(remove_accents(next_part)):
                    break
                values.append(next_part)
                look_ahead += 1

            _append_attribute(attributes, seen, name, ", ".join(values))
            index = look_ahead

    return attributes


def _append_attribute(
    attributes: list[dict[str, Any]],
    seen: set[str],
    name: str,
    raw_values: str,
) -> None:
    key = remove_accents(name).strip()
    if not key or key in seen:
        return

    values = [
        value.strip(" '\"")
        for value in re.split(r"[|,/]", raw_values)
        if value.strip(" '\"")
    ]
    if len(values) == 1 and key in {"size", "kich thuoc", "mau", "mau sac"}:
        values = [
            value.strip(" '\"")
            for value in re.split(r"\s+", raw_values)
            if value.strip(" '\"")
        ]
    values = list(dict.fromkeys(values))
    if values:
        seen.add(key)
        attributes.append({"ten": name, "giaTri": values})


def extract_custom_prices(text: str) -> list[dict[str, Any]]:
    plain = remove_accents(text)
    result: list[dict[str, Any]] = []
    pattern = re.compile(
        r"gia\s*chinh\s*sach\s+([^:=,;\n]+)\s*(?:(?:la)\s*)?[:=\-]\s*"
        + _NUMBER_PATTERN
        + r"(?:k|tr)?)"
    )
    for match in pattern.finditer(plain):
        name = text[match.start(1):match.end(1)].strip(" '\"")
        price = normalize_number(match.group(2))
        if name:
            result.append({"ten": name, "gia": price})
    return result


def extract_variants(
    text: str,
    attributes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    variants: list[dict[str, Any]] = []
    inferred: dict[str, list[str]] = {}

    for original_line in text.splitlines():
        plain_line = remove_accents(original_line).strip()
        match = re.match(r"phien\s*ban\s*:\s*(.+)$", plain_line)
        if not match:
            continue

        content = original_line[match.start(1):match.end(1)].strip()
        segments = [segment.strip() for segment in content.split("|") if segment.strip()]
        if not segments:
            continue

        descriptor = segments[0]
        assignments: list[tuple[str, str]] = []
        for item in re.split(r"[,/]", descriptor):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip()
            value = value.strip(" '\"")
            if name and value:
                assignments.append((name, value))
                inferred.setdefault(name, [])
                if value not in inferred[name]:
                    inferred[name].append(value)

        if assignments:
            assignment_map = {
                remove_accents(name): value for name, value in assignments
            }
            values = (
                [
                    assignment_map.get(remove_accents(attribute["ten"]), "")
                    for attribute in attributes
                ]
                if attributes
                else [value for _, value in assignments]
            )
        else:
            values = [
                value.strip(" '\"")
                for value in re.split(r"[/,]", descriptor)
                if value.strip(" '\"")
            ]

        variant: dict[str, Any] = {"selected": True, "attributes": values}
        details = " | ".join(segments[1:])
        details_plain = remove_accents(details)

        string_fields = {
            "name": [r"ten"],
            "maSKU": [r"ma\s*sku", r"sku"],
            "barcode": [r"barcode", r"ma\s*vach"],
            "viTri": [r"vi\s*tri", r"ke"],
        }
        for field, labels in string_fields.items():
            value = extract_label(details, details_plain, labels)
            if value:
                variant[field] = value

        numeric_fields = {
            "giaBanLe": [r"gia\s*ban\s*le", r"gia\s*ban"],
            # Same "- ctv" display-label tolerance as the product-level parser
            # above, so a per-variant line can also carry "... buon - ctv: N".
            "giaBanBuon": [r"gia\s*ban\s*buon(?:\s*-?\s*ctv)?", r"gia\s*buon"],
            "giaCongTacVien": [
                r"gia\s*cong\s*tac\s*vien",
                r"gia\s*ctv\s*cu",
            ],
            "giaOrderBuonCtv": [
                r"gia\s*order\s*buon(?:\s*-?\s*ctv)?",
                r"gia\s*order\s*ctv",
            ],
            "giaOrder": [r"gia\s*order\s*le", r"gia\s*order"],
            "giaNhap": [r"gia\s*nhap"],
        }
        for field, labels in numeric_fields.items():
            value = extract_money(details_plain, labels)
            if value > 0:
                variant[field] = value

        stock = extract_number(
            details_plain,
            [r"ton\s*kho", r"ton", r"so\s*luong"],
        )
        if stock is not None:
            variant["tonKho"] = max(0, int(stock))

        weight_match = re.search(
            r"(\d+(?:[\.,]\d+)?)\s*(kg|g)\b",
            details_plain,
        )
        if weight_match:
            variant["khoiLuong"] = normalize_number(weight_match.group(1))
            variant["donVi"] = weight_match.group(2)

        custom_prices = extract_custom_prices(details)
        if custom_prices:
            variant["chinhSachGia"] = custom_prices
        variants.append(variant)

    inferred_attributes = [
        {"ten": name, "giaTri": values}
        for name, values in inferred.items()
        if values
    ]
    return variants, inferred_attributes


def extract_bare_sku_variants(
    original: str,
    plain: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fallback for free-form phrasing like 'cac phien ban co sku la: A, B, C' —
    a bare comma list of SKUs with no attribute keyword, unlike the structured
    'Phien ban: attr=value | ...' syntax handled by extract_variants().

    Each token becomes its own variant (maSKU = that token) grouped under a
    synthetic 'Phien ban' attribute, matching the sell domain rule that
    variants only exist alongside thuocTinhs. Stops before an unrelated
    trailing clause introduced by "va" (e.g. "..., va co anh chung la").

    Known limits (rule-based, not real language understanding): requires the
    word "phien ban" to appear before "sku" in the sentence, and a single
    bare token is treated as too ambiguous to count as a variant list.
    """
    match = re.search(
        r"phien\s*ban[^:]{0,40}sku[^:]{0,20}(?:la\s*)?[:=]\s*"
        r"([a-z0-9][\w,\s/|-]*?)(?=(?:,?\s+va\s+)|[.\n]|$)",
        plain,
    )
    if not match:
        return [], []

    start, end = match.start(1), match.end(1)
    raw_list = original[start:end]
    tokens = [
        token.strip(" '\"")
        for token in re.split(r"[,/|]", raw_list)
        if token.strip(" '\"")
    ]
    tokens = list(dict.fromkeys(tokens))
    if len(tokens) < 2:
        return [], []

    attribute = {"ten": "Phiên bản", "giaTri": tokens}
    variants = [
        {"selected": True, "attributes": [token], "maSKU": token}
        for token in tokens
    ]
    return variants, [attribute]
