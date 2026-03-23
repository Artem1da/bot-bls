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

from captcha_solver import solve_hcaptcha, solve_grid_captcha
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


def solve_bls_number_captcha(page: Page) -> bool:
    """Solve the BLS number-grid CAPTCHA.

    The CAPTCHA shows: "Please select all boxes with number XXX"
    with a 3×3 grid of number images.  We screenshot the grid,
    send it to rucaptcha, and click the matching cells.
    """
    # Extract the target number from instruction text
    content = page.content()
    m = re.search(r"[Pp]lease select all boxes with number\s+(\d+)", content)
    if not m:
        logger.error("Cannot extract target number from captcha instruction")
        take_screenshot(page, "captcha_no_instruction")
        return False

    target_number = m.group(1)
    instruction = f"Select all boxes with number {target_number}"
    logger.info("BLS number CAPTCHA: target = %s", target_number)

    # Find the captcha grid container.
    # The grid cells are typically <div> or <img> elements inside a container.
    # We look for common patterns: a container with multiple clickable image cells.
    grid_container = page.query_selector(
        "div.captcha-grid, div.captcha-container, "
        "div:has(> div > img[src*='captcha']), "
        "div:has(> div > img[src*='Captcha']), "
        "div:has(> div > img[src*='blob'])"
    )
    if not grid_container:
        # Fallback: find the element that contains the instruction and the grid
        # Try to find a parent container by looking for the instruction text element
        instruction_el = page.query_selector(
            "p:has-text('select all boxes'), "
            "div:has-text('select all boxes'), "
            "span:has-text('select all boxes')"
        )
        if instruction_el:
            # The grid is likely a sibling or nearby element
            grid_container = instruction_el.evaluate_handle(
                """el => {
                    // Walk up to find a container that has multiple child divs/images
                    let parent = el.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const imgs = parent.querySelectorAll('img');
                        if (imgs.length >= 9) return parent;
                        parent = parent.parentElement;
                    }
                    return null;
                }"""
            ).as_element()

    if not grid_container:
        logger.error("Cannot find captcha grid container")
        take_screenshot(page, "captcha_no_grid")
        return False

    # Screenshot the grid for rucaptcha
    grid_screenshot = grid_container.screenshot()
    image_b64 = base64.b64encode(grid_screenshot).decode("ascii")
    logger.info("Captured captcha grid screenshot (%d bytes)", len(grid_screenshot))

    # Send to rucaptcha
    cells = solve_grid_captcha(RUCAPTCHA_KEY, image_b64, instruction, rows=3, cols=3)
    if not cells:
        logger.error("rucaptcha failed to solve grid captcha")
        return False

    # Find all clickable grid cells (images or divs)
    # They should be inside the container, in order (row-major)
    cell_elements = grid_container.query_selector_all("img")
    if len(cell_elements) < 9:
        # Try divs with background images
        cell_elements = grid_container.query_selector_all("div[style*='background'], div > img")
    if len(cell_elements) < 9:
        # Broader: any direct children that look clickable
        cell_elements = grid_container.query_selector_all("div > div")

    logger.info("Found %d captcha cell elements", len(cell_elements))

    if len(cell_elements) < 9:
        logger.error("Expected 9 captcha cells, found %d", len(cell_elements))
        take_screenshot(page, "captcha_wrong_cell_count")
        return False

    # Click the cells indicated by rucaptcha (1-indexed)
    for cell_num in cells:
        idx = cell_num - 1  # convert to 0-indexed
        if 0 <= idx < len(cell_elements):
            cell_elements[idx].click()
            logger.info("Clicked captcha cell %d", cell_num)
            time.sleep(0.3)
        else:
            logger.warning("Cell number %d out of range", cell_num)

    time.sleep(1)
    take_screenshot(page, "captcha_cells_clicked")
    return True


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
    content = page.content().lower()
    on_password_step = "enter your account password" in content or "password" in content.split("email")[0] if "email" not in content else False

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

        # Click "Verify" button
        verify_btn = page.query_selector(
            "button:has-text('Verify'), input[value='Verify'], "
            "button[type='submit'], input[type='submit']"
        )
        if verify_btn:
            verify_btn.click()
            logger.info("Clicked Verify button")
        else:
            logger.error("Cannot find Verify button")
            take_screenshot(page, "login_no_verify_btn")
            return False

        time.sleep(5)
        take_screenshot(page, "after_verify")

        # Check for rate-limit after verify
        if is_rate_limited(page):
            logger.warning("Rate-limited after email verify step")
            return False

    # ── Step 2: Password + Number CAPTCHA ──
    logger.info("Login step 2: entering password...")
    password_field = page.query_selector("input[type='password']")
    if not password_field:
        # BLS may use type=text for password too
        password_field = _find_visible_text_input(page)

    if not password_field:
        logger.error("Cannot find password input after verify")
        take_screenshot(page, "login_no_password_field")
        return False

    password_field.click()
    time.sleep(0.3)
    password_field.fill(BLS_PASSWORD)
    logger.info("Filled password")
    time.sleep(1)

    # Solve BLS number-grid CAPTCHA if present
    if _has_number_grid_captcha(page):
        logger.info("Number-grid CAPTCHA detected on password page")
        if not solve_bls_number_captcha(page):
            logger.error("Failed to solve number-grid CAPTCHA")
            return False

    # Solve hCaptcha if present instead
    if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
        logger.info("hCaptcha on login page, solving...")
        if not solve_and_submit_captcha(page):
            logger.error("Failed to solve login hCaptcha")
            return False

    # Click Submit button
    for sel in [
        "button:has-text('Submit')", "input[value='Submit']",
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Login')", "button:has-text('Sign In')",
        "input[value='Login']", "#btnSubmit", "#btnLogin",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                logger.info("Clicked login/submit button: %s", sel)
                break
        except Exception:
            continue

    time.sleep(5)
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


def fill_form(page: Page):
    """Fill in the visa type selection form."""
    # The BLS form has dropdowns that load sequentially.
    # We try common selector patterns used by BLS sites.

    # Category (Normal / Prime Time)
    for sel in ["#AppointmentCategoryId", "#category", "select[name='AppointmentCategoryId']"]:
        try:
            if page.query_selector(sel):
                select_dropdown(page, sel, CATEGORY)
                break
        except Exception:
            continue

    # Location
    for sel in ["#LocationId", "#centre", "#location", "select[name='LocationId']"]:
        try:
            if page.query_selector(sel):
                select_dropdown(page, sel, LOCATION)
                break
        except Exception:
            continue

    # Visa Type
    for sel in ["#VisaTypeId", "#visa_type", "select[name='VisaTypeId']"]:
        try:
            if page.query_selector(sel):
                select_dropdown(page, sel, VISA_TYPE)
                break
        except Exception:
            continue

    time.sleep(2)

    # Visa Sub Type
    for sel in ["#VisaSubTypeId", "#visa_sub_type", "select[name='VisaSubTypeId']"]:
        try:
            if page.query_selector(sel):
                select_dropdown(page, sel, VISA_SUB_TYPE)
                break
        except Exception:
            continue

    # Appointment for (Individual / Family)
    for sel in ["#AppointmentForId", "#appointment_for", "select[name='AppointmentForId']"]:
        try:
            if page.query_selector(sel):
                select_dropdown(page, sel, APPOINTMENT_FOR)
                break
        except Exception:
            continue


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
    """Check if the page shows a rate-limit / Too Many Requests error."""
    content = page.content().lower()
    indicators = [
        "too many requests",
        "rate limit",
        "excessive requests",
        "try again after some time",
        "429",
    ]
    return any(ind in content for ind in indicators)


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

            take_screenshot(page, "appointment_page")

            # Dump page selectors for debugging (first time after login)
            if iteration <= 2:
                dump_form_structure(page)

            # Fill the visa type form
            fill_form(page)
            time.sleep(2)

            take_screenshot(page, "form_filled")

            # Click submit/book to proceed
            click_submit(page)
            time.sleep(3)

            take_screenshot(page, "after_submit")

            # Solve CAPTCHA after submit (BLS shows number-grid or hCaptcha here)
            if _has_number_grid_captcha(page):
                logger.info("Number-grid CAPTCHA after submit, solving...")
                if not solve_bls_number_captcha(page):
                    logger.error("Post-submit CAPTCHA solve failed, retrying")
                    wait_with_jitter(CHECK_INTERVAL)
                    continue
                # Click submit again after solving captcha
                click_submit(page)
                time.sleep(5)
                take_screenshot(page, "after_captcha_submit")

            if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
                logger.info("hCaptcha after submit, solving...")
                if not solve_and_submit_captcha(page):
                    logger.error("Post-submit hCaptcha solve failed, retrying")
                    wait_with_jitter(CHECK_INTERVAL)
                    continue
                click_submit(page)
                time.sleep(5)
                take_screenshot(page, "after_hcaptcha_submit")

            # Check for available dates
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
