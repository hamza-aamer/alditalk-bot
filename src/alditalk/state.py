"""Small JSON state file: readings, claims, login attempts, heartbeat."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

READING_WINDOW_S = 1800  # consumption rate is estimated over the last 30 min
MAX_READINGS = 30
KEEP_HISTORY_S = 7 * 86400


@dataclass
class State:
    readings: list[list[float]] = field(default_factory=list)  # [epoch, remaining_mb]
    claims: list[list[float]] = field(default_factory=list)  # [epoch, gained_mb]
    claim_attempts: list[float] = field(default_factory=list)
    login_failures: list[float] = field(default_factory=list)
    total_claims: int = 0
    total_claimed_mb: float = 0.0
    last_ok: float | None = None
    last_summary_date: str | None = None
    cap_alert_sent: float | None = None
    login_limit_alert_sent: float | None = None

    path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "path"}
            st = cls(**known)
        except FileNotFoundError:
            st = cls()
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("State file unreadable (%s); starting fresh", exc)
            st = cls()
        st.path = path
        return st

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("path", None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # --------------------------------------------------------------- readings
    def add_reading(self, now: float, remaining_mb: float) -> None:
        if self.readings and remaining_mb > self.readings[-1][1] + 200:
            # Volume went up (claim / new month): consumption series restarts.
            self.readings = []
        self.readings.append([now, remaining_mb])
        self.readings = self.readings[-MAX_READINGS:]

    def consumption_rate(self, now: float) -> float | None:
        """MB per minute over the recent window, or None if unknown."""
        recent = [r for r in self.readings if now - r[0] <= READING_WINDOW_S]
        if len(recent) < 2:
            recent = self.readings[-2:]
        if len(recent) < 2:
            return None
        (t0, r0), (t1, r1) = recent[0], recent[-1]
        if t1 - t0 < 60:
            return None
        return max(0.0, (r0 - r1) / ((t1 - t0) / 60.0))

    # ----------------------------------------------------------------- claims
    def record_claim_attempt(self, now: float) -> None:
        self.claim_attempts.append(now)
        self._prune(now)

    def record_claim(self, now: float, gained_mb: float) -> None:
        self.claims.append([now, gained_mb])
        self.total_claims += 1
        self.total_claimed_mb += gained_mb
        self._prune(now)

    def last_claim_attempt(self) -> float | None:
        return self.claim_attempts[-1] if self.claim_attempts else None

    def claim_attempts_since(self, ts: float) -> int:
        return sum(1 for t in self.claim_attempts if t >= ts)

    def claims_since(self, ts: float) -> list[list[float]]:
        return [c for c in self.claims if c[0] >= ts]

    # ------------------------------------------------------------------ login
    def record_login_failure(self, now: float) -> None:
        self.login_failures.append(now)
        self._prune(now)

    def login_failures_since(self, ts: float) -> list[float]:
        return [t for t in self.login_failures if t >= ts]

    def _prune(self, now: float) -> None:
        cutoff = now - KEEP_HISTORY_S
        self.claim_attempts = [t for t in self.claim_attempts if t >= cutoff]
        self.claims = [c for c in self.claims if c[0] >= cutoff]
        self.login_failures = [t for t in self.login_failures if t >= cutoff]


def write_heartbeat(state_dir: Path, now: float | None = None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "heartbeat").write_text(str(now or time.time()), encoding="utf-8")


def heartbeat_age(state_dir: Path, now: float | None = None) -> float | None:
    try:
        ts = float((state_dir / "heartbeat").read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    return (now or time.time()) - ts
