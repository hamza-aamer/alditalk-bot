"""Local read-only dashboard: `python -m alditalk dashboard` → http://localhost:8377

Reads state/history.db, state/status.json and state/state.json. Never writes, never
touches the portal, needs no credentials. Binds to localhost by default.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .control import MANUAL_MIN_GAP_S, Control

log = logging.getLogger(__name__)

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
MAX_SERIES_POINTS = 1500
CHECK_TIMEOUT_S = 420  # cycle_timeout_s default
STALE_GRACE_S = 480


def _ro_connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def health(status: dict[str, Any], last_check_ts: float | None, now: float) -> str:
    """ok | stale | stopped | never."""
    if not status and last_check_ts is None:
        return "never"
    phase = status.get("phase")
    if phase == "stopped":
        return "stopped"
    since = status.get("since") or 0
    next_at = status.get("next_at")
    if phase in ("checking", "claiming"):
        limit = (next_at or since) + CHECK_TIMEOUT_S + 60 if phase == "claiming" else since + CHECK_TIMEOUT_S + 60
        return "ok" if now <= limit else "stale"
    if next_at is not None:
        return "ok" if now <= next_at + STALE_GRACE_S else "stale"
    return "ok" if last_check_ts and now - last_check_ts < 3 * 3600 else "stale"


def _bucket_key(ts: float, tz: ZoneInfo, hourly: bool) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%Y-%m-%dT%H:00") if hourly else dt.strftime("%Y-%m-%d")


def _bucket_keys(start: float, now: float, tz: ZoneInfo, hourly: bool) -> list[str]:
    step = 3600 if hourly else 86400
    keys: list[str] = []
    t = start
    while t <= now + step:
        k = _bucket_key(min(t, now), tz, hourly)
        if not keys or keys[-1] != k:
            keys.append(k)
        t += step / 2  # half steps: never skip a bucket across DST changes
    return keys


def build_payload(
    state_dir: Path, range_key: str = "24h", now: float | None = None, tz_name: str = "Europe/Berlin"
) -> dict[str, Any]:
    now = now or time.time()
    range_s = RANGES.get(range_key, RANGES["24h"])
    start = now - range_s
    tz = ZoneInfo(tz_name)
    hourly = range_key == "24h"

    status = _read_json(state_dir / "status.json")
    state = _read_json(state_dir / "state.json")
    payload: dict[str, Any] = {
        "now": now,
        "range": range_key if range_key in RANGES else "24h",
        "range_start": start,
        "bucket": "hour" if hourly else "day",
        "status": status,
        "current": None,
        "last_check": None,
        "stats": {},
        "totals": {
            "claims": int(state.get("total_claims") or 0),
            "claimed_mb": float(state.get("total_claimed_mb") or 0.0),
        },
        "series": [],
        "claims": [],
        "buckets": [],
        "intervals": [],
        "checks": [],
        "events": [],
        "snapshots": snapshot_info(state_dir),
        "control": Control(state_dir).snapshot(),
    }

    con = _ro_connect(state_dir / "history.db")
    if con is None:
        payload["status"] = {**status, "health": health(status, None, now)}
        return payload
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(checks)")}
        trigger_col = "trigger" if "trigger" in cols else "NULL AS trigger"
        latest_ok = con.execute(
            "SELECT ts, remaining_mb, total_mb, source, rate_mb_min FROM checks WHERE ok=1 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        last = con.execute("SELECT ts, ok, error FROM checks ORDER BY ts DESC LIMIT 1").fetchone()
        if latest_ok:
            payload["current"] = dict(latest_ok)
        if last:
            payload["last_check"] = dict(last)

        checks = [
            dict(r)
            for r in con.execute(
                "SELECT ts, ok, remaining_mb, total_mb, source, rate_mb_min, next_in_s, duration_s, route,"
                f" logged_in, browser_started, claimed, error, {trigger_col} FROM checks WHERE ts >= ? ORDER BY ts",
                (start,),
            )
        ]
        prev = con.execute(
            "SELECT ts, remaining_mb FROM checks WHERE ok=1 AND ts < ? ORDER BY ts DESC LIMIT 1", (start,)
        ).fetchone()
        claims = [
            dict(r)
            for r in con.execute(
                "SELECT ts, before_mb, after_mb, gained_mb, verified, dry_run, button FROM claims"
                " WHERE ts >= ? ORDER BY ts",
                (start,),
            )
        ]
        events = [
            dict(r)
            for r in con.execute(
                "SELECT ts, kind, detail FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT 100", (start,)
            )
        ]
    finally:
        con.close()

    ok_checks = [c for c in checks if c["ok"] and c["remaining_mb"] is not None]

    # Series for the line chart (downsampled evenly if huge).
    series = [[c["ts"], c["remaining_mb"]] for c in ok_checks]
    if len(series) > MAX_SERIES_POINTS:
        step = len(series) / MAX_SERIES_POINTS
        series = [series[int(i * step)] for i in range(MAX_SERIES_POINTS)] + [series[-1]]
    payload["series"] = series

    # Usage per bucket: only decreases count as usage; increases are claims / renewals.
    keys = _bucket_keys(start, now, tz, hourly)
    used = dict.fromkeys(keys, 0.0)
    claimed = dict.fromkeys(keys, 0.0)
    claim_count = dict.fromkeys(keys, 0)
    seq = ([{"ts": prev["ts"], "remaining_mb": prev["remaining_mb"]}] if prev else []) + ok_checks
    total_used = 0.0
    for a, b in zip(seq, seq[1:], strict=False):
        delta = a["remaining_mb"] - b["remaining_mb"]
        if delta > 0:
            k = _bucket_key(b["ts"], tz, hourly)
            if k in used:
                used[k] += delta
            total_used += delta
    real_claims = [c for c in claims if c["verified"] and not c["dry_run"]]
    for c in real_claims:
        k = _bucket_key(c["ts"], tz, hourly)
        if k in claimed:
            claimed[k] += c["gained_mb"] or 0.0
            claim_count[k] += 1
    payload["buckets"] = [
        {"key": k, "used_mb": round(used[k], 1), "claimed_mb": round(claimed[k], 1), "claims": claim_count[k]}
        for k in keys
    ]

    intervals = [c["next_in_s"] for c in ok_checks if c["next_in_s"]]
    payload["intervals"] = intervals
    payload["claims"] = claims
    payload["checks"] = list(reversed(checks[-200:]))
    payload["events"] = events

    hours = max(1e-9, (now - max(start, checks[0]["ts"] if checks else now)) / 3600)
    payload["stats"] = {
        "checks": len(checks),
        "errors": sum(1 for c in checks if not c["ok"]),
        "logins": sum(1 for c in checks if c["logged_in"]),
        "claims": len(real_claims),
        "claim_attempts": sum(1 for c in claims if not c["dry_run"]),
        "claimed_mb": sum(c["gained_mb"] or 0.0 for c in real_claims),
        "used_mb": total_used,
        "used_mb_per_h": total_used / hours if checks else None,
        "median_interval_s": statistics.median(intervals) if intervals else None,
        "min_interval_s": min(intervals) if intervals else None,
        "max_interval_s": max(intervals) if intervals else None,
    }
    payload["status"] = {**status, "health": health(status, last["ts"] if last else None, now)}
    return payload


def snapshot_info(state_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"latest": None, "error": None}
    latest = state_dir / "snapshots" / "latest.jpg"
    if latest.exists():
        out["latest"] = latest.stat().st_mtime
    err = _latest_error_png(state_dir)
    if err:
        out["error"] = {"ts": err.stat().st_mtime, "label": err.stem.split("-", 2)[-1]}
    return out


def _latest_error_png(state_dir: Path) -> Path | None:
    pngs = sorted((state_dir / "debug").glob("2*-*.png"))  # names start with a timestamp
    return pngs[-1] if pngs else None


def apply_control(state_dir: Path, action: str, now: float | None = None) -> tuple[int, str]:
    """Validate and apply a dashboard command. Returns (http status, message)."""
    now = now or time.time()
    ctl = Control(state_dir)
    status = _read_json(state_dir / "status.json")
    if action == "pause":
        ctl.set_hold(True)
        return 200, "Paused. No checks until you resume."
    if action == "resume":
        ctl.set_hold(False)
        return 200, "Resumed. Checking within a few seconds."
    if action != "check":
        return 400, "Unknown action"
    if status.get("phase") in ("checking", "claiming"):
        return 409, "A check is already running."
    if ctl.snapshot()["check_requested"]:
        return 409, "Already requested. Starting within a few seconds."
    con = _ro_connect(state_dir / "history.db")
    last_ts = None
    if con is not None:
        try:
            row = con.execute("SELECT MAX(ts) FROM checks").fetchone()
            last_ts = row[0] if row else None
        finally:
            con.close()
    if last_ts and now - last_ts < MANUAL_MIN_GAP_S:
        wait = MANUAL_MIN_GAP_S - (now - last_ts)
        return 429, f"Last check was {now - last_ts:.0f} s ago. Try again in {wait:.0f} s."
    if status.get("phase") == "hold":
        return 409, "Checks are paused. Resume first."
    ctl.request_check()
    return 202, "Check requested. Starting within a few seconds."


class _Handler(BaseHTTPRequestHandler):
    server_version = "alditalk-dashboard"
    state_dir: Path = Path("state")
    tz_name: str = "Europe/Berlin"
    allowed_hosts: set[str] = set()
    _html: bytes | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if self.allowed_hosts and host not in self.allowed_hosts:  # DNS-rebinding guard
            self._send(403, b"forbidden host", "text/plain; charset=utf-8")
            return False
        return True

    def _send_file(self, path: Path | None, ctype: str) -> None:
        if path is None or not path.is_file():
            self._send(404, b"no snapshot yet", "text/plain; charset=utf-8")
            return
        self._send(200, path.read_bytes(), ctype)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        origin = self.headers.get("Origin")
        if origin and origin.lower().removeprefix("http://") not in self.allowed_hosts:
            self._send(403, b"forbidden origin", "text/plain; charset=utf-8")
            return
        # A custom header can't be sent cross-site without a CORS preflight (which we never allow).
        if self.headers.get("X-Alditalk-Dashboard") != "1" or urlparse(self.path).path != "/api/control":
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1024)
            action = json.loads(self.rfile.read(length) or b"{}").get("action", "")
        except (ValueError, AttributeError):
            self._send(400, b'{"message":"bad request"}', "application/json")
            return
        code, message = apply_control(self.state_dir, str(action))
        log.info("Control %r -> %d %s", action, code, message)
        self._send(code, json.dumps({"ok": code < 300, "message": message}).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if url.path == "/api/snapshot/latest":
            self._send_file(self.state_dir / "snapshots" / "latest.jpg", "image/jpeg")
            return
        if url.path == "/api/snapshot/error":
            self._send_file(_latest_error_png(self.state_dir), "image/png")
            return
        if url.path == "/":
            if _Handler._html is None:
                _Handler._html = (resources.files("alditalk") / "web" / "dashboard.html").read_bytes()
            self._send(200, _Handler._html, "text/html; charset=utf-8")
        elif url.path == "/api/data":
            rng = (parse_qs(url.query).get("range") or ["24h"])[0]
            try:
                data = build_payload(self.state_dir, rng, tz_name=self.tz_name)
                self._send(200, json.dumps(data).encode(), "application/json")
            except Exception as exc:  # noqa: BLE001
                log.exception("payload failed")
                self._send(500, json.dumps({"error": type(exc).__name__}).encode(), "application/json")
        elif url.path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def serve(state_dir: Path | None = None, host: str | None = None, port: int | None = None) -> None:
    state_dir = state_dir or Path(os.environ.get("STATE_DIR", "state"))
    host = host or os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = port or int(os.environ.get("DASHBOARD_PORT", "8377"))
    _Handler.state_dir = state_dir
    _Handler.tz_name = os.environ.get("TIMEZONE", os.environ.get("TZ", "Europe/Berlin")) or "Europe/Berlin"
    _Handler.allowed_hosts = {f"{h}:{port}" for h in ("localhost", "127.0.0.1", "[::1]")} | {
        "localhost",
        "127.0.0.1",
    }
    httpd = ThreadingHTTPServer((host, port), _Handler)
    log.info("Dashboard on http://localhost:%d (reading %s)", port, state_dir)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
