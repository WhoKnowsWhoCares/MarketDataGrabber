"""Проверка бота: getMe + попытка отправки админу.
Если 'chat not found' — владельцу нужно написать боту /start (это не ошибка токена).
Запуск: .venv/bin/python scripts/smoke_telegram.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from mdgrabber.config import Settings


def main() -> int:
    s = Settings.load()
    with httpx.Client(verify=s.tls_verify, proxy=s.telegram_proxy, timeout=30.0) as http:
        me = http.get(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe").json()
        print("getMe:", me.get("result", {}).get("username"), "| ok:", me.get("ok"))
        r = http.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": s.admin_chat_id,
                  "text": "MarketDataGrabber: smoke-тест уведомлений ✅"},
        )
        d = r.json()
        if d.get("ok"):
            print("sendMessage: OK — сообщение доставлено админу")
            return 0
        print(f"sendMessage: {d.get('description')} — попросите владельца написать боту /start")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
