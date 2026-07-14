from __future__ import annotations

import logging

from .bot import ProductBot
from .config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    ProductBot(settings).run_forever()


if __name__ == "__main__":
    main()
