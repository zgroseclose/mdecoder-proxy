"""
Playwright-based scraper for mdecoder.com.

Each call to decode() spins up a fresh browser context (clean cookies, clean
fingerprint) routed through the provided SOCKS5 proxy, submits the VIN form,
waits out mdecoder's countdown timer, and returns the raw result HTML.

playwright-stealth is applied when available to mask headless-browser signals.

SELECTOR NOTES
--------------
mdecoder's HTML structure may change. If the scraper stops finding the input
or button, inspect the live page and update the SELECTORS dict below.
"""

import random
import time
from pathlib import Path
from typing import Optional, Tuple

from playwright.sync_api import (
    sync_playwright,
    Page,
    TimeoutError as PlaywrightTimeout,
)

try:
    from playwright_stealth import stealth_sync
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

from .detector import PageState, detect

MDECODER_URL = "https://www.mdecoder.com/"

# Update these if the site changes its markup.
SELECTORS = {
    # VIN text input — tries several common patterns
    "vin_input": (
        "input[name='vin'], "
        "input#vin, "
        "input[placeholder*='VIN' i], "
        "input[placeholder*='enter vin' i], "
        "form input[type='text']:first-of-type"
    ),
    # Submit / decode button
    "submit": (
        "button[type='submit'], "
        "input[type='submit'], "
        "button:has-text('Decode'), "
        "button:has-text('Check'), "
        "button:has-text('Search')"
    ),
    # Countdown timer shown while mdecoder makes you wait
    "countdown": (
        "[class*='countdown' i], "
        "[id*='countdown' i], "
        "[class*='timer' i], "
        "[id*='timer' i]"
    ),
    # Container that appears once results are ready
    "results": (
        "[class*='result' i], "
        "[id*='result' i], "
        "table.decode, "
        ".vin-data, "
        "#vin-result"
    ),
}

# Realistic Chrome UAs to randomise per session.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]


def decode(
    vin: str,
    config: dict,
    headless: bool = True,
) -> Tuple[PageState, str]:
    """
    Open a fresh browser (VPN must already be connected by the caller),
    decode the VIN on mdecoder.com, and return (PageState, html_content).

    html_content is the full outer HTML of the result page on SUCCESS, or the
    error/captcha page HTML on other states (useful for debugging).
    """
    max_wait_ms = int(config.get("max_wait_seconds", 120)) * 1000
    key_delay = config.get("inter_key_delay_ms", [80, 220])

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport=random.choice(VIEWPORTS),
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            timezone_id="America/Chicago",
            # Accept all cookies so the page doesn't nag us
            accept_downloads=False,
        )
        page = context.new_page()

        # Apply stealth patches if available
        if _STEALTH_AVAILABLE:
            stealth_sync(page)
        else:
            _manual_stealth(page)

        try:
            state, html = _run_decode(page, vin, max_wait_ms, key_delay)
        except PlaywrightTimeout:
            state, html = PageState.TIMEOUT, _safe_html(page)
        except Exception as exc:
            state, html = PageState.UNKNOWN, f"<!-- exception: {exc} -->"
        finally:
            browser.close()

    return state, html


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_decode(page: Page, vin: str, max_wait_ms: int, key_delay: list) -> Tuple[PageState, str]:
    # Navigate to mdecoder homepage
    page.goto(MDECODER_URL, wait_until="domcontentloaded", timeout=30_000)
    _random_pause(0.5, 1.5)

    # Find the VIN input field
    vin_input = page.locator(SELECTORS["vin_input"]).first
    vin_input.wait_for(state="visible", timeout=10_000)
    vin_input.click()
    _random_pause(0.2, 0.5)

    # Type VIN character-by-character with random delays
    for char in vin:
        page.keyboard.type(char)
        time.sleep(random.randint(key_delay[0], key_delay[1]) / 1000)

    _random_pause(0.3, 0.8)

    # Click submit
    submit_btn = page.locator(SELECTORS["submit"]).first
    submit_btn.click()

    # ── Wait strategy ──────────────────────────────────────────────────────
    # mdecoder shows a countdown timer for 20-30s before revealing results.
    # Strategy:
    #   1. Wait for the countdown element to appear (confirms form was accepted)
    #   2. Wait for it to disappear (countdown finished)
    #   3. Wait for network to settle
    # If the countdown selector doesn't match, fall back to a timed networkidle wait.

    countdown_appeared = _wait_for_selector_optional(
        page, SELECTORS["countdown"], timeout_ms=15_000
    )

    if countdown_appeared:
        # Wait for the countdown to go away — use a generous timeout
        try:
            page.locator(SELECTORS["countdown"]).first.wait_for(
                state="hidden", timeout=max_wait_ms
            )
        except PlaywrightTimeout:
            pass  # timed out waiting for countdown — check results anyway

    # Give the results a moment to render after the countdown
    _random_pause(1.0, 2.5)

    # Try to wait for the results container to appear
    _wait_for_selector_optional(page, SELECTORS["results"], timeout_ms=20_000)

    # Final network settle
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeout:
        pass

    state = detect(page)
    return state, page.content()


def _wait_for_selector_optional(page: Page, selector: str, timeout_ms: int) -> bool:
    """Try waiting for a selector; return True if found, False on timeout."""
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeout:
        return False


def _manual_stealth(page: Page) -> None:
    """
    Basic stealth patches when playwright-stealth isn't installed.
    Hides the most obvious headless-browser tells.
    """
    page.add_init_script("""
        // Remove webdriver flag
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        // Spoof plugins count
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        // Spoof languages
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        // Remove headless chrome signals
        window.chrome = { runtime: {} };
    """)


def _safe_html(page: Page) -> str:
    try:
        return page.content()
    except Exception:
        return ""


def _random_pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))
