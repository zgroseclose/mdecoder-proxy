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

# mdecoder renders this page when it has no active decode job for the VIN
# — usually because the original job's TTL expired while the user was
# solving a captcha. Retrying (fresh form submit from /) creates a new
# job and often succeeds.
NOT_FOUND_MARKERS = (
    "vehicle not found",
    "vin not found",
)

# Third-party ad / analytics hosts that mdecoder embeds. Blocking these
# through a Playwright route handler shaves a few seconds off every
# decode — some of these endpoints are slow through a residential proxy
# and we don't need the responses for anything. Matched as substrings
# against the full request URL.
#
# CAREFUL: do NOT add google.com or gstatic.com wholesale — Cloudflare's
# JS challenge and the reCAPTCHA widget load scripts from those origins.
# We whitelist specific paths (recaptcha) in _block_ads before the host
# check so the captcha keeps working.
AD_HOSTS = (
    "googlesyndication.com",
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "googleadservices.com",
    "adservice.google.",
    "pagead2.googlesyndication",
    "facebook.net",
    "facebook.com/tr",
    "connect.facebook.net",
    "adnxs.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "criteo.net",
    "scorecardresearch.com",
    "quantserve.com",
    "moatads.com",
    "amazon-adsystem.com",
    "adsrvr.org",
    "bing.com/bat",
    "hotjar.com",
    "clarity.ms",
    "segment.io",
    "mixpanel.com",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "casalemedia.com",
    "3lift.com",
    "bidswitch.net",
    "smartadserver.com",
    "yieldmo.com",
    "media.net",
    "zedo.com",
    "adform.net",
)

# Resource types worth killing wholesale. Fonts and <video>/<audio> only
# slow things down. We deliberately keep "image" because reCAPTCHA's
# challenge ("select all squares with traffic lights") is image-based —
# blocking images would break manual captcha solves.
_BLOCKED_RESOURCE_TYPES = {"media", "font"}


def _block_ads(route, request) -> None:
    """Playwright route handler: abort ad / analytics / heavy-asset requests.

    Runs for every request the page makes. We keep the logic tight since
    it's on the hot path — early-return on the allow-list, then check
    host substrings, then resource type.
    """
    url = request.url
    # Allow-list: reCAPTCHA must keep loading or manual solves break.
    # Cloudflare's JS challenge also lives on these origins.
    if "recaptcha" in url or "/cdn-cgi/" in url:
        route.continue_()
        return
    if any(host in url for host in AD_HOSTS):
        route.abort()
        return
    if request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
        return
    route.continue_()

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
    # Check not-found BEFORE "done": the not-found page is on /decode/{vin}/
    # and contains the VIN string, so it'd false-positive as "done" otherwise.
    if any(m in lowered for m in NOT_FOUND_MARKERS):
        return "not_found"
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


def _submit_vin(page, vin: str) -> None:
    """Navigate to / and submit the VIN lookup form.

    Used both for the initial submission and to restart the decode after
    a manual captcha is solved — mdecoder drops the original decode job
    when Cloudflare challenges the follow-up request, so the post-solve
    `/decode/{vin}/` page lands on "Vehicle not found, try again later"
    unless we kick off a fresh form submission.
    """
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
        # `no_wait_after=True` is critical: without it, page.click() *also*
        # waits for any resulting navigation, bounded by the click's own
        # timeout. That short-circuits the 60s expect_navigation wrapping
        # it — so under residential-proxy latency (especially right after
        # a Cloudflare challenge clears) the click times out at 15s even
        # though the navigation would finish in 20–30s.
        with page.expect_navigation(
            timeout=60_000, wait_until="domcontentloaded",
        ):
            page.click('button#decode-free', timeout=15_000, no_wait_after=True)
    except PlaywrightTimeoutError as exc:
        raise TransportError(
            f"form interaction failed: {exc}",
            debug_html=_safe_content(page),
        ) from exc


def _decode_with_browser(
    vin: str,
    proxy_cfg: ProxyConfig,
    browser: Browser,
    manual_captcha: bool,
) -> DecodeResult:
    context = browser.new_context(proxy=proxy_cfg.as_playwright())
    # Block ads / trackers / heavy assets before any page in this context
    # issues a request. mdecoder embeds a lot of ad JS that stalls under
    # a residential proxy; killing it early makes decodes noticeably
    # snappier without affecting the captcha flow.
    context.route("**/*", _block_ads)
    try:
        page = context.new_page()
        _STEALTH.apply_stealth_sync(page)
        _submit_vin(page, vin)

        # Chromium will automatically honor the holding page's
        # `<meta http-equiv="refresh" content="15">`. Each refresh is a real
        # navigation. We poll the current DOM until it's a decode result,
        # a captcha, or we time out.
        deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS
        awaiting_human = False
        retried_not_found = False
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

            elif verdict == "not_found":
                # mdecoder has no active decode job for this VIN. Almost
                # always because the job's TTL expired during a captcha
                # wait. Re-submitting the form from / creates a fresh job
                # — and since Cloudflare just validated this session,
                # the submission usually goes straight through without
                # another captcha. Only retry once so we don't loop.
                if retried_not_found:
                    log.warning(
                        "VIN %s still 'not found' after retry — giving up",
                        vin,
                    )
                    return DecodeResult(
                        vin=vin,
                        html=html,
                        url=page.url,
                        status_code=200,
                    )
                retried_not_found = True
                awaiting_human = False
                log.info(
                    "mdecoder returned 'Vehicle not found' for %s — "
                    "resubmitting VIN once",
                    vin,
                )
                _submit_vin(page, vin)
                deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS

            elif awaiting_human:
                # Captcha cleared. Let the site's own flow play out —
                # after the user clicks "Solve Captcha", mdecoder typically
                # navigates to a holding page and then the decode result.
                # Interrupting with a re-submit here wastes the user's
                # captcha solve and tends to trigger a second captcha.
                awaiting_human = False
                deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS
                log.info("captcha cleared — waiting for decode to complete")

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
