# Telegram Product Bot

Bot nay la lop giao dien Telegram cho viec tao san pham. Bot khong ghi truc tiep database va khong mo phong form web. Moi lenh tao san pham deu tao ban nhap, cho nguoi dung xac nhan, roi moi goi endpoint noi bo cua Laravel `sell`.

## Cach chay

1. Cai Python 3.11+ tren server.
2. Copy `.env.example` thanh `.env` va dien gia tri:
   - `TELEGRAM_BOT_TOKEN`: token bot tu BotFather.
   - `SELL_BASE_URL`: domain noi bo cua project sell, vi du `https://bongtom.store`.
   - `SELL_INTERNAL_TOKEN`: trung voi `TELEGRAM_PRODUCT_BOT_TOKEN` trong `.env` cua `sell`.
   - `ANTHROPIC_API_KEY` va `ANTHROPIC_MODEL`: tuy chon, dung Claude de hieu ngon ngu tu nhien tot hon. Neu de trong, bot van chay binh thuong bang bo phan tich quy tac (rule-based) co san.
3. Trong `sell/.env`, cau hinh:
   - `TELEGRAM_PRODUCT_BOT_TOKEN`: token noi bo rieng cho bot.
   - `TELEGRAM_PRODUCT_BOT_USERS`: map Telegram user sang nhan vien, vi du `123456789:1`.
4. Chay:

```bash
python -m app.main
```

## Luong an toan

1. Nguoi dung nhan tin mo ta san pham.
2. Bot phan tich thanh ban nhap.
3. Bot hien tom tat va yeu cau bam xac nhan.
4. Sau khi xac nhan, bot goi Laravel endpoint noi bo.
5. Laravel kiem tra token, mapping Telegram user, quyen nhan vien, SKU/barcode trung, idempotency key, roi moi goi logic tao san pham hien co.

Pham vi hien tai: tao san pham thuong, khong anh, khong bien the. Mo rong anh/bien the nen lam sau khi da co endpoint rieng va test rieng.

## Test

Khi server da co Python:

```bash
python -m unittest discover -s tests
```

## Chay nen tren Forge

Co the tao daemon/Supervisor rieng cho thu muc `bot-telegram` voi command:

```bash
python -m app.main
```

Nen chay bot bang user rieng hoac user deploy, va chi cap quyen doc `.env` cho user do.
