"""HTTP front-end around `decode_once` for host-side VIN decoding.

Designed to run on the user's desktop (not in Docker), so the Chromium
window launched by Playwright is visible and clickable when reCAPTCHA
appears. A separate backend (e.g. a containerized webapp) calls
`POST /decode/{vin}` and waits — the user solves the captcha once in the
browser window, the server scrapes the decoded HTML, and returns it.

Concurrency: Playwright's sync API is thread-affine, so all browser work is
pinned to a single worker thread. Decode requests are therefore serialized
— which matches the UX anyway (one Chromium window at a time).
"""

from __future__ import annotations

import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import Browser, sync_playwright
from pydantic import BaseModel

from decoder import RateLimited, TransportError, decode_once
from proxy import new_proxy_config

log = logging.getLogger("mdecoder.server")

# Single-threaded executor pins all Playwright calls to one thread — required
# by the sync API. Also naturally serializes requests, which is what we want
# when a human is solving captchas one at a time.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mdecoder-pw")

_pw = None
_browser: Browser | None = None


def _start_browser(headless: bool) -> None:
    global _pw, _browser
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=headless)
    log.info("browser started (headless=%s)", headless)


def _stop_browser() -> None:
    global _pw, _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None


def _do_decode(vin: str, manual_captcha: bool) -> dict:
    assert _browser is not None, "browser not initialized"
    cfg = new_proxy_config()
    log.info("decoding VIN %s session=%s", vin, cfg.session_id)
    try:
        result = decode_once(
            vin, cfg, browser=_browser, manual_captcha=manual_captcha,
        )
    except RateLimited as exc:
        return {
            "status": "rate_limited",
            "message": str(exc),
            "html": exc.debug_html or "",
        }
    except TransportError as exc:
        return {
            "status": "transport_error",
            "message": str(exc),
            "html": exc.debug_html or "",
        }
    return {"status": "ok", "html": result.html, "url": result.url}


class DecodeResponse(BaseModel):
    status: str  # "ok" | "rate_limited" | "transport_error"
    html: str
    url: str | None = None
    message: str | None = None


def create_app(*, headless: bool, manual_captcha: bool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Initialize the browser on the executor thread so sync_playwright
        # lives on the same thread that will use it.
        await _run_on_executor(lambda: _start_browser(headless))
        try:
            yield
        finally:
            await _run_on_executor(_stop_browser)
            _executor.shutdown(wait=True)

    app = FastAPI(title="mdecoder-proxy", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": _browser is not None}

    @app.post("/decode/{vin}", response_model=DecodeResponse)
    async def decode(vin: str):
        vin = vin.strip().upper()
        if len(vin) != 17:
            raise HTTPException(400, f"invalid VIN length: {vin!r}")
        result = await _run_on_executor(lambda: _do_decode(vin, manual_captcha))
        if result["status"] == "transport_error":
            return JSONResponse(status_code=502, content=result)
        return result

    return app


async def _run_on_executor(fn):
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("MDECODER_SERVER_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MDECODER_SERVER_PORT", "8765")),
        help="Bind port (default: 8765).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless (disables manual captcha flow).",
    )
    parser.add_argument(
        "--no-manual-captcha",
        action="store_true",
        help="Treat captcha pages as rate-limited instead of waiting for a human.",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = create_app(
        headless=args.headless,
        manual_captcha=not args.no_manual_captcha,
    )
    # Bind host is configurable but the default is loopback — this server
    # launches a visible browser on the machine it runs on and should not be
    # exposed to the network.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
