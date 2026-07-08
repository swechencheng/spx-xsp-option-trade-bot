"""Telegram Bot Notification Module for XSP Trading Bot.

Sends execution summaries to a Telegram chat after each scheduled run.
Credentials are loaded from `.tg_bot_secret.json` in the project root.
"""

import json
import os
from pathlib import Path

import requests


# Resolve secrets relative to this file's directory (works regardless of cwd)
_SECRETS_PATH = Path(__file__).resolve().parent / ".tg_bot_secret.json"

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Sends plain-text messages to a Telegram chat via the Bot API.

    The notifier is intentionally resilient: if the secret file is missing,
    malformed, or the Telegram API is unreachable, it logs a warning and
    returns False — it will **never** raise an exception that could crash
    the trading bot.
    """

    def __init__(self, secrets_path: str | Path | None = None):
        self._token: str | None = None
        self._chat_id: str | None = None
        self._enabled: bool = False

        path = Path(secrets_path) if secrets_path else _SECRETS_PATH
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._token = data["telegram_bot_token"]
            self._chat_id = str(data["telegram_chat_id"])
            self._enabled = True
        except FileNotFoundError:
            print(f"[TG] Secret file not found at {path}. Notifications disabled.")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[TG] Failed to parse secret file: {e}. Notifications disabled.")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_message(self, text: str) -> bool:
        """Send a plain-text message to the configured Telegram chat.

        Returns True on success, False on any failure.
        """
        if not self._enabled:
            print("[TG] Notifier is disabled — skipping message.")
            return False

        url = _TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200 and resp.json().get("ok"):
                print("[TG] ✅ Message sent successfully.")
                return True
            else:
                print(
                    f"[TG] ⚠ Telegram API returned status {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return False
        except requests.RequestException as e:
            print(f"[TG] ⚠ Failed to send message: {e}")
            return False
