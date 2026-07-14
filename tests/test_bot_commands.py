import unittest
from unittest.mock import Mock

from app.bot import ProductBot


class BotCommandTest(unittest.TestCase):
    def setUp(self):
        self.bot = ProductBot.__new__(ProductBot)
        self.bot.telegram = Mock()
        self.bot.sell = Mock()
        self.bot.drafts = Mock()

    def test_id_command_returns_telegram_user_id(self):
        self.bot.handle_update(
            {
                "message": {
                    "text": "/id",
                    "chat": {"id": 8718147635},
                    "from": {"id": 8718147635},
                }
            }
        )

        self.bot.telegram.send_message.assert_called_once_with(
            8718147635,
            "Telegram User ID của bạn: 8718147635",
        )
        self.bot.drafts.save.assert_not_called()
        self.bot.sell.create_product.assert_not_called()

    def test_id_command_accepts_telegram_bot_suffix(self):
        self.bot.handle_update(
            {
                "message": {
                    "text": "/id@bongtom_product_bot",
                    "chat": {"id": 1001},
                    "from": {"id": 2002},
                }
            }
        )

        self.bot.telegram.send_message.assert_called_once_with(
            1001,
            "Telegram User ID của bạn: 2002",
        )

    def test_unknown_command_replies_with_guidance(self):
        self.bot.handle_update(
            {
                "message": {
                    "text": "/xemm",
                    "chat": {"id": 1001},
                    "from": {"id": 2002},
                }
            }
        )

        self.bot.telegram.send_message.assert_called_once_with(
            1001,
            "Lệnh không hợp lệ. Dùng /start để xem hướng dẫn.",
        )
        self.bot.drafts.get.assert_not_called()

    def test_single_slash_replies_with_guidance(self):
        self.bot.handle_update(
            {
                "message": {
                    "text": "/",
                    "chat": {"id": 1001},
                    "from": {"id": 2002},
                }
            }
        )

        self.bot.telegram.send_message.assert_called_once_with(
            1001,
            "Lệnh không hợp lệ. Dùng /start để xem hướng dẫn.",
        )


if __name__ == "__main__":
    unittest.main()
