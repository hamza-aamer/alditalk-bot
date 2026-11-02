"""Dashboard → bot commands via tiny files in state/control/ (no sockets, no shared process).

The dashboard writes; the bot polls while it sleeps and deletes what it consumed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

CHECK_FILE = "check-now"
HOLD_FILE = "hold"
MANUAL_MIN_GAP_S = 30.0  # ignore manual requests right after a check


class Control:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir / "control"

    def _path(self, name: str) -> Path:
        return self.dir / name

    def _touch(self, name: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name + ".tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        os.replace(tmp, self._path(name))

    # ----------------------------------------------------------- dashboard side
    def request_check(self) -> None:
        self._touch(CHECK_FILE)

    def set_hold(self, on: bool) -> None:
        if on:
            self._touch(HOLD_FILE)
        else:
            self._path(HOLD_FILE).unlink(missing_ok=True)

    def snapshot(self) -> dict[str, bool]:
        return {"hold": self.on_hold(), "check_requested": self._path(CHECK_FILE).exists()}

    # ----------------------------------------------------------------- bot side
    def take_check_request(self) -> bool:
        try:
            self._path(CHECK_FILE).unlink()
            return True
        except FileNotFoundError:
            return False

    def on_hold(self) -> bool:
        return self._path(HOLD_FILE).exists()
