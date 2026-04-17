# mdecoder-proxy

Scrapes VIN decode result pages from [mdecoder.com](https://www.mdecoder.com/) by rotating through PIA SOCKS5 proxies to defeat per-IP daily limits. Saves raw result HTML for downstream processing.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Edit `config.yaml`:

```yaml
pia:
  username: YOUR_PIA_USERNAME   # or proxy-specific credentials from PIA dashboard
  password: YOUR_PIA_PASSWORD
```

> **PIA proxy credentials**: PIA sometimes issues separate proxy credentials distinct from your login. Check your PIA web dashboard under **Settings → Proxy** and use those if available.

## Usage

```bash
# Activate the venv first
source .venv/bin/activate

# Decode VINs from a file (one per line)
python -m src.main --vins vins.txt

# Decode VINs passed directly
python -m src.main 1HGBH41JXMN109186 WBA3A5G59ENP26085

# Manual mode — browser window shown, you solve CAPTCHAs
python -m src.main --vins vins.txt --mode manual

# Force headless off (show browser) without full manual mode
python -m src.main --vins vins.txt --headless false
```

## Output

| Path | Description |
|---|---|
| `output/{VIN}.html` | Raw result page HTML for each successfully decoded VIN |
| `state.db` | SQLite queue — tracks status per VIN, fully resumable |
| `failed.txt` | VINs that exhausted all proxy retries |

Runs are resumable — if interrupted, just re-run the same command and it picks up from where it left off.

## How it works

1. Fetches the full PIA server list (~hundreds of IPs) from PIA's public API
2. For each VIN, opens a fresh Chromium context through a new SOCKS5 proxy IP
3. Fills the VIN form with human-like random keystroke delays
4. Waits for mdecoder's 20–30s countdown to complete
5. Detects whether the result is: success / CAPTCHA / IP block / timeout
6. On block/CAPTCHA: marks that proxy exhausted, rotates to next IP, retries
7. Saves the full result page HTML on success

## Updating selectors

If mdecoder changes its HTML and the scraper stops working, update the `SELECTORS` dict in [`src/scraper.py`](src/scraper.py).
