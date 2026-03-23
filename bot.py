#!/usr/bin/env python3
"""
BLS Spain Armenia — Visa Appointment Monitor & Auto-Booker.

Monitors the BLS visa appointment page for available slots,
solves hCaptcha via 2captcha, and attempts to book when dates
matching criteria are found.

Usage:
    1. Copy .env.example → .env and fill in your values
    2. pip install -r requirements.txt
    3. playwright install chromium
    4. python bot.py
"""

import os
import re
import sys
import time
import logging
from datetime import datetime, date

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PWTimeout

from captcha_solver import solve_hcaptcha
from notifier import send_telegram

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────
TARGET_URL = os.getenv("TARGET_URL", "")
TWOCAPTCHA_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

CATEGORY = os.getenv("CATEGORY", "Normal")
LOCATION = os.getenv("LOCATION", "Yerevan")
VISA_TYPE = os.getenv("VISA_TYPE", "Schengen Visa")
VISA_SUB_TYPE = os.getenv("VISA_SUB_TYPE", "Tourism (Short Term)")
APPOINTMENT_FOR = os.getenv("APPOINTMENT_FOR", "Individual")
MIN_DATE_STR = os.getenv("MIN_DATE", "2026-04-04")
MIN_DATE = date.fromisoformat(MIN_DATE_STR)

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Personal details
FIRST_NAME = os.getenv("FIRST_NAME", "")
LAST_NAME = os.getenv("LAST_NAME", "")
EMAIL = os.getenv("EMAIL", "")
PHONE = os.getenv("PHONE", "")
PASSPORT_NUMBER = os.getenv("PASSPORT_NUMBER", "")
DATE_OF_BIRTH = os.getenv("DATE_OF_BIRTH", "")

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
    page.evaluate(f"""() => {{
        const ta1 = document.querySelector('textarea[name="g-recaptcha-response"]');
        const ta2 = document.querySelector('textarea[name="h-captcha-response"]');
        if (ta1) ta1.value = '{token}';
        if (ta2) ta2.value = '{token}';
    }}""")
    logger.info("CAPTCHA token injected")


def extract_dates(page: Page) -> tuple[list[str], list[str]]:
    """Extract available_dates and fullCapicity_dates from page JS."""
    src = page.content()

    available = []
    full = []

    m = re.search(r"var\s+available_dates\s*=\s*(\[.*?\]);", src)
    if m:
        raw = m.group(1).replace("'", '"')
        import json
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


def fill_form(page: Page):
    """Fill in the visa type selection form."""
    # The BLS form typically has dropdowns that load sequentially.
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
    """Find hCaptcha on page, solve via 2captcha, inject, and submit."""
    # Look for hCaptcha iframe or div
    hcaptcha_el = page.query_selector(".h-captcha, [data-hcaptcha-widget-id]")
    if not hcaptcha_el:
        logger.warning("No hCaptcha element found on page")
        # Try to find any captcha iframe
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

    token = solve_hcaptcha(TWOCAPTCHA_KEY, sitekey, current_url)
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
                # Select first available time
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

    # Fill personal details if form fields are present
    fill_personal_details(page)

    # Solve CAPTCHA if present at this stage
    if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
        if not solve_and_submit_captcha(page):
            logger.error("Failed to solve CAPTCHA during booking")
            return False

    # Submit
    click_submit(page)
    time.sleep(5)

    # Check for success indicators
    src = page.content().lower()
    if any(kw in src for kw in ["success", "confirmed", "booked", "reference number", "confirmation"]):
        return True

    return False


def fill_personal_details(page: Page):
    """Fill personal info fields if they exist on the page."""
    field_map = {
        "#FirstName": FIRST_NAME,
        "#LastName": LAST_NAME,
        "#Email": EMAIL,
        "#Phone": PHONE,
        "#PassportNumber": PASSPORT_NUMBER,
        "#DateOfBirth": DATE_OF_BIRTH,
        "input[name='FirstName']": FIRST_NAME,
        "input[name='LastName']": LAST_NAME,
        "input[name='Email']": EMAIL,
        "input[name='Phone']": PHONE,
        "input[name='ContactNumber']": PHONE,
    }
    for sel, val in field_map.items():
        if not val:
            continue
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(val)
                logger.debug("Filled %s", sel)
        except Exception:
            continue


def take_screenshot(page: Page, name: str):
    """Save a screenshot for debugging."""
    os.makedirs("screenshots", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"screenshots/{name}_{ts}.png"
    page.screenshot(path=path, full_page=True)
    logger.info("Screenshot saved: %s", path)


def monitor_loop(page: Page, browser: Browser):
    """Main monitoring loop."""
    iteration = 0
    while True:
        iteration += 1
        logger.info("── Iteration %d ──", iteration)

        try:
            # Navigate to the appointment page
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            time.sleep(3)

            # Take debug screenshot
            take_screenshot(page, "page_loaded")

            # Log page title and URL
            logger.info("Page: %s | URL: %s", page.title(), page.url)

            # Dump page selectors for debugging (first iteration)
            if iteration == 1:
                dump_form_structure(page)

            # Fill the form
            fill_form(page)
            time.sleep(2)

            take_screenshot(page, "form_filled")

            # Solve CAPTCHA and submit
            if page.query_selector(".h-captcha, iframe[src*='hcaptcha']"):
                logger.info("CAPTCHA detected, solving...")
                if not solve_and_submit_captcha(page):
                    logger.error("CAPTCHA solve failed, retrying next iteration")
                    time.sleep(CHECK_INTERVAL)
                    continue

            # Click submit/book to proceed
            click_submit(page)
            time.sleep(5)

            take_screenshot(page, "after_submit")

            # Check for available dates
            available, full_cap = extract_dates(page)
            logger.info("Available dates: %s", available)
            logger.info("Full capacity dates: %s", full_cap)

            if not available or (len(available) == 1 and available[0] == ""):
                logger.info("No available dates found")
                notify(f"[Iteration {iteration}] No slots available")
                time.sleep(CHECK_INTERVAL)
                continue

            # Filter dates
            good_dates = filter_dates(available, MIN_DATE)
            if not good_dates:
                logger.info("Available dates exist but none match criteria (>= %s)", MIN_DATE)
                time.sleep(CHECK_INTERVAL)
                continue

            # Found matching dates!
            notify(f"🎉 SLOTS FOUND! Dates: {', '.join(good_dates)}")
            take_screenshot(page, "slots_found")

            # Attempt booking
            for target_date in good_dates:
                logger.info("Attempting to book: %s", target_date)
                success = try_book_date(page, target_date)
                if success:
                    notify(f"✅ APPOINTMENT BOOKED for {target_date}!")
                    take_screenshot(page, "booked")
                    return  # Done!
                else:
                    logger.warning("Booking attempt for %s did not confirm success", target_date)
                    take_screenshot(page, f"book_attempt_{target_date}")

            notify("⚠️ Slots were found but booking didn't confirm. Check screenshots!")

        except PWTimeout:
            logger.error("Page load timeout")
            take_screenshot(page, "timeout")
        except Exception as e:
            logger.error("Error in iteration %d: %s", iteration, e, exc_info=True)
            take_screenshot(page, "error")

        time.sleep(CHECK_INTERVAL)


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

    # Check for captcha
    hcap = page.query_selector(".h-captcha")
    if hcap:
        logger.info("hCaptcha found, sitekey=%s", hcap.get_attribute("data-sitekey"))

    iframes = page.query_selector_all("iframe")
    logger.info("Found %d iframes:", len(iframes))
    for f in iframes:
        src = f.get_attribute("src") or ""
        logger.info("  iframe src=%s", src[:100])


def main():
    if not TARGET_URL:
        logger.error("TARGET_URL not set. Copy .env.example to .env and configure it.")
        sys.exit(1)
    if not TWOCAPTCHA_KEY:
        logger.error("TWOCAPTCHA_API_KEY not set. Get one from https://2captcha.com")
        sys.exit(1)

    notify("🚀 BLS Visa Appointment Bot started")
    logger.info("Config: category=%s, location=%s, visa=%s, sub=%s, for=%s",
                CATEGORY, LOCATION, VISA_TYPE, VISA_SUB_TYPE, APPOINTMENT_FOR)
    logger.info("Looking for dates >= %s", MIN_DATE)
    logger.info("Check interval: %ds, Headless: %s", CHECK_INTERVAL, HEADLESS)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
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
            notify("⏹ Bot stopped by user")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
