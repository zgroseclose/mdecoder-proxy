"""Single-attempt mdecoder.com VIN decode via Playwright.

One attempt = one fresh residential IP = one fresh browser context. A real
Chromium instance is required because Cloudflare's JS Detection
(cdn-cgi/challenge-platform/scripts/jsd/main.js) fingerprints the browser
and serves a "Solve Captcha" page on follow-up requests from clients that
don't execute JS.

Flow:
  1. New browser context with the floxy proxy bound to it.
  2. Navigate to /, fill the VIN input, submit.
  3. Chromium follows the server's meta-refresh (every 15s) automatically.
     We wait until the page is no longer the "please wait" holding page.
  4. Check for "Solve Captcha" → RateLimited. Otherwise return the HTML.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass

from playwright.sync_api import (
    Browser,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from playwright_stealth import Stealth

from proxy import ProxyConfig

# Patches navigator.webdriver, chrome runtime, plugin shapes, etc. so
# Cloudflare's JS Detection doesn't flag the page as a headless bot.
_STEALTH = Stealth()

BASE_URL = "https://www.mdecoder.com"

LIMIT_MARKERS = (
    "solve captcha",
    "daily limit",
    "limit reached",
    "too many requests",
    "try again tomorrow",
    "exceeded",
)

HOLDING_MARKERS = (
    "data will be available",
    "please refresh",
)

# Total time we'll keep the page open waiting for the holding state to clear.
# The server promises ~30s; allow generous headroom across a few meta-refreshes.
DECODE_TIMEOUT_SECONDS = 90
# How often we re-check the DOM while waiting.
POLL_INTERVAL_SECONDS = 2
# When the user is solving a captcha by hand, give them up to five minutes.
MANUAL_CAPTCHA_TIMEOUT_SECONDS = 300

log = logging.getLogger(__name__)


class DecodeError(Exception):
    """Base class for decode failures."""


class RateLimited(DecodeError):
    """Site returned a captcha / limit-reached page for this IP."""

    def __init__(self, message: str, debug_html: str | None = None):
        super().__init__(message)
        self.debug_html = debug_html


class TransportError(DecodeError):
    """Navigation / browser / network failure."""

    def __init__(self, message: str, debug_html: str | None = None):
        super().__init__(message)
        self.debug_html = debug_html


@dataclass
class DecodeResult:
    vin: str
    html: str
    url: str
    status_code: int  # always 200 for Playwright successes; kept for compat


def _classify(html: str, url: str, vin: str) -> str:
    lowered = html.lower()
    if any(m in lowered for m in LIMIT_MARKERS):
        return "captcha"
    if any(m in lowered for m in HOLDING_MARKERS):
        return "holding"
    # Positive result signals: on the /decode/ path AND the page mentions
    # the VIN somewhere. Anything else is an incomplete / intermediate state
    # we shouldn't declare "done" on.
    if "/decode/" in url and vin.lower() in lowered:
        return "done"
    return "pending"


def decode_once(
    vin: str,
    proxy_cfg: ProxyConfig,
    *,
    browser: Browser | None = None,
    headless: bool = True,
    manual_captcha: bool = False,
) -> DecodeResult:
    """Run one decode attempt through the given proxy.

    Raises `RateLimited` if the page becomes a captcha / limit page (unless
    `manual_captcha=True`, in which case we wait for the human to solve it),
    or `TransportError` on navigation / timeout failures.

    Pass an existing `browser` to reuse a long-running Chromium instance
    across attempts (each attempt still uses a fresh context). If omitted,
    a browser is launched and closed inside this call.
    """
    if browser is not None:
        return _decode_with_browser(vin, proxy_cfg, browser, manual_captcha)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            return _decode_with_browser(vin, proxy_cfg, browser, manual_captcha)
        finally:
            browser.close()


def _decode_with_browser(
    vin: str,
    proxy_cfg: ProxyConfig,
    browser: Browser,
    manual_captcha: bool,
) -> DecodeResult:
    context = browser.new_context(proxy=proxy_cfg.as_playwright())
    try:
        page = context.new_page()
        _STEALTH.apply_stealth_sync(page)
        try:
            # `domcontentloaded` instead of `load`: load waits for every ad
            # tracker / analytics bundle and routinely hangs through a
            # residential proxy. We only need the form to be present.
            page.goto(BASE_URL + "/", timeout=60_000, wait_until="domcontentloaded")
            page.wait_for_selector('input[name="vin"]', timeout=30_000)
        except PlaywrightTimeoutError as exc:
            raise TransportError(f"goto /: {exc}") from exc

        try:
            page.fill('input[name="vin"]', vin, timeout=15_000)
            with page.expect_navigation(
                timeout=60_000, wait_until="domcontentloaded",
            ):
                page.click('button#decode-free', timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise TransportError(
                f"form interaction failed: {exc}",
                debug_html=_safe_content(page),
            ) from exc

        # Chromium will automatically honor the holding page's
        # `<meta http-equiv="refresh" content="15">`. Each refresh is a real
        # navigation. We poll the current DOM until it's a decode result,
        # a captcha, or we time out.
        deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS
        awaiting_human = False
        while True:
            html = _safe_content(page)
            verdict = _classify(html, page.url, vin)

            if verdict == "captcha":
                if not manual_captcha:
                    raise RateLimited(
                        f"captcha/limit page for VIN {vin}",
                        debug_html=html,
                    )
                if not awaiting_human:
                    awaiting_human = True
                    # Bell character pings the terminal; combined with the log
                    # line the user should notice the browser needs input.
                    sys.stdout.write("\a")
                    sys.stdout.flush()
                    log.warning(
                        "CAPTCHA for VIN %s — solve it in the browser window "
                        "(waiting up to %ds)",
                        vin, MANUAL_CAPTCHA_TIMEOUT_SECONDS,
                    )
                    deadline = time.monotonic() + MANUAL_CAPTCHA_TIMEOUT_SECONDS
                # fall through to the sleep + re-poll below

            elif verdict == "done":
                # We hit "done" as soon as the head parses (title contains the
                # VIN), but the body is often still streaming. Wait for the
                # network to idle, then re-capture so we save the full DOM.
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeoutError:
                    pass  # trackers may never idle; take what we have
                html = _safe_content(page)
                return DecodeResult(
                    vin=vin,
                    html=html,
                    url=page.url,
                    status_code=200,
                )

            elif awaiting_human:
                # Human solved the captcha; we're back to the normal holding
                # or post-submit flow. Reset the decode deadline.
                awaiting_human = False
                deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS
                log.info("captcha cleared — continuing decode")

            if time.monotonic() >= deadline:
                raise TransportError(
                    f"decode still {verdict} after timeout",
                    debug_html=html,
                )
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        context.close()


def _safe_content(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""
