from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    sell_base_url: str
    sell_internal_token: str
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    bot_data_dir: Path = Path("./data")
    telegram_max_image_bytes: int = 5 * 1024 * 1024
    telegram_max_common_images: int = 10

    @property
    def sell_product_create_url(self) -> str:
        return self.sell_base_url.rstrip("/") + "/api/noi-bo/telegram/san-pham/tao"

    @property
    def sell_product_v2_preview_url(self) -> str:
        return self.sell_base_url.rstrip("/") + "/api/noi-bo/telegram/san-pham/v2/xem-truoc"

    @property
    def sell_product_v2_create_url(self) -> str:
        return self.sell_base_url.rstrip("/") + "/api/noi-bo/telegram/san-pham/v2/tao"

    @property
    def media_dir(self) -> Path:
        return self.bot_data_dir / "media"


def load_settings() -> Settings:
    load_dotenv(Path(".env"))

    settings = Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        sell_base_url=os.getenv("SELL_BASE_URL", "http://127.0.0.1:8000").strip(),
        sell_internal_token=os.getenv("SELL_INTERNAL_TOKEN", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()
        or "claude-haiku-4-5",
        bot_data_dir=Path(os.getenv("BOT_DATA_DIR", "./data")).resolve(),
        telegram_max_image_bytes=max(
            1,
            int(os.getenv("TELEGRAM_PRODUCT_BOT_MAX_IMAGE_BYTES", str(5 * 1024 * 1024))),
        ),
        telegram_max_common_images=max(
            1,
            int(os.getenv("TELEGRAM_PRODUCT_BOT_MAX_COMMON_IMAGES", "10")),
        ),
    )

    missing = []
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.sell_internal_token:
        missing.append("SELL_INTERNAL_TOKEN")
    if missing:
        raise RuntimeError("Missing required config: " + ", ".join(missing))

    settings.bot_data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    return settings
