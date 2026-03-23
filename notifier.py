"""Telegram notification helper."""

import logging
import requests

logger = logging.getLogger(__name__)

# Track the last processed update to avoid re-processing
_last_update_id = 0


def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram bot. Returns True on success."""
    if not bot_token or not chat_id:
        logger.debug("Telegram not configured, skipping notification")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=15)
        if resp.status_code == 200:
            logger.info("Telegram message sent")
            return True
        logger.warning("Telegram API error: %s", resp.text)
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
    return False


def check_telegram_commands(bot_token: str, chat_id: str) -> "str | None":
    """Poll Telegram for new commands from the user.

    Returns the command string (e.g. '/stop', '/restart', '/status')
    if a new command was received, or None.
    Only processes messages from the configured chat_id.
    """
    global _last_update_id

    if not bot_token or not chat_id:
        return None

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        resp = requests.get(url, params={
            "offset": _last_update_id + 1,
            "timeout": 0,  # non-blocking
            "allowed_updates": '["message"]',
        }, timeout=5)
        if resp.status_code != 200:
            return None

        data = resp.json()
        if not data.get("ok"):
            return None

        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            if update_id > _last_update_id:
                _last_update_id = update_id

            msg = update.get("message", {})
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip().lower()

            if msg_chat_id == str(chat_id) and text.startswith("/"):
                logger.info("Received Telegram command: %s", text)
                return text

    except Exception as e:
        logger.debug("Telegram poll error: %s", e)

    return None
