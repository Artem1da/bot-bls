"""hCaptcha solver using 2captcha API."""

import time
import requests
import logging

logger = logging.getLogger(__name__)

TWOCAPTCHA_IN = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"


def solve_hcaptcha(api_key: str, sitekey: str, page_url: str, max_attempts: int = 30) -> str | None:
    """Submit hCaptcha to 2captcha and poll for solution.

    Returns the captcha token string, or None on failure.
    """
    # Submit task
    resp = requests.get(TWOCAPTCHA_IN, params={
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": page_url,
        "json": 1,
    }, timeout=30)
    data = resp.json()
    if data.get("status") != 1:
        logger.error("2captcha submit error: %s", data)
        return None

    request_id = data["request"]
    logger.info("CAPTCHA submitted, request_id=%s", request_id)

    # Poll for result
    time.sleep(30)  # hCaptcha typically takes 30-60s
    for attempt in range(max_attempts):
        resp = requests.get(TWOCAPTCHA_RES, params={
            "key": api_key,
            "action": "get",
            "id": request_id,
            "json": 1,
        }, timeout=30)
        data = resp.json()

        if data.get("status") == 1:
            logger.info("CAPTCHA solved successfully")
            return data["request"]

        if data.get("request") != "CAPCHA_NOT_READY":
            logger.error("2captcha error: %s", data)
            return None

        logger.debug("CAPTCHA not ready, attempt %d/%d", attempt + 1, max_attempts)
        time.sleep(10)

    logger.error("CAPTCHA solving timed out after %d attempts", max_attempts)
    return None
