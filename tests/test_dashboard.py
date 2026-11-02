import json

import pytest

from alditalk.dashboard import build_payload, health
from alditalk.history import History

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def hist(tmp_path):
    clock = Clock(NOW - 3 * 3600)
    h = History(tmp_path, clock=clock)
    yield h, clock, tmp_path
    h.close()


def test_empty_state_dir(tmp_path):
    p = build_payload(tmp_path, "24h", now=NOW)
    assert p["current"] is None
    assert p["status"]["health"] == "never"
    assert p["series"] == []


def test_payload_usage_claims_and_stats(hist):
    h, clock, d = hist
    readings = [3000, 2500, 1100, 900]
    for i, r in enumerate(readings):
        clock.t = NOW - 3 * 3600 + i * 1800
        h.record_check(True, remaining_mb=r, total_mb=51200, source="api", next_in_s=300 + i, logged_in=(i == 0))
    clock.t += 60
    h.record_claim(before_mb=900, after_mb=1924, verified=True, dry_run=False, button="1 GB")
    h.record_check(True, remaining_mb=1924, total_mb=51200, source="api", next_in_s=200, claimed=True)
    clock.t += 300
    h.record_check(False, error="TimeoutError: boom", next_in_s=60)
    h.event("error", "TimeoutError: boom")
    h.set_phase("backoff", next_at=clock.t + 60)
    (d / "state.json").write_text(json.dumps({"total_claims": 4, "total_claimed_mb": 4096}))

    p = build_payload(d, "24h", now=clock.t + 10)
    assert p["current"]["remaining_mb"] == 1924
    assert p["last_check"]["ok"] == 0
    assert p["status"]["health"] == "ok"
    s = p["stats"]
    assert s["checks"] == 6 and s["errors"] == 1 and s["logins"] == 1
    assert s["claims"] == 1 and s["claimed_mb"] == pytest.approx(1024)
    assert s["used_mb"] == pytest.approx(3000 - 900)  # increases (the claim) are not usage
    assert sum(b["used_mb"] for b in p["buckets"]) == pytest.approx(2100, abs=0.5)
    assert sum(b["claimed_mb"] for b in p["buckets"]) == pytest.approx(1024)
    assert p["totals"] == {"claims": 4, "claimed_mb": 4096.0}
    assert len(p["series"]) == 5
    assert p["checks"][0]["ok"] == 0  # newest first
    assert p["events"][0]["kind"] == "error"


def test_range_filters_old_rows(hist):
    h, clock, d = hist
    clock.t = NOW - 3 * 86400
    h.record_check(True, remaining_mb=5000, total_mb=51200)
    clock.t = NOW - 600
    h.record_check(True, remaining_mb=4000, total_mb=51200)
    assert len(build_payload(d, "24h", now=NOW)["series"]) == 1
    assert len(build_payload(d, "7d", now=NOW)["series"]) == 2
    assert build_payload(d, "7d", now=NOW)["bucket"] == "day"


def test_health_states():
    assert health({}, None, NOW) == "never"
    assert health({"phase": "stopped"}, NOW - 5, NOW) == "stopped"
    assert health({"phase": "sleeping", "next_at": NOW + 100}, NOW - 5, NOW) == "ok"
    assert health({"phase": "sleeping", "next_at": NOW - 3600}, NOW - 4000, NOW) == "stale"
    assert health({"phase": "checking", "since": NOW - 100}, NOW - 400, NOW) == "ok"
    assert health({"phase": "checking", "since": NOW - 3600}, NOW - 4000, NOW) == "stale"


def test_history_survives_bad_writes(tmp_path):
    h = History(tmp_path)
    h._db.close()  # simulate a broken connection
    h.record_check(True, remaining_mb=1)  # must not raise
    h.event("x")
