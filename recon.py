"""One-off recon: fetch the homepage through a residential IP and dump it,
so we can inspect the real form/URL before spending IPs on the full flow."""

from __future__ import annotations

import sys

import requests
from dotenv import load_dotenv

from proxy import rotating_proxies

load_dotenv()

proxies, session = rotating_proxies()
print(f"session={session}", file=sys.stderr)

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

r = requests.get("https://www.mdecoder.com/", headers=headers, proxies=proxies, timeout=45)
print(f"status={r.status_code} final_url={r.url} bytes={len(r.content)}", file=sys.stderr)
sys.stdout.write(r.text)
