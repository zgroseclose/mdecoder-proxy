# mdecoder-proxy

Scrapes VIN decode result pages from [mdecoder.com](https://www.mdecoder.com)
through rotating [floxy.io](https://floxy.io) residential IPs, using a real
Chromium browser (Playwright). Saves raw HTML for downstream parsing by the
garage comparison app.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# fill in FLOXY_USER / FLOXY_PASS from the floxy dashboard
```

## Usage

```bash
# single VIN, solve the captcha yourself when it appears
python mdecoder.py --vin 3MW49FF01P8C98301 --manual-captcha

# file of VINs (one per line, # comments allowed)
python mdecoder.py --file vins.txt --manual-captcha
```

Output HTML is written to `results/<VIN>_<UTC-timestamp>_<sessionid>.html`.

## HTTP server mode

For integration with a separate app (e.g. a containerized webapp that can't
pop a browser itself), run mdecoder-proxy as a local HTTP service:

```bash
python server.py                    # binds 127.0.0.1:8765, headed + manual captcha
python server.py --port 9000
python server.py --headless --no-manual-captcha   # unattended mode
```

The server exposes:

- `GET  /health` → `{"ok": true}` once the browser is up
- `POST /decode/{vin}` → `{"status": "ok", "html": "...", "url": "..."}` on
  success, `{"status": "rate_limited", ...}` if the captcha appears while
  `--no-manual-captcha` is set, or HTTP 502 with `{"status":
  "transport_error", ...}` on navigation failure

Browser work is pinned to a single thread, so requests are serialized —
which matches the manual-captcha UX anyway. The default bind is loopback;
don't expose this port publicly, it drives a visible browser on the host.

### Why `--manual-captcha`?

Every residential IP we've tested from floxy immediately triggers mdecoder's
reCAPTCHA v2 gate — the pools are pre-flagged. `--manual-captcha` opens a
visible Chromium window; when the captcha appears the terminal beeps, you
solve it once in the browser, and the script auto-resumes and saves the
decoded HTML.

Without the flag, the script rotates through `--max-attempts` fresh IPs
catching each captcha as a rate-limit and retrying. In practice that almost
always exhausts the attempt budget without ever getting a clean decode, so
the flag is effectively required.

For unattended batches (e.g. behind a web UI), plug in a captcha-solving
service like CapSolver or 2Captcha — that's a future addition.

## How it works

For each VIN attempt:

1. Spin up a fresh Chromium context with a new floxy session id → new exit
   IP. [playwright-stealth](https://pypi.org/project/playwright-stealth/)
   patches the usual headless-detection vectors.
2. `goto /`, fill the VIN input, submit the form.
3. Server returns a "please wait" holding page with
   `<meta http-equiv="refresh" content="15">`. Chromium honors it; we poll
   the DOM every 2s.
4. If the page text changes to "Solve Captcha" and `--manual-captcha` is
   set, we wait up to 5 minutes for you to click through it.
5. Once the URL matches `/decode/<vin>` and the page body contains the VIN,
   wait briefly for network idle, then save the full HTML.

## Files

- `mdecoder.py` — CLI entry, retry loop, file I/O
- `server.py` — FastAPI HTTP front-end for integration with other apps
- `decoder.py` — single-attempt decode via Playwright
- `proxy.py` — floxy proxy config with rotating session ids
- `.env.example` — template for floxy credentials
