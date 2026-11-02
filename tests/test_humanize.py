import random
import statistics
from datetime import datetime

import pytest

from alditalk.humanize import Humanizer

NOON = datetime(2026, 11, 2, 12, 0)
NIGHT = datetime(2026, 11, 2, 3, 30)


@pytest.fixture
def h(settings):
    return Humanizer(settings, rng=random.Random(1234))


def test_intervals_vary_and_stay_in_bounds(h, settings):
    vals = [h.next_interval(3000, None, NOON) for _ in range(300)]
    assert len({round(v, 3) for v in vals}) > 250  # not a fixed cadence
    assert min(vals) >= settings.min_interval_s
    assert max(vals) <= settings.poll_base_mid_s * 2.6 + 1
    assert statistics.pstdev(vals) > 20


def test_tier_bases_scale_with_remaining(h):
    high = statistics.median(h.next_interval(20_000, None, NOON) for _ in range(200))
    low = statistics.median(h.next_interval(1500, None, NOON) for _ in range(200))
    assert high > 3 * low


def test_never_sleeps_past_projected_crossing(h, settings):
    # 2000 MB left, 50 MB/min -> 976 MB headroom -> ~19.5 min to the 1 GB line
    horizon = (2000 - settings.threshold_mb) / 50 * 60
    for _ in range(300):
        assert h.next_interval(2000, 50.0, NOON) <= 0.7 * horizon + 1e-6


def test_slow_consumption_allows_long_sleeps(h, settings):
    settings.keep_session_alive = False
    vals = [h.next_interval(20_000, 0.2, NOON) for _ in range(200)]
    assert statistics.median(vals) > 0.5 * settings.poll_cap_high_s


def test_quiet_hours_stretch_interval(h, settings):
    night = [h.next_interval(3000, 0.5, NIGHT) for _ in range(100)]
    assert min(night) >= settings.quiet_min_s


def test_quiet_hours_ignored_when_data_is_used_fast(h, settings):
    night = [h.next_interval(3000, 40.0, NIGHT) for _ in range(100)]
    assert max(night) < settings.quiet_min_s


def test_quiet_hours_wraparound(settings):
    settings.quiet_start_hour, settings.quiet_end_hour = 23, 6
    h = Humanizer(settings, rng=random.Random(1))
    assert h.in_quiet_hours(datetime(2026, 1, 1, 23, 30))
    assert h.in_quiet_hours(datetime(2026, 1, 1, 2, 0))
    assert not h.in_quiet_hours(datetime(2026, 1, 1, 12, 0))


def test_under_threshold_checks_soon(h, settings):
    for _ in range(100):
        assert h.next_interval(500, 10.0, NIGHT) <= settings.poll_cap_low_s


def test_claim_delay_immediate_when_low(h, settings):
    for _ in range(100):
        assert h.claim_delay(settings.claim_delay_floor_mb - 1, None) <= 6.0


def test_claim_delay_random_when_comfortable(h, settings):
    vals = [h.claim_delay(900, None) for _ in range(200)]
    assert min(vals) >= settings.claim_delay_min_s
    assert max(vals) <= settings.claim_delay_max_s
    assert statistics.pstdev(vals) > 10


def test_claim_delay_shrinks_with_fast_consumption(h):
    # 900 MB, floor 400, 200 MB/min -> 150 s to the floor -> wait at most half of that
    for _ in range(100):
        assert h.claim_delay(900, 200.0) <= 75.0 + 1e-6


def test_backoff_grows_and_caps(h):
    assert h.backoff(1) < h.backoff(4) * 1.5
    assert all(h.backoff(20) <= 900 * 1.3 for _ in range(50))


def test_route_mix(h):
    picks = [h.pick_route() for _ in range(1000)]
    assert {"reload", "goto", "via_bookings"} == set(picks)
    assert picks.count("reload") > picks.count("goto") > picks.count("via_bookings")


def test_keepalive_caps_daytime_gaps(h, settings):
    vals = [h.next_interval(20_000, 0.2, NOON) for _ in range(300)]
    assert max(vals) <= settings.session_keepalive_max_s
    assert min(vals) >= settings.min_interval_s
    assert len({round(v) for v in vals}) > 100  # still randomized


def test_keepalive_does_not_shorten_quiet_hours(h, settings):
    vals = [h.next_interval(20_000, 0.2, NIGHT) for _ in range(100)]
    assert min(vals) >= settings.quiet_min_s
