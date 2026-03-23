"""CAPTCHA solvers using rucaptcha.com API.

Supports:
  - hCaptcha (token-based)
  - BLS number-grid CAPTCHA (image-based, 3×3 grid)
"""

import base64
import re
import time
import requests
import logging

logger = logging.getLogger(__name__)

RUCAPTCHA_IN = "https://rucaptcha.com/in.php"
RUCAPTCHA_RES = "https://rucaptcha.com/res.php"


def _poll_result(api_key: str, request_id: str, max_attempts: int = 30,
                 first_delay: int = 15, poll_interval: int = 5) -> str | None:
    """Poll rucaptcha for a solved result."""
    time.sleep(first_delay)
    for attempt in range(max_attempts):
        resp = requests.get(RUCAPTCHA_RES, params={
            "key": api_key,
            "action": "get",
            "id": request_id,
            "json": 1,
        }, timeout=30)
        data = resp.json()

        if data.get("status") == 1:
            logger.info("CAPTCHA solved successfully: %s", data["request"])
            return data["request"]

        if data.get("request") != "CAPCHA_NOT_READY":
            logger.error("rucaptcha error: %s", data)
            return None

        logger.debug("CAPTCHA not ready, attempt %d/%d", attempt + 1, max_attempts)
        time.sleep(poll_interval)

    logger.error("CAPTCHA solving timed out after %d attempts", max_attempts)
    return None


def solve_hcaptcha(api_key: str, sitekey: str, page_url: str, max_attempts: int = 30) -> str | None:
    """Submit hCaptcha to rucaptcha and poll for solution.

    Returns the captcha token string, or None on failure.
    """
    resp = requests.get(RUCAPTCHA_IN, params={
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": page_url,
        "json": 1,
    }, timeout=30)
    data = resp.json()
    if data.get("status") != 1:
        logger.error("rucaptcha submit error: %s", data)
        return None

    request_id = data["request"]
    logger.info("hCaptcha submitted to rucaptcha, request_id=%s", request_id)
    return _poll_result(api_key, request_id, max_attempts, first_delay=30, poll_interval=10)


def solve_grid_captcha(api_key: str, image_base64: str, instruction: str,
                       rows: int = 3, cols: int = 3,
                       max_attempts: int = 30) -> list[int] | None:
    """Solve a grid/canvas CAPTCHA via rucaptcha.

    Sends a base64-encoded screenshot of the grid + the instruction text.
    Returns a list of 1-indexed cell numbers to click, or None on failure.

    Example return: [2, 6, 7] means click cells 2, 6, 7 (row-major, 1-indexed).
    """
    resp = requests.post(RUCAPTCHA_IN, data={
        "key": api_key,
        "method": "base64",
        "body": image_base64,
        "textinstructions": instruction,
        "recaptcharows": rows,
        "recaptchacols": cols,
        "json": 1,
    }, timeout=30)
    data = resp.json()
    if data.get("status") != 1:
        logger.error("rucaptcha grid submit error: %s", data)
        return None

    request_id = data["request"]
    logger.info("Grid CAPTCHA submitted to rucaptcha, request_id=%s", request_id)

    result = _poll_result(api_key, request_id, max_attempts, first_delay=15, poll_interval=5)
    if not result:
        return None

    # Parse response like "click:2/6/7" or "click:2/6/7/8"
    cells = []
    click_part = result.replace("click:", "").strip()
    for part in click_part.split("/"):
        part = part.strip()
        if part.isdigit():
            cells.append(int(part))
    if not cells:
        logger.error("Could not parse grid CAPTCHA response: %s", result)
        return None

    logger.info("Grid CAPTCHA cells to click: %s", cells)
    return cells


def solve_coord_captcha(api_key: str, image_base64: str, instruction: str,
                        max_attempts: int = 30) -> list[tuple[int, int]] | None:
    """Solve a coordinate-click CAPTCHA via rucaptcha.

    Sends a base64-encoded screenshot + instruction text.
    Uses coordinatescaptcha method — rucaptcha returns coordinates to click.

    Returns list of (x, y) coordinates relative to the image, or None on failure.
    """
    resp = requests.post(RUCAPTCHA_IN, data={
        "key": api_key,
        "method": "base64",
        "body": image_base64,
        "coordinatescaptcha": 1,
        "textinstructions": instruction,
        "json": 1,
    }, timeout=30)
    data = resp.json()
    if data.get("status") != 1:
        logger.error("rucaptcha coord submit error: %s", data)
        return None

    request_id = data["request"]
    logger.info("Coord CAPTCHA submitted to rucaptcha, request_id=%s", request_id)

    result = _poll_result(api_key, request_id, max_attempts, first_delay=15, poll_interval=5)
    if not result:
        return None

    # Parse response like "coordinates:x=112,y=87|x=283,y=87|x=112,y=216"
    logger.info("Coord CAPTCHA raw response: %s", result)
    coords = []
    raw = result.replace("coordinates:", "").strip()
    for point in raw.split("|"):
        point = point.strip()
        mx = re.search(r"x=(\d+)", point)
        my = re.search(r"y=(\d+)", point)
        if mx and my:
            coords.append((int(mx.group(1)), int(my.group(1))))

    if not coords:
        logger.error("Could not parse coord CAPTCHA response: %s", result)
        return None

    logger.info("Coord CAPTCHA click points: %s", coords)
    return coords
