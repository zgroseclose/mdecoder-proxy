"""
VPN-based IP rotation using PIA's piactl CLI.

For each VIN we:
  1. Pick the next unused PIA region
  2. Connect the VPN (changes the machine's exit IP)
  3. Run the scraper with no proxy (VPN handles routing)
  4. Disconnect before moving to the next VIN

Requires PIA Desktop Client to be installed (provides piactl).
"""

import random
import subprocess
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Set


# Regions to skip — streaming-optimised servers are sometimes less reliable
# for general web traffic. Remove this filter if you want maximum pool size.
_SKIP_SUFFIXES = ("-streaming-optimized",)


class VPNRotator:
    def __init__(self, regions: Optional[List[str]] = None):
        """
        regions: explicit list of PIA region IDs to use.
                 If None, all regions are fetched from piactl on first fetch().
        """
        self._regions: List[str] = regions or []
        self._index: int = 0
        self._exhausted: Set[str] = set()
        self._current_region: Optional[str] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def fetch(self) -> int:
        """
        Populate the region pool from piactl.
        Returns the number of available regions.
        """
        if not self._regions:
            raw = _run(["piactl", "get", "regions"])
            self._regions = [
                r for r in (line.strip() for line in raw.splitlines())
                if r and r != "auto"
                and not any(r.endswith(s) for s in _SKIP_SUFFIXES)
            ]
            random.shuffle(self._regions)
        return len(self._regions)

    @contextmanager
    def connected(self) -> Iterator[str]:
        """
        Context manager: connect to the next region, yield region ID,
        then disconnect on exit (even if an exception is raised).

        Usage:
            with vpn.connected() as region:
                html = scrape(vin)
        """
        region = self._next_region()
        if region is None:
            raise RuntimeError("All VPN regions exhausted — no more IPs to try.")

        self._current_region = region
        try:
            _disconnect_if_needed()
            print(f"  → VPN connecting to {region}...", end=" ", flush=True)
            _run(["piactl", "set", "region", region])
            _run(["piactl", "connect"])
            _wait_for_state("Connected", timeout=45)
            print("connected.")
            yield region
        finally:
            _disconnect_if_needed(quiet=True)
            self._current_region = None

    def mark_exhausted(self, region: str) -> None:
        """Mark a region as blocked/rate-limited so it won't be reused."""
        self._exhausted.add(region)

    @property
    def available_count(self) -> int:
        return len(self._regions) - len(self._exhausted)

    @property
    def total_count(self) -> int:
        return len(self._regions)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _next_region(self) -> Optional[str]:
        for _ in range(len(self._regions)):
            region = self._regions[self._index % len(self._regions)]
            self._index += 1
            if region not in self._exhausted:
                return region
        return None


# ── piactl helpers ─────────────────────────────────────────────────────────────

def _run(cmd: List[str]) -> str:
    """Run a piactl command and return stdout. Raises on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _connection_state() -> str:
    return _run(["piactl", "get", "connectionstate"])


def _disconnect_if_needed(quiet: bool = False) -> None:
    state = _connection_state()
    if state in ("Disconnected", "Disconnecting"):
        return
    if not quiet:
        print("  → VPN disconnecting...", end=" ", flush=True)
    _run(["piactl", "disconnect"])
    _wait_for_state("Disconnected", timeout=20)
    if not quiet:
        print("done.")


def _wait_for_state(target: str, timeout: int = 45) -> None:
    """Poll piactl until the connection state matches target."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _connection_state() == target:
            return
        time.sleep(1)
    raise TimeoutError(
        f"VPN did not reach '{target}' within {timeout}s "
        f"(current: {_connection_state()})"
    )
