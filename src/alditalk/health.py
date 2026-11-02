"""Docker healthcheck: healthy if the loop wrote a heartbeat recently."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .state import heartbeat_age

MAX_AGE_S = float(os.environ.get("HEALTH_MAX_AGE_S", "4500"))  # quiet-hour sleeps can reach ~40 min


def main() -> int:
    age = heartbeat_age(Path(os.environ.get("STATE_DIR", "state")))
    if age is None:
        print("no heartbeat yet")
        return 1
    print(f"heartbeat age {age:.0f} s")
    return 0 if age < MAX_AGE_S else 1


if __name__ == "__main__":
    sys.exit(main())
