"""Telegram notification helper."""

import logging
import requests

logger = logging.getLogger(__name__)


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
