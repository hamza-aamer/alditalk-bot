"""Append-only history (SQLite) + a live status file, both read by the dashboard.

The bot is the only writer. The dashboard opens the database read-only.
Nothing personal is stored: volumes, timings, and error texts only.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEEP_DAYS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    remaining_mb REAL,
    total_mb REAL,
    source TEXT,
    rate_mb_min REAL,
    next_in_s REAL,
    duration_s REAL,
    route TEXT,
    logged_in INTEGER NOT NULL DEFAULT 0,
    browser_started INTEGER NOT NULL DEFAULT 0,
    claimed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    trigger TEXT
);
CREATE INDEX IF NOT EXISTS checks_ts ON checks (ts);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    before_mb REAL,
    after_mb REAL,
    gained_mb REAL,
    verified INTEGER NOT NULL,
    dry_run INTEGER NOT NULL,
    button TEXT
);
CREATE INDEX IF NOT EXISTS claims_ts ON claims (ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


class History:
    """Writer side. Every method swallows errors: history must never break the bot."""

    def __init__(self, state_dir: Path, clock: Any = time.time) -> None:
        self.state_dir = state_dir
        self.db_path = state_dir / "history.db"
        self.status_path = state_dir / "status.json"
        self.clock = clock
        state_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=10)
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(checks)")}
        if "trigger" not in cols:  # migrate databases from before manual checks existed
            self._db.execute("ALTER TABLE checks ADD COLUMN trigger TEXT")
        self._db.commit()
        self.prune()

    def _exec(self, sql: str, args: tuple[Any, ...]) -> None:
        try:
            self._db.execute(sql, args)
            self._db.commit()
        except sqlite3.Error as exc:
            log.warning("History write failed: %s", exc)

    def record_check(
        self,
        ok: bool,
        *,
        remaining_mb: float | None = None,
        total_mb: float | None = None,
        source: str | None = None,
        rate_mb_min: float | None = None,
        next_in_s: float | None = None,
        duration_s: float | None = None,
        route: str | None = None,
        logged_in: bool = False,
        browser_started: bool = False,
        claimed: bool = False,
        error: str | None = None,
        trigger: str = "auto",
    ) -> None:
        self._exec(
            "INSERT INTO checks (ts, ok, remaining_mb, total_mb, source, rate_mb_min, next_in_s, duration_s,"
            " route, logged_in, browser_started, claimed, error, trigger) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.clock(),
                int(ok),
                remaining_mb,
                total_mb,
                source,
                rate_mb_min,
                next_in_s,
                duration_s,
                route,
                int(logged_in),
                int(browser_started),
                int(claimed),
                error,
                trigger,
            ),
        )

    def record_claim(
        self,
        *,
        before_mb: float,
        after_mb: float | None,
        verified: bool,
        dry_run: bool,
        button: str = "",
    ) -> None:
        gained = (after_mb - before_mb) if after_mb is not None else None
        self._exec(
            "INSERT INTO claims (ts, before_mb, after_mb, gained_mb, verified, dry_run, button) VALUES (?,?,?,?,?,?,?)",
            (self.clock(), before_mb, after_mb, gained, int(verified), int(dry_run), button[:80]),
        )

    def event(self, kind: str, detail: str = "") -> None:
        self._exec("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)", (self.clock(), kind, detail[:500]))

    def set_phase(self, phase: str, next_at: float | None = None, **extra: Any) -> None:
        """Live status for the dashboard: checking / claiming / sleeping / backoff / paused / stopped."""
        data = {"phase": phase, "since": self.clock(), "next_at": next_at, **extra}
        try:
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError as exc:
            log.debug("Status write failed: %s", exc)

    def prune(self) -> None:
        cutoff = self.clock() - KEEP_DAYS * 86400
        for table in ("checks", "claims", "events"):
            self._exec(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))  # noqa: S608 - fixed table names

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass
