"""
VIN queue backed by SQLite.

Tracks which VINs are pending, in-progress, done, or failed so that runs
are fully resumable — if the process is killed mid-run, just restart and it
picks up where it left off.
"""

import sqlite3
from pathlib import Path
from typing import List, Optional
from datetime import datetime

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vins (
    vin         TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_proxy  TEXT,
    error       TEXT,
    updated_at  TEXT
);
"""


class VINQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def load_from_file(self, path: str) -> int:
        """
        Load VINs from a plain-text file (one VIN per line).
        VINs already in the DB are left untouched (existing status preserved).
        Returns the number of newly inserted VINs.
        """
        vins = []
        with open(path) as f:
            for line in f:
                v = line.strip().upper()
                if v and not v.startswith("#"):
                    vins.append(v)
        return self.add_vins(vins)

    def add_vins(self, vins: List[str]) -> int:
        """Insert VINs that aren't already tracked. Returns newly added count."""
        added = 0
        for vin in vins:
            vin = vin.strip().upper()
            if not vin:
                continue
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO vins (vin) VALUES (?)", (vin,)
            )
            if cur.rowcount:
                added += 1
        self._conn.commit()
        return added

    def next_pending(self) -> Optional[str]:
        """Return the next VIN with status=pending, or None if the queue is empty."""
        row = self._conn.execute(
            "SELECT vin FROM vins WHERE status = ? LIMIT 1", (STATUS_PENDING,)
        ).fetchone()
        return row["vin"] if row else None

    def mark_in_progress(self, vin: str, proxy_str: str = "") -> None:
        self._conn.execute(
            "UPDATE vins SET status=?, last_proxy=?, updated_at=? WHERE vin=?",
            (STATUS_IN_PROGRESS, proxy_str, _now(), vin),
        )
        self._conn.commit()

    def mark_done(self, vin: str) -> None:
        self._conn.execute(
            "UPDATE vins SET status=?, error=NULL, updated_at=? WHERE vin=?",
            (STATUS_DONE, _now(), vin),
        )
        self._conn.commit()

    def mark_failed(self, vin: str, error: str = "") -> None:
        self._conn.execute(
            "UPDATE vins SET status=?, error=?, updated_at=? WHERE vin=?",
            (STATUS_FAILED, error, _now(), vin),
        )
        self._conn.commit()

    def increment_attempts(self, vin: str) -> int:
        """Bump attempt counter and return the new value."""
        self._conn.execute(
            "UPDATE vins SET attempts = attempts + 1, updated_at=? WHERE vin=?",
            (_now(), vin),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT attempts FROM vins WHERE vin=?", (vin,)
        ).fetchone()
        return row["attempts"] if row else 0

    def reset_to_pending(self, vin: str) -> None:
        """Put a VIN back in the queue (used after a proxy rotation retry)."""
        self._conn.execute(
            "UPDATE vins SET status=?, updated_at=? WHERE vin=?",
            (STATUS_PENDING, _now(), vin),
        )
        self._conn.commit()

    def stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM vins GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def pending_vins(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT vin FROM vins WHERE status=?", (STATUS_PENDING,)
        ).fetchall()
        return [r["vin"] for r in rows]

    def close(self) -> None:
        self._conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat()
