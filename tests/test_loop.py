import random

import pytest

from alditalk.auth import BadCredentials
from alditalk.humanize import Humanizer
from alditalk.loop import Bot, safe_error
from alditalk.parsing import Usage
from alditalk.portal import ClaimResult
from alditalk.state import State


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.pings = []

    async def send(self, text, silent=False):
        self.messages.append(text)
        return True

    async def ping(self, fail=False):
        self.pings.append(fail)


class FakePortal:
    def __init__(self, remaining, claim_gain=1024.0, burn=0.0, clock=None):
        self.remaining = remaining
        self.claim_gain = claim_gain
        self.burn = burn
        self.clock = clock
        self.clicks = 0
        self.recycles = 0
        self.fail_prepare = 0

    async def prepare(self):
        if self.fail_prepare:
            self.fail_prepare -= 1
            raise RuntimeError("boom\nCall log:\n  - fill('hunter2-secret')")
        return {"route": "reload", "logged_in": False, "browser_started": False}

    async def read_usage(self):
        return Usage(self.remaining, 25 * 1024)

    async def claim(self, dry_run):
        before = Usage(self.remaining, 25 * 1024)
        if dry_run:
            return ClaimResult(False, False, before, None, "1 GB nachbuchen")
        self.clicks += 1
        self.remaining += self.claim_gain
        after = Usage(self.remaining, 25 * 1024)
        return ClaimResult(True, self.claim_gain >= 800, before, after, "1 GB nachbuchen")

    async def idle(self):
        pass

    async def dump_debug(self, label):
        return None

    async def snapshot(self):
        self.snapshots = getattr(self, "snapshots", 0) + 1

    async def recycle(self):
        self.recycles += 1


def make_bot(settings, portal, clock):
    state = State(path=settings.state_dir / "state.json")
    notifier = FakeNotifier()
    bot = Bot(settings, portal, Humanizer(settings, random.Random(7)), state, notifier, clock=clock, sleep=clock.sleep)
    return bot, state, notifier


async def test_no_claim_above_threshold(settings):
    clock = FakeClock()
    portal = FakePortal(5000)
    bot, state, _ = make_bot(settings, portal, clock)
    sleep_s = await bot.cycle()
    assert portal.clicks == 0
    assert sleep_s >= settings.min_interval_s
    assert (settings.state_dir / "heartbeat").exists()


async def test_claims_below_threshold_and_records(settings):
    clock = FakeClock()
    portal = FakePortal(700)
    bot, state, notifier = make_bot(settings, portal, clock)
    await bot.cycle()
    assert portal.clicks == 1
    assert state.total_claims == 1
    assert any("+1 GB claimed" in m for m in notifier.messages)


async def test_claim_waits_random_delay_first(settings):
    clock = FakeClock()
    start = clock.t
    portal = FakePortal(900)  # above the 400 MB floor -> 20-150 s delay
    bot, *_ = make_bot(settings, portal, clock)
    await bot.cycle()
    assert settings.claim_delay_min_s <= clock.t - start <= settings.claim_delay_max_s + 1


async def test_cooldown_blocks_second_claim(settings):
    clock = FakeClock()
    portal = FakePortal(700, claim_gain=0)  # click does nothing
    bot, state, notifier = make_bot(settings, portal, clock)
    await bot.cycle()
    clock.t += 30  # well inside the 180 s cooldown
    await bot.cycle()
    assert portal.clicks == 1
    assert any("didn't increase" in m for m in notifier.messages)


async def test_hourly_cap_stops_runaway_clicking(settings):
    clock = FakeClock()
    portal = FakePortal(700, claim_gain=0)
    bot, state, notifier = make_bot(settings, portal, clock)
    start = clock.t
    while clock.t - start < 3500:
        await bot.cycle()
        clock.t += settings.claim_cooldown_s + 1
    assert portal.clicks == settings.max_claims_per_hour
    assert sum("Claim cap reached" in m for m in notifier.messages) == 1


async def test_dry_run_never_clicks(settings):
    settings.dry_run = True
    clock = FakeClock()
    portal = FakePortal(300)
    bot, state, notifier = make_bot(settings, portal, clock)
    await bot.cycle()
    assert portal.clicks == 0
    assert state.total_claims == 0
    assert any("DRY RUN" in m for m in notifier.messages)


async def test_run_forever_recovers_then_exits_after_too_many_failures(settings):
    settings.max_consecutive_failures = 3
    clock = FakeClock()
    portal = FakePortal(5000)
    portal.fail_prepare = 10
    bot, state, notifier = make_bot(settings, portal, clock)
    with pytest.raises(SystemExit):
        await bot.run_forever()
    assert portal.recycles == 2
    assert all("hunter2" not in m for m in notifier.messages)


async def test_bad_credentials_pause(settings):
    clock = FakeClock()
    portal = FakePortal(5000)
    calls = {"n": 0}

    async def prepare():
        calls["n"] += 1
        if calls["n"] == 1:
            raise BadCredentials("nope")
        raise SystemExit(0)  # stop the loop on the second round

    portal.prepare = prepare
    bot, state, notifier = make_bot(settings, portal, clock)
    start = clock.t
    with pytest.raises(SystemExit):
        await bot.run_forever()
    assert clock.t - start >= settings.bad_credentials_pause_s
    assert any("Login rejected" in m for m in notifier.messages)


def test_safe_error_strips_call_log():
    exc = RuntimeError("Timeout 30000ms exceeded.\nCall log:\n  - fill('my-password')")
    assert "my-password" not in safe_error(exc)
    assert safe_error(exc).startswith("RuntimeError: Timeout")


async def test_history_records_checks_claims_and_phase(settings):
    from alditalk.dashboard import build_payload
    from alditalk.history import History

    clock = FakeClock()
    portal = FakePortal(700)
    state = State(path=settings.state_dir / "state.json")
    hist = History(settings.state_dir, clock=clock)
    bot = Bot(
        settings,
        portal,
        Humanizer(settings, random.Random(3)),
        state,
        FakeNotifier(),
        clock=clock,
        sleep=clock.sleep,
        history=hist,
    )
    await bot.cycle()
    hist.set_phase("sleeping", next_at=clock.t + 100)
    p = build_payload(settings.state_dir, "24h", now=clock.t + 1)
    assert p["stats"]["claims"] == 1
    assert p["checks"][0]["claimed"] == 1
    assert p["checks"][0]["route"] == "reload"
    assert p["status"]["health"] == "ok"


# --- dashboard controls -------------------------------------------------------------------------
from alditalk.control import Control  # noqa: E402


async def test_wait_runs_full_timer_without_commands(settings):
    clock = FakeClock()
    bot, *_ = make_bot(settings, FakePortal(5000), clock)
    start = clock.t
    assert await bot._wait(300) == "timer"
    assert clock.t - start == pytest.approx(300)


async def test_check_now_wakes_the_bot_early(settings):
    clock = FakeClock()
    bot, *_ = make_bot(settings, FakePortal(5000), clock)
    Control(settings.state_dir).request_check()
    start = clock.t
    assert await bot._wait(1800) == "manual"
    assert clock.t - start < 5
    assert not Control(settings.state_dir).snapshot()["check_requested"]  # consumed


async def test_check_now_ignored_right_after_a_check(settings):
    clock = FakeClock()
    bot, *_ = make_bot(settings, FakePortal(5000), clock)
    await bot.cycle()  # just finished a check
    Control(settings.state_dir).request_check()
    assert await bot._wait(60) == "timer"


async def test_pause_holds_until_resume(settings):
    clock = FakeClock()
    bot, *_ = make_bot(settings, FakePortal(5000), clock)
    ctl = Control(settings.state_dir)
    ctl.set_hold(True)
    real_sleep = clock.sleep
    slept = {"n": 0}

    async def sleep_then_resume(s):
        slept["n"] += 1
        if slept["n"] == 50:  # resume after a while
            ctl.set_hold(False)
        await real_sleep(s)

    bot.sleep = sleep_then_resume
    assert await bot._wait(10) == "resume"
    assert slept["n"] >= 50  # stayed paused well past the 10 s timer


async def test_snapshot_taken_each_check(settings):
    clock = FakeClock()
    portal = FakePortal(5000)
    bot, *_ = make_bot(settings, portal, clock)
    await bot.cycle()
    await bot.cycle()
    assert portal.snapshots == 2
