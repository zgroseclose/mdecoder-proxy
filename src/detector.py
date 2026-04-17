"""
Page state detection — determines whether a page loaded after VIN submission
represents a successful result, a CAPTCHA challenge, an IP block, or something
unknown/timed-out.

All checks are done against the live Playwright page object so we can inspect
both the DOM and the URL without re-fetching.
"""

from enum import Enum, auto
from playwright.sync_api import Page


class PageState(Enum):
    SUCCESS = auto()   # Vehicle data present on page
    CAPTCHA = auto()   # CAPTCHA challenge detected
    BLOCKED = auto()   # IP daily limit or explicit block message
    TIMEOUT = auto()   # Waited too long, no recognisable outcome
    UNKNOWN = auto()   # Page loaded but we can't classify it


# ── Text fragments that indicate an IP/daily-limit block ──────────────────────
BLOCK_PHRASES = [
    "daily limit",
    "limit reached",
    "too many requests",
    "try again tomorrow",
    "try again later",
    "you have exceeded",
    "access denied",
    "your ip",
    "ip address has been",
    "rate limit",
]

# ── Text/URL fragments that indicate a successful decode ──────────────────────
SUCCESS_PHRASES = [
    "make",
    "model",
    "year",
    "engine",
    "body style",
    "transmission",
    "country of origin",
    "plant",
    "vehicle identification",
]

# mdecoder puts decoded data in a table; the URL often changes to include the VIN
SUCCESS_URL_PATTERN = "/decode/"


def detect(page: Page) -> PageState:
    """
    Inspect the current page and return a PageState.
    Call this after waiting for the results to load.
    """
    url = page.url.lower()

    # 1. CAPTCHA — look for common captcha iframe/element signatures
    if _has_captcha(page):
        return PageState.CAPTCHA

    # 2. Blocked — look for block/limit phrases in visible text
    body_text = _body_text(page)
    if _contains_any(body_text, BLOCK_PHRASES):
        return PageState.BLOCKED

    # 3. Success — URL contains decode path OR page has enough result phrases
    url_hit = SUCCESS_URL_PATTERN in url
    phrase_hits = sum(1 for p in SUCCESS_PHRASES if p in body_text)
    if url_hit or phrase_hits >= 3:
        return PageState.SUCCESS

    return PageState.UNKNOWN


def _has_captcha(page: Page) -> bool:
    """Check for hCaptcha, reCAPTCHA, Cloudflare, etc."""
    # iframe src patterns
    captcha_frame_urls = [
        "hcaptcha.com",
        "recaptcha",
        "challenges.cloudflare.com",
        "captcha",
    ]
    for frame in page.frames:
        frame_url = frame.url.lower()
        if any(p in frame_url for p in captcha_frame_urls):
            return True

    # DOM elements
    captcha_selectors = [
        ".h-captcha",
        ".g-recaptcha",
        "#cf-challenge-running",
        "[data-sitekey]",
        "iframe[src*='captcha']",
        "iframe[src*='hcaptcha']",
        "iframe[src*='recaptcha']",
    ]
    for sel in captcha_selectors:
        if page.locator(sel).count() > 0:
            return True

    # Cloudflare "Checking your browser" text
    if "checking your browser" in _body_text(page):
        return True

    return False


def _body_text(page: Page) -> str:
    """Return all visible text from the page body, lowercased."""
    try:
        return page.inner_text("body").lower()
    except Exception:
        return page.content().lower()


def _contains_any(text: str, phrases: list) -> bool:
    return any(p in text for p in phrases)
