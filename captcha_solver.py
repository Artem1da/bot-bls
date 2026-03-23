"""CAPTCHA solvers using rucaptcha.com API.

Supports:
  - hCaptcha (token-based)
  - BLS number-grid CAPTCHA (OCR each cell individually)
"""

import base64
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

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


def _submit_ocr(api_key: str, image_base64: str,
                numeric_only: bool = True) -> str | None:
    """Submit a single image for OCR text recognition and return request_id."""
    payload = {
        "key": api_key,
        "method": "base64",
        "body": image_base64,
        "json": 1,
    }
    if numeric_only:
        payload["numeric"] = 1
        payload["min_len"] = 2
        payload["max_len"] = 4
    resp = requests.post(RUCAPTCHA_IN, data=payload, timeout=30)
    data = resp.json()
    if data.get("status") != 1:
        logger.error("rucaptcha OCR submit error: %s", data)
        return None
    return data["request"]


def ocr_cells_batch(api_key: str, cell_images_b64: list[str],
                    max_attempts: int = 30,
                    text_indices: set[int] | None = None) -> list[str | None]:
    """OCR multiple cell images in parallel via rucaptcha.

    Submits all images at once, then polls for all results.
    Returns list of OCR texts (or None for failed cells), same order as input.

    text_indices: set of indices that contain mixed text+digits (not numeric-only).
                  These are submitted without numeric constraint.
    """
    if text_indices is None:
        text_indices = set()

    # Submit all cells in parallel
    request_ids = []

    def submit_one(idx_img):
        idx, img_b64 = idx_img
        return _submit_ocr(api_key, img_b64, numeric_only=(idx not in text_indices))

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(submit_one, (idx, img)): idx
                   for idx, img in enumerate(cell_images_b64)}
        id_by_idx = {}
        for future in as_completed(futures):
            idx = futures[future]
            req_id = future.result()
            id_by_idx[idx] = req_id

    request_ids = [id_by_idx.get(i) for i in range(len(cell_images_b64))]
    submitted = sum(1 for r in request_ids if r)
    logger.info("Submitted %d/%d cell images for OCR", submitted, len(cell_images_b64))

    # Wait for initial processing (OCR is faster than hCaptcha)
    time.sleep(5)

    # Poll all results
    results: list[str | None] = [None] * len(request_ids)
    pending = {i for i, rid in enumerate(request_ids) if rid}

    for attempt in range(max_attempts):
        if not pending:
            break

        still_pending = set()
        for idx in pending:
            rid = request_ids[idx]
            try:
                resp = requests.get(RUCAPTCHA_RES, params={
                    "key": api_key,
                    "action": "get",
                    "id": rid,
                    "json": 1,
                }, timeout=15)
                data = resp.json()
                if data.get("status") == 1:
                    results[idx] = data["request"].strip()
                elif data.get("request") == "CAPCHA_NOT_READY":
                    still_pending.add(idx)
                else:
                    logger.warning("OCR cell %d error: %s", idx + 1, data)
            except Exception as e:
                logger.warning("OCR poll error for cell %d: %s", idx + 1, e)
                still_pending.add(idx)

        pending = still_pending
        if pending:
            time.sleep(3)

    logger.info("OCR results: %s", results)
    return results
