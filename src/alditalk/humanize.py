"""All randomness lives here: timing, typing, mouse movement, navigation.

Rule: randomize *behaviour*, keep *identity* (profile, viewport, IP) stable.
Every method takes its randomness from `self.rng`, so tests can seed it.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Humanizer:
    def __init__(self, settings: Settings, rng: random.Random | None = None) -> None:
        self.s = settings
        self.rng = rng or random.Random()
        self._mouse = (
            self.rng.uniform(200, 900),
            self.rng.uniform(150, 600),
        )

    # ------------------------------------------------------------------ timing
    def jitter(self, base: float, sigma: float = 0.35, lo: float = 0.55, hi: float = 2.6) -> float:
        """Log-normal jitter: mostly near `base`, occasionally much longer."""
        return base * clamp(math.exp(self.rng.gauss(0.0, sigma)), lo, hi)

    def in_quiet_hours(self, now: datetime) -> bool:
        start, end, h = self.s.quiet_start_hour, self.s.quiet_end_hour, now.hour
        if start == end:
            return False
        if start < end:
            return start <= h < end
        return h >= start or h < end  # window wraps midnight

    def tier(self, remaining_mb: float) -> tuple[float, float]:
        s = self.s
        if remaining_mb > s.tier_high_mb:
            return s.poll_base_high_s, s.poll_cap_high_s
        if remaining_mb > s.tier_mid_mb:
            return s.poll_base_mid_s, s.poll_cap_mid_s
        return s.poll_base_low_s, s.poll_cap_low_s

    def next_interval(self, remaining_mb: float, rate_mb_min: float | None, now: datetime) -> float:
        """Seconds until the next check.

        Consumption-aware: with a known rate, aim to look again at roughly half
        the projected time until the 1 GB line is crossed (never past 70% of it).
        """
        s = self.s
        headroom = remaining_mb - s.threshold_mb
        if headroom <= 0:
            # Already under the line (cooldown / failed claim): look again soon.
            return clamp(self.jitter(s.poll_base_low_s, sigma=0.25), s.min_interval_s, s.poll_cap_low_s)

        base, cap = self.tier(remaining_mb)
        horizon = math.inf
        if rate_mb_min is not None and rate_mb_min > 0.01:
            horizon = headroom / rate_mb_min * 60.0

        nominal = base if rate_mb_min is None else min(cap, 0.5 * horizon)
        interval = min(self.jitter(nominal), 1.5 * cap)  # outliers allowed, but bounded

        slow = rate_mb_min is not None and rate_mb_min < 1.0
        if remaining_mb > s.tier_high_mb and (rate_mb_min is None or slow) and self.rng.random() < 1 / 15:
            interval += self.rng.uniform(600, 1800)  # occasional "away from phone" gap

        quiet_ok = (rate_mb_min is not None and rate_mb_min < s.quiet_busy_rate_mb_min) or (
            remaining_mb > s.tier_mid_mb and (rate_mb_min is None or rate_mb_min < s.quiet_busy_rate_mb_min)
        )
        if self.in_quiet_hours(now) and quiet_ok:
            interval = max(interval, self.rng.uniform(s.quiet_min_s, s.quiet_max_s))
        elif s.keep_session_alive:
            # Look in before the portal's idle logout, so no new login is needed.
            interval = min(interval, self.rng.uniform(s.session_keepalive_min_s, s.session_keepalive_max_s))

        if math.isfinite(horizon):
            interval = min(interval, 0.7 * horizon)
        return max(s.min_interval_s, interval)

    def claim_delay(self, remaining_mb: float, rate_mb_min: float | None) -> float:
        """Don't click the instant we cross 1 GB, unless we're running low."""
        s = self.s
        if remaining_mb <= s.claim_delay_floor_mb:
            return self.rng.uniform(1.5, 6.0)
        delay = self.rng.uniform(s.claim_delay_min_s, s.claim_delay_max_s)
        if rate_mb_min is not None and rate_mb_min > 0.01:
            to_floor_s = (remaining_mb - s.claim_delay_floor_mb) / rate_mb_min * 60.0
            delay = min(delay, 0.5 * to_floor_s)
        return max(1.5, delay)

    def backoff(self, failures: int) -> float:
        return min(900.0, 30.0 * 2 ** max(0, failures - 1)) * self.rng.uniform(0.8, 1.3)

    def step_pause(self) -> float:
        return clamp(self.jitter(1.1, sigma=0.4), 0.5, 2.5)

    # -------------------------------------------------------------- navigation
    def pick_route(self) -> str:
        """How to look at the overview this time."""
        return self.rng.choices(["reload", "goto", "via_bookings"], weights=[65, 25, 10])[0]

    def pick_login_entry(self) -> str:
        return self.rng.choices(["direct", "homepage"], weights=[70, 30])[0]

    # ------------------------------------------------------ browser interaction
    async def pause(self, lo: float | None = None, hi: float | None = None) -> None:
        if lo is None:
            delay = self.step_pause()
        else:
            delay = self.rng.uniform(lo, hi if hi is not None else lo * 2)
        await asyncio.sleep(delay)

    async def move_to(self, page: Any, x: float, y: float) -> None:
        """Curved-ish path: optional random waypoint, then multi-step move."""
        cx, cy = self._mouse
        if self.rng.random() < 0.7:
            mx = (cx + x) / 2 + self.rng.uniform(-90, 90)
            my = (cy + y) / 2 + self.rng.uniform(-60, 60)
            await page.mouse.move(mx, my, steps=self.rng.randint(4, 12))
        await page.mouse.move(x, y, steps=self.rng.randint(6, 20))
        self._mouse = (x, y)

    async def click(self, page: Any, locator: Any, timeout_ms: int = 15000) -> None:
        """Move to a random point inside the element, hover briefly, click."""
        await locator.wait_for(state="visible", timeout=timeout_ms)
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        box = await locator.bounding_box()
        if not box or box["width"] < 3 or box["height"] < 3:
            await locator.click(timeout=timeout_ms)
            return
        x = box["x"] + box["width"] * self.rng.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * self.rng.uniform(0.3, 0.7)
        await self.move_to(page, x, y)
        await asyncio.sleep(self.rng.uniform(0.08, 0.4))
        await page.mouse.down()
        await asyncio.sleep(self.rng.uniform(0.04, 0.14))
        await page.mouse.up()

    async def type(self, page: Any, locator: Any, text: str) -> None:
        """Type key by key with uneven delays and the odd hesitation."""
        await self.click(page, locator)
        await asyncio.sleep(self.rng.uniform(0.2, 0.6))
        await locator.fill("")  # clear browser autofill, if any
        for ch in text:
            await page.keyboard.type(ch)
            delay = self.rng.uniform(0.06, 0.22)
            if self.rng.random() < 0.06:
                delay += self.rng.uniform(0.3, 0.9)
            await asyncio.sleep(delay)
        # Input masks may reformat (e.g. phone spacing); compare loosely.
        value = await locator.input_value()
        if _loose(value) != _loose(text):
            await locator.fill(text)

    async def dwell(self, page: Any) -> None:
        """Linger on the page like a person reading it; sometimes scroll."""
        if self.rng.random() < 0.35:
            dy = self.rng.randint(150, 600)
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(self.rng.uniform(0.8, 2.5))
            if self.rng.random() < 0.6:
                await page.mouse.wheel(0, -dy)
        await asyncio.sleep(self.rng.uniform(1.5, 7.0))


def _loose(s: str) -> str:
    return re.sub(r"[\s\-/()]", "", s)
