"""The 24/7 loop: check, maybe claim, sleep a human-ish amount, recover from anything."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from .auth import BadCredentials, LoginLimitReached
from .control import MANUAL_MIN_GAP_S, Control
from .history import History
from .parsing import Usage, fmt_mb
from .state import write_heartbeat

if TYPE_CHECKING:
    from pathlib import Path

    from .config import Settings
    from .humanize import Humanizer
    from .notify import Notifier
    from .portal import ClaimResult
    from .state import State

log = logging.getLogger(__name__)


class PortalLike(Protocol):
    async def prepare(self) -> dict[str, Any] | None: ...
    async def read_usage(self) -> Usage: ...
    async def claim(self, dry_run: bool) -> ClaimResult: ...
    async def idle(self) -> None: ...
    async def dump_debug(self, label: str) -> Path | None: ...
    async def snapshot(self) -> None: ...
    async def recycle(self) -> None: ...


def safe_error(exc: BaseException) -> str:
    """One line, without Playwright's call log (which can echo typed values)."""
    text = str(getattr(exc, "message", "") or exc).split("Call log:", 1)[0].strip()
    first = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first[:300]}" if first else type(exc).__name__


class Bot:
    def __init__(
        self,
        settings: Settings,
        portal: PortalLike,
        humanizer: Humanizer,
        state: State,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        history: History | None = None,
        control: Control | None = None,
    ) -> None:
        self.s = settings
        self.portal = portal
        self.h = humanizer
        self.state = state
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.tz = ZoneInfo(settings.timezone)
        self.last_usage: Usage | None = None
        self.history = history or _NullHistory()
        self.control = control or Control(settings.state_dir)
        self._clicked = False
        self._last_check_end = 0.0

    def _phase(self, phase: str, next_at: float | None = None) -> None:
        s = self.s
        schedule = {
            "tier_high_mb": s.tier_high_mb,
            "tier_mid_mb": s.tier_mid_mb,
            "high": [s.poll_base_high_s, s.poll_cap_high_s],
            "mid": [s.poll_base_mid_s, s.poll_cap_mid_s],
            "low": [s.min_interval_s, s.poll_cap_low_s],
            "quiet": [s.quiet_start_hour, s.quiet_end_hour, s.quiet_min_s, s.quiet_max_s],
            "keepalive": [s.session_keepalive_min_s, s.session_keepalive_max_s] if s.keep_session_alive else None,
        }
        self.history.set_phase(
            phase,
            next_at,
            dry_run=s.dry_run,
            threshold_mb=s.threshold_mb,
            schedule=schedule,
            hold=self.control.on_hold(),
        )

    def local_now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.tz)

    # ------------------------------------------------------------ one cycle
    async def cycle(self, dwell: bool = True, trigger: str = "auto") -> float:
        """One check. Returns seconds to sleep before the next one."""
        t0 = self.clock()
        self._clicked = False
        self._phase("checking")
        info = await self.portal.prepare() or {}
        if info.get("browser_started"):
            self.history.event("browser_start", "browser (re)started")
        if info.get("logged_in"):
            idle = (t0 - self.state.last_ok) / 60 if self.state.last_ok else None
            detail = f"session had expired after {idle:.0f} min idle; logged in" if idle else "logged in"
            log.info("%s", detail[0].upper() + detail[1:])
            self.history.event("login", detail)
        usage = await self.portal.read_usage()
        now = self.clock()
        self.state.add_reading(now, usage.remaining_mb)
        rate = self.state.consumption_rate(now)
        log.info(
            "Remaining %s (source=%s, rate=%s)",
            usage.short(),
            usage.source,
            f"{rate:.1f} MB/min" if rate is not None else "unknown",
        )

        if usage.remaining_mb < self.s.threshold_mb:
            usage = await self._maybe_claim(usage, rate) or usage

        self.last_usage = usage
        await self.portal.snapshot()
        if dwell:
            await self.portal.idle()
        self.state.last_ok = self.clock()
        self.state.save()
        write_heartbeat(self.s.state_dir, self.clock())
        await self.notifier.ping()
        await self._maybe_daily_summary(usage)
        next_s = self.h.next_interval(usage.remaining_mb, self.state.consumption_rate(self.clock()), self.local_now())
        self.history.record_check(
            True,
            remaining_mb=usage.remaining_mb,
            total_mb=usage.total_mb,
            source=usage.source,
            rate_mb_min=rate,
            next_in_s=next_s,
            duration_s=self.clock() - t0,
            route=info.get("route"),
            logged_in=bool(info.get("logged_in")),
            browser_started=bool(info.get("browser_started")),
            claimed=self._clicked,
            trigger=trigger,
        )
        self._last_check_end = self.clock()
        return next_s

    async def _maybe_claim(self, usage: Usage, rate: float | None) -> Usage | None:
        now = self.clock()
        last = self.state.last_claim_attempt()
        if last is not None and now - last < self.s.claim_cooldown_s:
            log.info("Under threshold, but in cooldown (%.0f s left)", self.s.claim_cooldown_s - (now - last))
            return None
        if self.state.claim_attempts_since(now - 3600) >= self.s.max_claims_per_hour:
            log.warning("Hourly claim cap (%d) reached; not clicking", self.s.max_claims_per_hour)
            if not self.state.cap_alert_sent or now - self.state.cap_alert_sent > 3600:
                self.state.cap_alert_sent = now
                self.history.event("cap_reached", f"{self.s.max_claims_per_hour} claim attempts in the last hour")
                await self.notifier.send(
                    f"🛑 <b>Claim cap reached</b> ({self.s.max_claims_per_hour}/h). "
                    "Pausing claims. Check the portal, maybe the page changed."
                )
            return None

        delay = self.h.claim_delay(usage.remaining_mb, rate)
        log.info("Under %s, claiming in %.0f s", fmt_mb(self.s.threshold_mb), delay)
        self._phase("claiming", next_at=self.clock() + delay)
        await self.sleep(delay)

        self.state.record_claim_attempt(self.clock())
        self.state.save()
        result = await self.portal.claim(self.s.dry_run)
        self._clicked = result.clicked
        self.history.record_claim(
            before_mb=result.before.remaining_mb,
            after_mb=result.after.remaining_mb if result.after else None,
            verified=result.verified,
            dry_run=not result.clicked,
            button=result.button_text,
        )
        if not result.clicked:  # dry run
            await self.notifier.send(
                f"🧪 DRY RUN: would claim 1 GB now (remaining {html.escape(result.before.short())}, "
                f"button {html.escape(result.button_text)!r})",
                silent=True,
            )
            return None
        if result.verified and result.after is not None:
            self.state.record_claim(self.clock(), result.gained_mb)
            self.state.save()
            log.info("Claim verified: %s → %s", result.before.short(), result.after.short())
            today = len(self.state.claims_since(self.clock() - 86400))
            await self.notifier.send(
                f"✅ <b>+1 GB claimed</b>\n{html.escape(result.before.short())} → "
                f"{html.escape(result.after.short())}\nLast 24 h: {today} claims",
                silent=True,
            )
            return result.after

        after = result.after.short() if result.after else "unreadable"
        log.warning("Clicked, but volume did not go up (%s → %s)", result.before.short(), after)
        stem = await self.portal.dump_debug("claim-unverified")
        await self.notifier.send(
            f"⚠️ <b>Clicked rebook, but volume didn't increase</b>\n"
            f"{html.escape(result.before.short())} → {html.escape(after)}\n"
            f"Debug: <code>{html.escape(str(stem))}</code>"
        )
        return result.after

    async def _maybe_daily_summary(self, usage: Usage) -> None:
        local = self.local_now()
        today = local.date().isoformat()
        if local.hour < self.s.daily_summary_hour or self.state.last_summary_date == today:
            return
        self.state.last_summary_date = today
        self.state.save()
        claims = self.state.claims_since(self.clock() - 86400)
        gb = sum(c[1] for c in claims) / 1024
        await self.notifier.send(
            f"📊 <b>Daily summary</b>\nLast 24 h: {len(claims)} claims (+{gb:.1f} GB)\n"
            f"Now: {html.escape(usage.short())}\nAll time: {self.state.total_claims} claims",
            silent=True,
        )

    # ------------------------------------------------------------ forever
    async def run_forever(self) -> None:
        mode = " (DRY RUN)" if self.s.dry_run else ""
        await self.notifier.send(f"🤖 ALDI TALK bot started{mode}", silent=True)
        self.history.event("start", f"bot started{mode}")
        try:
            await self._loop()
        except SystemExit:
            self.history.event("restart", "too many failures in a row; exiting for a container restart")
            self._phase("stopped")
            raise
        except asyncio.CancelledError:
            self.history.event("stop", "bot stopped")
            self._phase("stopped")
            raise

    async def _wait(self, seconds: float) -> str:
        """Sleep until the next check, waking early for dashboard commands.

        Returns "timer", "manual" (Check now) or "resume" (unpaused).
        """
        end = self.clock() + seconds
        held = False
        while True:
            if self.control.take_check_request():
                if self.clock() - self._last_check_end >= MANUAL_MIN_GAP_S:
                    log.info("Manual check requested from the dashboard")
                    self.history.event("manual", "check requested from the dashboard")
                    return "manual"
                log.info("Ignoring manual check request (a check just finished)")
            if self.control.on_hold():
                if not held:
                    held = True
                    log.info("Paused from the dashboard")
                    self.history.event("hold", "checks paused from the dashboard")
                    self._phase("hold")
                await self.sleep(2.0)
                continue
            if held:
                log.info("Resumed from the dashboard")
                self.history.event("resume", "checks resumed from the dashboard")
                return "resume"
            left = end - self.clock()
            if left <= 0:
                return "timer"
            await self.sleep(min(2.0, left))

    async def _loop(self) -> None:
        failures = 0
        trigger = "auto"
        if self.control.on_hold():  # paused before a restart: stay paused
            trigger = "manual" if await self._wait(0) != "timer" else "auto"
        while True:
            t0 = self.clock()
            phase = "sleeping"
            try:
                sleep_s = await asyncio.wait_for(self.cycle(trigger=trigger), timeout=self.s.cycle_timeout_s)
                if failures:
                    self.history.event("recovered", f"after {failures} failed checks")
                    await self.notifier.send(f"✅ Recovered after {failures} failed checks", silent=True)
                failures = 0
            except BadCredentials as exc:
                log.error("Login rejected: %s", exc)
                phase = "paused"
                self.history.event("login_rejected", "Anmeldung fehlgeschlagen")
                self.history.record_check(False, error="login rejected", duration_s=self.clock() - t0)
                await self.notifier.send(
                    "❌ <b>Login rejected</b> (Anmeldung fehlgeschlagen). Check ALDI_PHONE / ALDI_PASSWORD in .env."
                )
                sleep_s = self.s.bad_credentials_pause_s
            except LoginLimitReached as exc:
                log.error("%s", exc)
                phase = "paused"
                self.history.record_check(False, error=str(exc), duration_s=self.clock() - t0)
                now = self.clock()
                if not self.state.login_limit_alert_sent or now - self.state.login_limit_alert_sent > 6 * 3600:
                    self.state.login_limit_alert_sent = now
                    self.state.save()
                    self.history.event("login_paused", str(exc))
                    await self.notifier.send(
                        f"⛔ <b>Too many failed logins today</b>. Pausing logins for "
                        f"{exc.retry_after_s / 3600:.1f} h to avoid an account lock."
                    )
                sleep_s = min(exc.retry_after_s, 3600)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                failures += 1
                err = safe_error(exc)
                log.error("Check failed (%d in a row): %s", failures, err)
                phase = "backoff"
                if failures == 1:
                    self.history.event("error", err)
                stem = None
                try:
                    stem = await asyncio.wait_for(self.portal.dump_debug("error"), timeout=30)
                except Exception:  # noqa: BLE001
                    pass
                await self.notifier.ping(fail=True)
                if failures == 1 or failures % 5 == 0:
                    await self.notifier.send(
                        f"⚠️ <b>Check failed</b> ({failures}× in a row)\n<code>{html.escape(err)}</code>"
                        + (f"\nDebug: <code>{html.escape(str(stem))}</code>" if stem else "")
                    )
                if failures >= self.s.max_consecutive_failures:
                    await self.notifier.send("🔁 Too many failures in a row; restarting the container.")
                    raise SystemExit(1) from exc
                try:
                    await asyncio.wait_for(self.portal.recycle(), timeout=30)
                except Exception:  # noqa: BLE001
                    pass
                sleep_s = self.h.backoff(failures)
                self.history.record_check(False, error=err, next_in_s=sleep_s, duration_s=self.clock() - t0)
            log.info("Next check in %s", _fmt_duration(sleep_s))
            self._last_check_end = self.clock()
            self._phase(phase, next_at=self.clock() + sleep_s)
            reason = await self._wait(sleep_s)
            trigger = "manual" if reason in ("manual", "resume") else "auto"


def _fmt_duration(seconds: float) -> str:
    if seconds >= 90:
        return f"{seconds / 60:.1f} min"
    return f"{seconds:.0f} s"


class _NullHistory:
    """Used when no history is wired in (tests)."""

    def record_check(self, *a: Any, **k: Any) -> None: ...
    def record_claim(self, *a: Any, **k: Any) -> None: ...
    def event(self, *a: Any, **k: Any) -> None: ...
    def set_phase(self, *a: Any, **k: Any) -> None: ...
