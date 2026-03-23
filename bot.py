#!/usr/bin/env python3
"""
BLS Spain Armenia — Visa Appointment Monitor & Auto-Booker.

Monitors the BLS visa appointment page for available slots,
solves hCaptcha via rucaptcha.com, and attempts to book when dates
matching criteria are found.

Usage:
    1. Copy .env.example → .env and fill in your values
    2. pip install -r requirements.txt
    3. playwright install chromium
    4. python bot.py
"""

import json
import os
import random
import re
import sys
import time
import logging
from datetime import datetime, date

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PWTimeout

import base64

from captcha_solver import solve_hcaptcha, ocr_cells_batch
from notifier import send_telegram

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────
TARGET_URL = os.getenv("TARGET_URL", "")
RUCAPTCHA_KEY = os.getenv("RUCAPTCHA_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# BLS account credentials
BLS_EMAIL = os.getenv("BLS_EMAIL", "")
BLS_PASSWORD = os.getenv("BLS_PASSWORD", "")

CATEGORY = os.getenv("CATEGORY", "Normal")
LOCATION = os.getenv("LOCATION", "Yerevan")
VISA_TYPE = os.getenv("VISA_TYPE", "Schengen Visa")
VISA_SUB_TYPE = os.getenv("VISA_SUB_TYPE", "Tourism (Short Term)")
APPOINTMENT_FOR = os.getenv("APPOINTMENT_FOR", "Individual")
MIN_DATE_STR = os.getenv("MIN_DATE", "2026-04-04")
MIN_DATE = date.fromisoformat(MIN_DATE_STR)

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Rate-limit backoff settings
MAX_BACKOFF = 600  # 10 minutes max wait
JITTER_RANGE = 0.3  # ±30% random jitter on intervals

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def notify(msg: str):
    """Log and send Telegram notification."""
    logger.info(msg)
    send_telegram(TG_TOKEN, TG_CHAT, msg)


def select_dropdown(page: Page, selector: str, value: str, timeout: int = 10000):
    """Select an option from a <select> dropdown by visible text."""
    page.wait_for_selector(selector, timeout=timeout)
    page.select_option(selector, label=value)
    logger.info("Selected '%s' in %s", value, selector)
    time.sleep(1)  # allow dependent dropdowns to load


def inject_captcha_token(page: Page, token: str):
    """Inject solved hCaptcha token into the page."""
    page.evaluate("""(token) => {
        const ta1 = document.querySelector('textarea[name="g-recaptcha-response"]');
        const ta2 = document.querySelector('textarea[name="h-captcha-response"]');
        if (ta1) ta1.value = token;
        if (ta2) ta2.value = token;
    }""", token)
    logger.info("CAPTCHA token injected")


def extract_dates(page: Page) -> tuple[list[str], list[str]]:
    """Extract available_dates and fullCapicity_dates from page JS."""
    src = page.content()

    available = []
    full = []

    m = re.search(r"var\s+available_dates\s*=\s*(\[.*?\]);", src)
    if m:
        raw = m.group(1).replace("'", '"')
        try:
            available = json.loads(raw)
        except Exception:
            available = [d.strip().strip('"').strip("'") for d in raw.strip("[]").split(",") if d.strip()]

    m = re.search(r"var\s+fullCapicity_dates\s*=\s*(\[.*?\]);", src)
    if m:
        raw = m.group(1).replace("'", '"')
        try:
            full = json.loads(raw)
        except Exception:
            full = [d.strip().strip('"').strip("'") for d in raw.strip("[]").split(",") if d.strip()]

    return available, full


def filter_dates(dates: list[str], min_d: date) -> list[str]:
    """Keep only dates >= min_date."""
    result = []
    for d in dates:
        if not d:
            continue
        try:
            parsed = datetime.strptime(d, "%d/%m/%Y").date()
        except ValueError:
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                logger.warning("Unparseable date: %s", d)
                continue
        if parsed >= min_d:
            result.append(d)
    return result


def _find_visible_text_input(page: Page) -> "ElementHandle | None":
    """Return the first visible, non-hidden text input on the page.

    BLS obfuscates field ids/names on every load, so we can't rely on
    selectors like #EmailId.  Instead we grab all visible <input type=text>.
    """
    for inp in page.query_selector_all("input[type='text']"):
        try:
            if inp.is_visible():
                return inp
        except Exception:
            continue
    return None


def _is_login_page(page: Page) -> bool:
    """Heuristic: are we on the BLS login/verify page?"""
    url = page.url.lower()
    content = page.content().lower()
    return (
        "login" in url
        or "session is expired" in content
        or "please log in" in content
        or "enter your account password" in content
    )


def _has_number_grid_captcha(page: Page) -> bool:
    """Check if the BLS number-grid CAPTCHA is present on page."""
    content = page.content().lower()
    return "please select all boxes with number" in content


def _find_captcha_grid(page: Page) -> dict:
    """Find the captcha grid area and instruction element position.

    Instead of trying to find individual cell <img> elements (BLS layers
    multiple images per cell for obfuscation), we find the visual grid
    container and divide it into a 3×3 grid mathematically.

    NOTE: BLS uses a custom font that renders different digits visually
    than what the DOM text contains, so we cannot trust textContent for
    the target number. The caller must OCR the instruction line visually.

    Returns dict with:
      - instrBox: {x, y, w, h} bounding box of the instruction text
      - gridBox: {x, y, w, h} bounding box of the 3×3 grid area
      - cellBoxes: list of 9 {x, y, w, h} boxes (row-major order)
      - debug: debug info string
    """
    info = page.evaluate("""() => {
        const result = { instrBox: null, gridBox: null, cellBoxes: [], debug: '' };

        // Step 1: Find the instruction element
        let instructionEl = null;
        const allEls = document.querySelectorAll('*');
        for (const el of allEls) {
            const txt = el.textContent || '';
            if (/select all boxes with number/i.test(txt) && el.children.length < 20) {
                if (!instructionEl || el.textContent.length < instructionEl.textContent.length) {
                    instructionEl = el;
                }
            }
        }

        if (!instructionEl) {
            result.debug = 'No instruction element found';
            return result;
        }

        // Get instruction bounding box for visual OCR
        const instrRect = instructionEl.getBoundingClientRect();
        result.instrBox = {
            x: instrRect.x, y: instrRect.y,
            w: instrRect.width, h: instrRect.height
        };
        result.debug = 'Instruction element found: ' +
            Math.round(instrRect.width) + 'x' + Math.round(instrRect.height);

        // Step 2: Walk up from instruction to find a container with images
        let container = instructionEl.parentElement;
        let imgs = [];
        for (let i = 0; i < 8 && container; i++) {
            imgs = Array.from(container.querySelectorAll('img'));
            if (imgs.length >= 9) break;
            container = container.parentElement;
        }

        if (!container || imgs.length < 9) {
            result.debug += ' | No container with enough images found';
            return result;
        }

        result.debug += ' | Container: ' + container.tagName +
            ' | imgs: ' + imgs.length;

        // Step 3: Find the grid bounding box from images below instruction
        const imgRects = imgs.map(img => {
            const r = img.getBoundingClientRect();
            return { x: r.x, y: r.y, w: r.width, h: r.height, area: r.width * r.height };
        }).filter(i => i.w > 30 && i.h > 30 && i.area > 1000);

        if (imgRects.length < 9) {
            result.debug += ' | Not enough sized images: ' + imgRects.length;
            return result;
        }

        // Filter images that are BELOW the instruction text
        const belowInstr = imgRects.filter(r => r.y >= instrRect.bottom - 5);
        const targetImgs = belowInstr.length >= 9 ? belowInstr : imgRects;

        const minX = Math.min(...targetImgs.map(r => r.x));
        const minY = Math.min(...targetImgs.map(r => r.y));
        const maxX = Math.max(...targetImgs.map(r => r.x + r.w));
        const maxY = Math.max(...targetImgs.map(r => r.y + r.h));

        const gridBox = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
        result.gridBox = gridBox;

        result.debug += ' | Grid box: ' + Math.round(gridBox.x) + ',' +
            Math.round(gridBox.y) + ' ' + Math.round(gridBox.w) + 'x' +
            Math.round(gridBox.h) + ' (from ' + targetImgs.length + ' imgs)';

        // Step 4: Divide grid into 3×3 cells
        const cellW = gridBox.w / 3;
        const cellH = gridBox.h / 3;
        for (let row = 0; row < 3; row++) {
            for (let col = 0; col < 3; col++) {
                result.cellBoxes.push({
                    x: gridBox.x + col * cellW,
                    y: gridBox.y + row * cellH,
                    w: cellW,
                    h: cellH,
                });
            }
        }

        return result;
    }""")

    logger.info("Captcha grid detection: cells=%d | %s",
                len(info.get('cellBoxes', [])), info.get('debug', ''))
    return info


def solve_bls_number_captcha(page: Page) -> bool:
    """Solve the BLS number-grid CAPTCHA.

    The CAPTCHA shows: "Please select all boxes with number XXX"
    with a 3×3 grid of number images.

    BLS uses a custom font that renders different digits than the DOM text,
    so we cannot trust textContent. Instead we OCR all 9 cells, then
    determine the target as the most frequent number (the captcha always
    has multiple correct cells, so the mode = the answer).
    """
    # Find grid layout
    captcha_info = _find_captcha_grid(page)
    cell_boxes = captcha_info.get('cellBoxes', [])

    if len(cell_boxes) < 9:
        logger.error("Could not find captcha grid cells (found %d)", len(cell_boxes))
        take_screenshot(page, "captcha_no_cells")
        return False

    os.makedirs("screenshots", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Screenshot each cell
    cell_images_b64 = []
    for i, box in enumerate(cell_boxes):
        # Inset by 10% to avoid grid borders / gaps between cells
        inset_x = box['w'] * 0.10
        inset_y = box['h'] * 0.10
        clip = {
            "x": box['x'] + inset_x,
            "y": box['y'] + inset_y,
            "width": box['w'] - 2 * inset_x,
            "height": box['h'] - 2 * inset_y,
        }
        cell_png = page.screenshot(clip=clip)
        cell_b64 = base64.b64encode(cell_png).decode("ascii")
        cell_images_b64.append(cell_b64)
        with open(f"screenshots/captcha_cell_{ts}_{i+1}.png", "wb") as f:
            f.write(cell_png)

    logger.info("Captured %d cell screenshots", len(cell_images_b64))

    # OCR all 9 cells in parallel via rucaptcha
    ocr_results = ocr_cells_batch(RUCAPTCHA_KEY, cell_images_b64)
    if not ocr_results:
        logger.error("OCR batch failed")
        return False

    # Clean OCR results: keep only digits
    cleaned_cells = []
    for i, ocr_text in enumerate(ocr_results):
        cleaned = re.sub(r'\D', '', ocr_text) if ocr_text else ""
        cleaned_cells.append(cleaned)
        logger.info("Cell %d OCR: '%s' -> '%s'", i + 1, ocr_text, cleaned)

    # Determine target: the most frequent number among the 9 cells.
    # The captcha always has multiple correct cells (3-5), making the
    # correct number the most common one.
    from collections import Counter
    counts = Counter(c for c in cleaned_cells if c)
    if not counts:
        logger.error("All OCR results empty: %s", ocr_results)
        take_screenshot(page, "captcha_ocr_empty")
        return False

    target_number = counts.most_common(1)[0][0]
    target_count = counts.most_common(1)[0][1]
    logger.info("Target number (most frequent): %s (appears %d times). All counts: %s",
                target_number, target_count, dict(counts))

    # Find cells matching target
    cells_to_click = []
    for i, cleaned in enumerate(cleaned_cells):
        if cleaned == target_number:
            cells_to_click.append(i)

    if not cells_to_click:
        logger.error("No cells matched target number %s. OCR results: %s",
                     target_number, cell_ocr)
        take_screenshot(page, "captcha_no_match")
        return False

    logger.info("Cells matching target %s: %s", target_number,
                [c + 1 for c in cells_to_click])

    # Click matching cells
    for idx in cells_to_click:
        box = cell_boxes[idx]
        center_x = box['x'] + box['w'] / 2
        center_y = box['y'] + box['h'] / 2
        page.mouse.click(center_x, center_y)
        logger.info("Clicked captcha cell %d at (%.0f, %.0f)", idx + 1, center_x, center_y)
        time.sleep(0.5)

    time.sleep(1)
    take_screenshot(page, "captcha_cells_clicked")
    return True


def _find_password_field(page: Page) -> "ElementHandle | None":
    """Find the password input on the BLS captcha/password page.

    Tries type=password first, then looks for a visible text input
    near the 'Password' label.
    """
    # Standard password field
    pf = page.query_selector("input[type='password']")
    if pf:
        return pf

    # BLS may label it "Password *" but use type=text with obfuscated id.
    # Find by proximity to the Password label.
    try:
        pf = page.evaluate_handle("""() => {
            // Find a label or text node containing "Password"
            const allText = document.querySelectorAll('label, span, p, div');
            for (const el of allText) {
                const txt = el.textContent.trim();
                if (/^Password\\s*\\*?$/.test(txt)) {
                    // Look for an input right after this element
                    let sibling = el.nextElementSibling;
                    for (let i = 0; i < 5 && sibling; i++) {
                        const inp = sibling.tagName === 'INPUT' ? sibling
                                  : sibling.querySelector('input');
                        if (inp && inp.type !== 'hidden') return inp;
                        sibling = sibling.nextElementSibling;
                    }
                    // Also check parent's next sibling
                    let parent = el.parentElement;
                    if (parent) {
                        const inp = parent.querySelector('input:not([type=hidden])');
                        if (inp) return inp;
                    }
                }
            }
            return null;
        }""").as_element()
        if pf:
            return pf
    except Exception:
        pass

    # Last resort: any visible text input (but not the ones in captcha)
    return _find_visible_text_input(page)


def do_login(page: Page) -> bool:
    """Log in to BLS account.

    BLS uses a two-step login:
      Step 1 — Enter email, click "Verify"
      Step 2 — Enter password + solve number-grid CAPTCHA, click "Submit"
    """
    if not _is_login_page(page):
        logger.info("Not on login page, assuming already logged in")
        return True

    take_screenshot(page, "login_page")

    # Detect which step we're on
    on_password_step = _has_number_grid_captcha(page) or "enter your account password" in page.content().lower()

    # ── Step 1: Email ──
    if not on_password_step:
        logger.info("Login step 1: entering email...")
        email_field = _find_visible_text_input(page)
        if not email_field:
            logger.error("Cannot find email input on login page")
            take_screenshot(page, "login_no_email_field")
            return False

        email_field.click()
        time.sleep(0.3)
        email_field.fill(BLS_EMAIL)
        logger.info("Filled email: %s", BLS_EMAIL)
        time.sleep(1)

        # Click "Verify" — this triggers navigation to the password+captcha page.
        # We use expect_navigation to avoid implicit waits hanging on networkidle.
        verify_btn = page.query_selector(
            "button:has-text('Verify'), input[value='Verify'], "
            "button[type='submit'], input[type='submit']"
        )
        if not verify_btn:
            logger.error("Cannot find Verify button")
            take_screenshot(page, "login_no_verify_btn")
            return False

        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                verify_btn.click()
                logger.info("Clicked Verify, waiting for navigation...")
        except PWTimeout:
            logger.warning("Navigation after Verify timed out, continuing anyway")
        except Exception as e:
            logger.warning("Navigation after Verify: %s, continuing", e)

        # Wait for the page to settle (captcha images may still load)
        time.sleep(3)
        take_screenshot(page, "after_verify")

        # Check for rate-limit after verify
        if is_rate_limited(page):
            logger.warning("Rate-limited after email verify step")
            return False

    # ── Step 2: Password + Number CAPTCHA ──
    logger.info("Login step 2: password + captcha...")

    # Fill password by clicking near the "Password" label and typing.
    # BLS hides the actual <input> behind overlays, so we locate the label
    # text, click ~30px below it (where the input visually sits), and type.
    try:
        pwd_label_box = page.evaluate("""() => {
            const labels = document.querySelectorAll('label, span, p, div');
            for (const el of labels) {
                if (/^Password\\s*\\*?$/.test(el.textContent.trim())) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        return {x: r.x + r.width / 2, y: r.bottom + 15};
                    }
                }
            }
            return null;
        }""")
    except Exception as e:
        logger.error("Failed to locate Password label: %s", e)
        pwd_label_box = None

    if pwd_label_box:
        x, y = pwd_label_box["x"], pwd_label_box["y"]
        logger.info("Clicking near Password label at (%d, %d)", x, y)
        page.mouse.click(x, y)
        time.sleep(0.5)
        page.keyboard.type(BLS_PASSWORD, delay=50)
        logger.info("Typed password via keyboard")
    else:
        # Fallback: try to find the input and set value via JS
        logger.warning("Password label not found, falling back to JS value set")
        password_field = _find_password_field(page)
        if not password_field:
            logger.error("Cannot find password input at all")
            take_screenshot(page, "login_no_password_field")
            return False
        try:
            page.evaluate("""([el, pwd]) => {
                el.removeAttribute('disabled');
                el.removeAttribute('readonly');
                el.classList.remove('entry-disabled');
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(el, pwd);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", [password_field, BLS_PASSWORD])
            logger.info("Set password via JS fallback")
        except Exception as e:
            logger.error("JS password fallback also failed: %s", e)
            take_screenshot(page, "login_password_fill_error")
            return False
    time.sleep(0.5)

    # Solve CAPTCHA
    if _has_number_grid_captcha(page):
        logger.info("Number-grid CAPTCHA detected on password page")
        if not solve_bls_number_captcha(page):
            logger.error("Failed to solve number-grid CAPTCHA")
            return False
    elif page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
        logger.info("hCaptcha on login page, solving...")
        if not solve_and_submit_captcha(page):
            logger.error("Failed to solve login hCaptcha")
            return False

    time.sleep(0.5)
    take_screenshot(page, "password_and_captcha_done")

    # Click Submit — this also triggers navigation
    submit_clicked = False
    for sel in [
        "button:has-text('Submit')", "input[value='Submit']",
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Login')", "input[value='Login']",
        "#btnSubmit", "#btnLogin",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                try:
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                        el.click()
                        logger.info("Clicked Submit: %s", sel)
                except PWTimeout:
                    logger.warning("Navigation after Submit timed out, continuing")
                except Exception as e:
                    logger.warning("Navigation after Submit: %s, continuing", e)
                submit_clicked = True
                break
        except Exception:
            continue

    if not submit_clicked:
        logger.error("Could not find Submit button on login page")
        take_screenshot(page, "login_no_submit")
        return False

    time.sleep(3)
    take_screenshot(page, "after_login")

    # Check for rate-limit
    if is_rate_limited(page):
        logger.warning("Rate-limited after login submit")
        return False

    # Verify we left the login page
    if _is_login_page(page):
        logger.error("Still on login page after submit — login likely failed")
        take_screenshot(page, "login_failed")
        return False

    logger.info("Login successful")
    return True


def click_book_appointment(page: Page) -> bool:
    """Click the 'Book New Appointment' / 'Book your appointment' link/button."""
    for sel in [
        "a:has-text('Book New Appointment')",
        "a:has-text('Book your appointment')",
        "a:has-text('Book Appointment')",
        "a[href*='newappointment']",
        "a[href*='NewAppointment']",
        "a[href*='appointment']",
        "button:has-text('Book')",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                logger.info("Clicked 'Book Appointment': %s", sel)
                time.sleep(3)
                return True
        except Exception:
            continue

    logger.warning("Could not find 'Book Appointment' button/link")
    take_screenshot(page, "no_book_appointment")
    return False


def fill_form(page: Page) -> bool:
    """Fill in the visa type selection form.

    Returns True if at least one dropdown was found and filled.
    """
    # The BLS form has dropdowns that load sequentially.
    # We try common selector patterns used by BLS sites.
    filled_count = 0

    # Log all <select> elements on page for debugging
    all_selects = page.query_selector_all("select")
    logger.info("Found %d <select> elements on page", len(all_selects))
    for i, s in enumerate(all_selects):
        try:
            sid = s.get_attribute("id") or ""
            sname = s.get_attribute("name") or ""
            opts = s.query_selector_all("option")
            opt_texts = [o.text_content().strip() for o in opts[:5]]
            logger.info("  select[%d] id=%s name=%s options=%s", i, sid, sname, opt_texts)
        except Exception:
            pass

    # Each dropdown: list of (selectors, value, label)
    dropdowns = [
        (["#AppointmentCategoryId", "#category", "select[name='AppointmentCategoryId']"],
         CATEGORY, "Category"),
        (["#LocationId", "#centre", "#location", "select[name='LocationId']"],
         LOCATION, "Location"),
        (["#VisaTypeId", "#visa_type", "select[name='VisaTypeId']"],
         VISA_TYPE, "Visa Type"),
    ]

    for selectors, value, label in dropdowns:
        found = False
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    select_dropdown(page, sel, value)
                    filled_count += 1
                    found = True
                    break
            except Exception as e:
                logger.warning("Failed to select '%s' in %s: %s", value, sel, e)
                continue
        if not found:
            logger.warning("Could not find dropdown for %s", label)

    time.sleep(2)

    # These depend on previous selections loading
    dependent_dropdowns = [
        (["#VisaSubTypeId", "#visa_sub_type", "select[name='VisaSubTypeId']"],
         VISA_SUB_TYPE, "Visa Sub Type"),
        (["#AppointmentForId", "#appointment_for", "select[name='AppointmentForId']"],
         APPOINTMENT_FOR, "Appointment For"),
    ]

    for selectors, value, label in dependent_dropdowns:
        found = False
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    select_dropdown(page, sel, value)
                    filled_count += 1
                    found = True
                    break
            except Exception as e:
                logger.warning("Failed to select '%s' in %s: %s", value, sel, e)
                continue
        if not found:
            logger.warning("Could not find dropdown for %s", label)

    logger.info("Filled %d/%d dropdowns", filled_count, len(dropdowns) + len(dependent_dropdowns))
    return filled_count > 0


def solve_and_submit_captcha(page: Page) -> bool:
    """Find hCaptcha on page, solve via rucaptcha, inject, and submit."""
    hcaptcha_el = page.query_selector(".h-captcha, [data-hcaptcha-widget-id]")
    if not hcaptcha_el:
        # Try to find captcha iframe
        iframe = page.query_selector("iframe[src*='hcaptcha']")
        if iframe:
            sitekey_match = re.search(r"sitekey=([a-f0-9-]+)", iframe.get_attribute("src") or "")
            if sitekey_match:
                sitekey = sitekey_match.group(1)
            else:
                logger.error("Cannot extract hCaptcha sitekey")
                return False
        else:
            logger.error("No CAPTCHA found on page")
            return False
    else:
        sitekey = hcaptcha_el.get_attribute("data-sitekey")
        if not sitekey:
            logger.error("hCaptcha element found but no data-sitekey attribute")
            return False

    logger.info("Found hCaptcha sitekey: %s", sitekey)
    current_url = page.url

    token = solve_hcaptcha(RUCAPTCHA_KEY, sitekey, current_url)
    if not token:
        return False

    inject_captcha_token(page, token)
    return True


def click_submit(page: Page):
    """Find and click the submit/book button."""
    for selector in [
        "input[type='submit']",
        "button[type='submit']",
        "#btnSubmit",
        "#btnBook",
        "input[value='Book Appointment']",
        "input[value='Submit']",
        "button:has-text('Book')",
        "button:has-text('Submit')",
        "a:has-text('Book Appointment')",
        'input[onclick*="validate"]',
    ]:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                el.click()
                logger.info("Clicked submit: %s", selector)
                return
        except Exception:
            continue
    logger.warning("Could not find submit button")


def try_book_date(page: Page, target_date: str) -> bool:
    """Attempt to click a date in the calendar and complete booking."""
    # Click the date input to open calendar
    for sel in ["#app_date", "#AppointmentDate", "input[name='AppointmentDate']", "#datepicker"]:
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                time.sleep(1)
                break
        except Exception:
            continue

    # Try to click the target date in the calendar
    try:
        parsed = None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(target_date, fmt)
                break
            except ValueError:
                continue

        if not parsed:
            logger.error("Cannot parse date: %s", target_date)
            return False

        day = parsed.day
        # Click the day cell in datepicker
        page.click(f"td.day:has-text('{day}'):not(.disabled):not(.old):not(.new)")
        logger.info("Clicked date: %s (day %d)", target_date, day)
        time.sleep(2)
    except Exception as e:
        logger.error("Failed to click date %s: %s", target_date, e)
        return False

    # Select time slot if available
    for sel in ["#app_time", "#AppointmentTime", "select[name='AppointmentTime']"]:
        try:
            el = page.query_selector(sel)
            if el:
                options = el.query_selector_all("option")
                for opt in options:
                    val = opt.get_attribute("value")
                    if val and val != "" and val != "0":
                        page.select_option(sel, value=val)
                        logger.info("Selected time slot: %s", opt.text_content())
                        break
                break
        except Exception:
            continue

    time.sleep(1)

    # Solve CAPTCHA if present at this stage
    if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
        if not solve_and_submit_captcha(page):
            logger.error("Failed to solve CAPTCHA during booking")
            return False

    # Submit
    click_submit(page)
    time.sleep(5)

    take_screenshot(page, "booking_result")

    # Check for success indicators
    src = page.content().lower()
    if any(kw in src for kw in ["success", "confirmed", "booked", "reference number", "confirmation"]):
        return True

    return False


def is_rate_limited(page: Page) -> bool:
    """Check if the page shows a rate-limit / Too Many Requests error.

    We check visible text only (not raw HTML) to avoid false positives
    from random numbers in image URLs, captcha data, etc.
    """
    try:
        # Check page title
        title = (page.title() or "").lower()
        if "too many requests" in title or "429" in title:
            return True

        # Check visible body text (not raw HTML source)
        body_text = page.inner_text("body").lower()
        indicators = [
            "too many requests",
            "rate limit",
            "excessive requests from your ip",
        ]
        return any(ind in body_text for ind in indicators)
    except Exception:
        return False


def wait_with_jitter(seconds: float):
    """Sleep for `seconds` with ±JITTER_RANGE random jitter."""
    jitter = seconds * random.uniform(-JITTER_RANGE, JITTER_RANGE)
    actual = max(1, seconds + jitter)
    logger.info("Waiting %.0f seconds (base %d ± jitter)", actual, seconds)
    time.sleep(actual)


def take_screenshot(page: Page, name: str):
    """Save a screenshot for debugging."""
    os.makedirs("screenshots", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"screenshots/{name}_{ts}.png"
    page.screenshot(path=path, full_page=True)
    logger.info("Screenshot saved: %s", path)


def dump_form_structure(page: Page):
    """Log all form elements on page for debugging."""
    selects = page.query_selector_all("select")
    logger.info("Found %d <select> elements:", len(selects))
    for s in selects:
        name = s.get_attribute("name") or ""
        id_ = s.get_attribute("id") or ""
        options = s.query_selector_all("option")
        opts_text = [o.text_content().strip() for o in options[:10]]
        logger.info("  select id=%s name=%s options=%s", id_, name, opts_text)

    inputs = page.query_selector_all("input")
    logger.info("Found %d <input> elements:", len(inputs))
    for inp in inputs:
        type_ = inp.get_attribute("type") or ""
        name = inp.get_attribute("name") or ""
        id_ = inp.get_attribute("id") or ""
        logger.info("  input id=%s name=%s type=%s", id_, name, type_)

    hcap = page.query_selector(".h-captcha")
    if hcap:
        logger.info("hCaptcha found, sitekey=%s", hcap.get_attribute("data-sitekey"))

    iframes = page.query_selector_all("iframe")
    logger.info("Found %d iframes:", len(iframes))
    for f in iframes:
        src = f.get_attribute("src") or ""
        logger.info("  iframe src=%s", src[:100])


def monitor_loop(page: Page, browser: Browser):
    """Main monitoring loop."""
    iteration = 0
    logged_in = False
    backoff = 0  # current rate-limit backoff in seconds

    RATE_LIMIT_COOLDOWN = 120  # minimum 2 minutes on rate-limit

    while True:
        iteration += 1
        logger.info("── Iteration %d ──", iteration)

        # If we were rate-limited, wait BEFORE making any request
        if backoff > 0:
            logger.info("Rate-limit cooldown: sleeping %d s before next request", backoff)
            wait_with_jitter(backoff)

        try:
            # Navigate to the appointment page
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            time.sleep(3)

            # ── Rate-limit detection ──
            if is_rate_limited(page):
                backoff = min(max(backoff * 2, RATE_LIMIT_COOLDOWN), MAX_BACKOFF)
                logger.warning("Rate-limited by BLS! Will wait %d s before next attempt", backoff)
                notify(f"[Iter {iteration}] Rate-limited. Cooling down {backoff}s.")
                take_screenshot(page, "rate_limited")
                continue
            # Reset backoff on success
            backoff = 0

            take_screenshot(page, "page_loaded")
            logger.info("Page: %s | URL: %s", page.title(), page.url)

            # Login if needed
            if not logged_in:
                if BLS_EMAIL and BLS_PASSWORD:
                    logged_in = do_login(page)
                    if not logged_in:
                        logger.error("Login failed, retrying next iteration")
                        wait_with_jitter(CHECK_INTERVAL)
                        continue
                else:
                    logged_in = True  # No credentials = no login needed

            # After login we land on a dashboard — click "Book New Appointment"
            if not click_book_appointment(page):
                # Maybe we're already on the form page, try to continue
                logger.info("Proceeding without Book Appointment click")

            time.sleep(3)
            if is_rate_limited(page):
                backoff = RATE_LIMIT_COOLDOWN
                logger.warning("Rate-limited after navigating! Will wait %d s", backoff)
                logged_in = False
                continue

            take_screenshot(page, "after_book_click")

            # ── Solve CAPTCHA that appears BEFORE the form ──
            # BLS shows a "Captcha Verification" page after clicking Book Now.
            if _has_number_grid_captcha(page):
                logger.info("Number-grid CAPTCHA before form, solving...")
                if not solve_bls_number_captcha(page):
                    logger.error("Pre-form CAPTCHA solve failed, retrying")
                    wait_with_jitter(CHECK_INTERVAL)
                    continue
                # Click Submit to pass the captcha page
                click_submit(page)
                time.sleep(5)
                take_screenshot(page, "after_pre_form_captcha")

            if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
                logger.info("hCaptcha before form, solving...")
                if not solve_and_submit_captcha(page):
                    logger.error("Pre-form hCaptcha solve failed, retrying")
                    wait_with_jitter(CHECK_INTERVAL)
                    continue
                click_submit(page)
                time.sleep(5)
                take_screenshot(page, "after_pre_form_hcaptcha")

            # ── Now we should be on the form page with dropdowns ──
            take_screenshot(page, "form_page")

            # Dump page selectors for debugging (first iterations)
            if iteration <= 2:
                dump_form_structure(page)

            # Fill the visa type form (Category, Location, etc.)
            form_ok = fill_form(page)
            time.sleep(2)

            take_screenshot(page, "form_filled")

            if not form_ok:
                logger.warning("No dropdowns found — page may not have loaded correctly")
                take_screenshot(page, "form_not_found")
                # Don't proceed to check dates, retry next iteration
                logged_in = False
                wait_with_jitter(CHECK_INTERVAL)
                continue

            # Click submit/book to proceed to the calendar
            click_submit(page)
            time.sleep(3)

            take_screenshot(page, "after_form_submit")

            # Solve CAPTCHA after form submit if one appears
            if _has_number_grid_captcha(page):
                logger.info("Number-grid CAPTCHA after form submit, solving...")
                if not solve_bls_number_captcha(page):
                    logger.error("Post-form CAPTCHA solve failed, retrying")
                    wait_with_jitter(CHECK_INTERVAL)
                    continue
                click_submit(page)
                time.sleep(5)
                take_screenshot(page, "after_post_form_captcha")

            if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
                logger.info("hCaptcha after form submit, solving...")
                if not solve_and_submit_captcha(page):
                    logger.error("Post-form hCaptcha solve failed, retrying")
                    wait_with_jitter(CHECK_INTERVAL)
                    continue
                click_submit(page)
                time.sleep(5)
                take_screenshot(page, "after_post_form_hcaptcha")

            # ── Check for available dates ──
            available, full_cap = extract_dates(page)
            logger.info("Available dates: %s", available)
            logger.info("Full capacity dates: %s", full_cap)

            if not available or (len(available) == 1 and available[0] == ""):
                logger.info("No available dates found")
                notify(f"[Iter {iteration}] No slots available")
                wait_with_jitter(CHECK_INTERVAL)
                continue

            # Filter dates
            good_dates = filter_dates(available, MIN_DATE)
            if not good_dates:
                logger.info("Available dates exist but none >= %s", MIN_DATE)
                wait_with_jitter(CHECK_INTERVAL)
                continue

            # Found matching dates!
            notify(f"SLOTS FOUND! Dates: {', '.join(good_dates)}")
            take_screenshot(page, "slots_found")

            # Attempt booking
            for target_date in good_dates:
                logger.info("Attempting to book: %s", target_date)
                success = try_book_date(page, target_date)
                if success:
                    notify(f"APPOINTMENT BOOKED for {target_date}!")
                    take_screenshot(page, "booked")
                    return  # Done!
                else:
                    logger.warning("Booking attempt for %s did not confirm", target_date)
                    take_screenshot(page, f"book_attempt_{target_date}")

            notify("Slots found but booking didn't confirm. Check screenshots!")

        except PWTimeout:
            logger.error("Page load timeout")
            try:
                take_screenshot(page, "timeout")
            except Exception:
                pass
            logged_in = False  # Session may have expired
        except Exception as e:
            logger.error("Error in iteration %d: %s", iteration, e, exc_info=True)
            try:
                take_screenshot(page, "error")
            except Exception:
                pass
            logged_in = False

        wait_with_jitter(CHECK_INTERVAL)


def main():
    if not TARGET_URL:
        logger.error("TARGET_URL not set. Copy .env.example to .env and configure it.")
        sys.exit(1)
    if not RUCAPTCHA_KEY:
        logger.error("RUCAPTCHA_API_KEY not set. Get one from https://rucaptcha.com")
        sys.exit(1)

    notify("BLS Visa Appointment Bot started")
    logger.info("Config: category=%s, location=%s, visa=%s, sub=%s, for=%s",
                CATEGORY, LOCATION, VISA_TYPE, VISA_SUB_TYPE, APPOINTMENT_FOR)
    logger.info("Looking for dates >= %s", MIN_DATE)
    logger.info("Check interval: %ds, Headless: %s", CHECK_INTERVAL, HEADLESS)

    with sync_playwright() as p:
        # Use system chromium if playwright's bundled one is missing
        chromium_path = os.path.expanduser("~/.cache/ms-playwright/chromium-1194/chrome-linux/chrome")
        launch_kwargs = {
            "headless": HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }
        if os.path.exists(chromium_path):
            launch_kwargs["executable_path"] = chromium_path
            logger.info("Using chromium at %s", chromium_path)
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        # Mask webdriver detection
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()
        try:
            monitor_loop(page, browser)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            notify("Bot stopped by user")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
