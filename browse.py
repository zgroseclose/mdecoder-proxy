#!/usr/bin/env python3
"""Open mdecoder.com through the Floxy proxy for manual testing.

Run from the mdecoder-proxy directory:
    .venv/bin/python browse.py [URL]

Defaults to https://www.mdecoder.com/ if no URL given.
Applies the same stealth and ad-blocking as the real decoder flow.
Press Enter in the terminal to close.
"""
import sys
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from proxy import new_proxy_config
from decoder import _block_ads

load_dotenv()

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.mdecoder.com/"

cfg = new_proxy_config()
print(f"session : {cfg.session_id}")
print(f"proxy   : {cfg.server}")
print(f"url     : {url}")
print()

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context(proxy=cfg.as_playwright())
    context.route("**/*", _block_ads)
    page = context.new_page()
    Stealth().apply_stealth_sync(page)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    print("Browser open — poke around, then press Enter here to close.")
    input()
    print(f"final url : {page.url}")
    browser.close()
