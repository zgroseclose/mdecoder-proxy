"""
mdecoder-proxy — CLI entrypoint

Usage:
    python -m src.main --vins vins.txt
    python -m src.main --vins vins.txt --mode manual
    python -m src.main 1HGBH41JXMN109186 WBA3A5G59ENP26085
    python -m src.main --config /path/to/config.yaml --vins vins.txt

The program will:
  1. Load available PIA VPN regions via piactl
  2. For each VIN: connect VPN to next region, scrape mdecoder.com, disconnect
  3. Save raw result HTML to output/{VIN}.html on success
  4. Rotate to the next region on CAPTCHA/block and retry
  5. Write permanently failed VINs to failed.txt
"""

import argparse
from pathlib import Path

import yaml

from .proxy import VPNRotator
from .queue import VINQueue
from .scraper import decode
from .detector import PageState


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(config: dict, vins: list[str], vin_file: str | None, mode: str, headless: bool | None):
    # ── Setup ──────────────────────────────────────────────────────────────────
    output_dir = Path(config.get("output_dir", "./output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = config.get("db_path", "./state.db")
    scraper_cfg = config.get("scraper", {})
    max_retries = scraper_cfg.get("max_retries", 8)

    if headless is None:
        headless = scraper_cfg.get("headless", True)

    if mode == "manual":
        headless = False
        print("[manual mode] Browser window will be shown on CAPTCHA/block.")

    # ── VPN rotator ────────────────────────────────────────────────────────────
    vpn = VPNRotator()
    print("Loading PIA VPN regions...", end=" ", flush=True)
    count = vpn.fetch()
    print(f"{count} regions loaded.")

    # ── VIN queue ──────────────────────────────────────────────────────────────
    queue = VINQueue(db_path)

    if vin_file:
        added = queue.load_from_file(vin_file)
        if added:
            print(f"Loaded {added} new VIN(s) from {vin_file}.")

    if vins:
        added = queue.add_vins(vins)
        if added:
            print(f"Added {added} new VIN(s) from command line.")

    stats = queue.stats()
    pending = stats.get("pending", 0)
    if pending == 0:
        print("No pending VINs. Exiting.")
        return

    print(f"Queue: {pending} pending, {stats.get('done', 0)} done, {stats.get('failed', 0)} failed.\n")

    # ── Main loop ──────────────────────────────────────────────────────────────
    failed_log = Path("failed.txt")

    while True:
        vin = queue.next_pending()
        if vin is None:
            break

        output_path = output_dir / f"{vin}.html"
        if output_path.exists():
            print(f"[{vin}] Output already exists, marking done.")
            queue.mark_done(vin)
            continue

        print(f"\n{'─'*60}")
        print(f"[{vin}] Starting decode...")

        attempt = 0
        success = False

        while attempt < max_retries:
            attempt = queue.increment_attempts(vin)

            if vpn.available_count == 0:
                print(f"[{vin}] All VPN regions exhausted. Giving up.")
                queue.mark_failed(vin, "all_regions_exhausted")
                _log_failed(failed_log, vin, "all_regions_exhausted")
                break

            try:
                with vpn.connected() as region:
                    print(f"[{vin}] Attempt {attempt}/{max_retries} via {region}")
                    queue.mark_in_progress(vin, region)

                    state, html = decode(vin, scraper_cfg, headless=headless)

                    # Save HTML on first attempt for debugging
                    if attempt == 1 and html:
                        debug_path = output_dir / f"{vin}_debug.html"
                        debug_path.write_text(html, encoding="utf-8")
                        print(f"[{vin}] Debug HTML → {debug_path}")

                    if state == PageState.SUCCESS:
                        output_path.write_text(html, encoding="utf-8")
                        queue.mark_done(vin)
                        print(f"[{vin}] SUCCESS — saved to {output_path}")
                        success = True

                    elif state == PageState.CAPTCHA:
                        if mode == "manual":
                            success = _handle_captcha_manual(vin, region, scraper_cfg, output_path, queue, headless)
                        else:
                            print(f"[{vin}] CAPTCHA detected — rotating region.")
                            vpn.mark_exhausted(region)
                            queue.reset_to_pending(vin)

                    elif state == PageState.BLOCKED:
                        print(f"[{vin}] IP blocked/limit reached — rotating region.")
                        vpn.mark_exhausted(region)
                        queue.reset_to_pending(vin)

                    elif state == PageState.TIMEOUT:
                        print(f"[{vin}] Timed out — rotating region.")
                        queue.reset_to_pending(vin)

                    else:
                        print(f"[{vin}] Unknown page state — rotating region.")
                        queue.reset_to_pending(vin)

            except TimeoutError as e:
                print(f"[{vin}] VPN connect timed out ({e}) — trying next region.")
                queue.reset_to_pending(vin)

            if success:
                break

        if not success and attempt >= max_retries:
            print(f"[{vin}] FAILED after {attempt} attempts.")
            queue.mark_failed(vin, f"max_retries_reached ({attempt})")
            _log_failed(failed_log, vin, "max_retries_reached")

    # ── Summary ────────────────────────────────────────────────────────────────
    final = queue.stats()
    print(f"\n{'═'*60}")
    print("Run complete.")
    print(f"  Done:    {final.get('done', 0)}")
    print(f"  Failed:  {final.get('failed', 0)}")
    print(f"  Pending: {final.get('pending', 0)}")
    if final.get("failed", 0):
        print(f"  Failed VINs logged to: {failed_log}")


def _handle_captcha_manual(vin, region, scraper_cfg, output_path, queue, headless) -> bool:
    """
    Manual CAPTCHA mode: the browser window is already visible. Ask the user
    if they solved it, then re-run once more with the same region/IP to
    capture the result HTML.
    """
    print(f"\n[{vin}] CAPTCHA detected via {region}")
    print("  The browser window should be visible. Solve the CAPTCHA there.")
    answer = input("  Did the results load successfully? [y/n]: ").strip().lower()
    if answer == "y":
        print(f"[{vin}] Re-running to capture results HTML...")
        state, html = decode(vin, scraper_cfg, headless=False)
        if state == PageState.SUCCESS:
            output_path.write_text(html, encoding="utf-8")
            queue.mark_done(vin)
            print(f"[{vin}] SUCCESS — saved to {output_path}")
            return True
        print(f"[{vin}] Still not a success page (state={state.name}). Rotating region.")
    return False


def _log_failed(path: Path, vin: str, reason: str) -> None:
    with open(path, "a") as f:
        f.write(f"{vin}\t{reason}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape VIN decode results from mdecoder.com via PIA VPN region rotation."
    )
    parser.add_argument("--vins", metavar="FILE", help="Text file with one VIN per line.")
    parser.add_argument("vin_args", nargs="*", metavar="VIN", help="VINs to decode.")
    parser.add_argument("--config", default="config.yaml", metavar="FILE")
    parser.add_argument("--mode", choices=["auto", "manual"], default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None)

    args = parser.parse_args()

    if not args.vins and not args.vin_args:
        parser.error("Provide VINs via --vins FILE or as positional arguments.")

    cfg = load_config(args.config)
    mode = args.mode or cfg.get("mode", "auto")

    run(
        config=cfg,
        vins=args.vin_args,
        vin_file=args.vins,
        mode=mode,
        headless=args.headless,
    )


if __name__ == "__main__":
    main()
