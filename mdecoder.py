"""CLI: decode one or many VINs via rotating floxy residential proxies.

Usage:
    python mdecoder.py --vin 1HGBH41JXMN109186
    python mdecoder.py --file vins.txt
    python mdecoder.py --file vins.txt --max-attempts 25 --output-dir results
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from decoder import DecodeResult, RateLimited, TransportError, decode_once
from proxy import new_proxy_config

log = logging.getLogger("mdecoder")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _read_vins(args: argparse.Namespace) -> list[str]:
    if args.vin:
        return [args.vin.strip().upper()]
    lines = Path(args.file).read_text().splitlines()
    vins = []
    for raw in lines:
        stripped = raw.strip().upper()
        if stripped and not stripped.startswith("#"):
            vins.append(stripped)
    return vins


def _save(result: DecodeResult, output_dir: Path, session_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{result.vin}_{timestamp}_{session_id}.html"
    path = output_dir / filename
    path.write_text(result.html)
    return path


def _decode_with_retries(
    vin: str,
    max_attempts: int,
    output_dir: Path,
    browser,
    manual_captcha: bool,
) -> bool:
    for attempt in range(1, max_attempts + 1):
        cfg = new_proxy_config()
        log.info(
            "VIN %s attempt %d/%d session=%s",
            vin, attempt, max_attempts, cfg.session_id,
        )
        try:
            result = decode_once(
                vin, cfg, browser=browser, manual_captcha=manual_captcha,
            )
        except RateLimited as exc:
            log.warning("  rate limited: %s — rotating IP", exc)
            debug = getattr(exc, "debug_html", None)
            if debug:
                output_dir.mkdir(parents=True, exist_ok=True)
                path = output_dir / f"captcha_{vin}_{cfg.session_id}.html"
                path.write_text(debug)
                log.warning("  saved captcha page: %s", path)
            continue
        except TransportError as exc:
            log.warning("  transport error: %s — rotating IP", exc)
            debug = getattr(exc, "debug_html", None)
            if debug:
                output_dir.mkdir(parents=True, exist_ok=True)
                path = output_dir / f"debug_{vin}_{cfg.session_id}.html"
                path.write_text(debug)
                log.warning("  saved debug: %s", path)
            time.sleep(2)
            continue

        path = _save(result, output_dir, cfg.session_id)
        log.info("  saved %s", path)
        return True

    log.error("VIN %s FAILED after %d attempts", vin, max_attempts)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--vin", help="Single VIN to decode.")
    source.add_argument("--file", help="Path to newline-delimited VIN list.")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=15,
        help="Max proxy rotations per VIN before giving up (default: 15).",
    )
    parser.add_argument(
        "--output-dir",
        default="results",
        help="Directory for saved HTML (default: ./results).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Launch a visible browser (for debugging).",
    )
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help=(
            "When the reCAPTCHA appears, pause and let the user solve it in "
            "the browser window. Implies --headed."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.manual_captcha:
        args.headed = True

    load_dotenv()
    _setup_logging(args.verbose)

    vins = _read_vins(args)
    if not vins:
        log.error("no VINs found")
        return 2

    output_dir = Path(args.output_dir)
    failures = 0

    # One browser process shared across all attempts; each attempt still uses
    # a fresh context, which means fresh cookies + fresh proxy IP.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        try:
            for vin in vins:
                ok = _decode_with_retries(
                    vin, args.max_attempts, output_dir, browser,
                    manual_captcha=args.manual_captcha,
                )
                if not ok:
                    failures += 1
        finally:
            browser.close()

    log.info(
        "done: %d succeeded, %d failed, %d total",
        len(vins) - failures, failures, len(vins),
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
