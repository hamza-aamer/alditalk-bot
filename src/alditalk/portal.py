"""Reading the data volume and pressing the free '1 GB' rebook button."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .auth import ensure_on_overview, hard_reload, settle
from .browser import BrowserSession
from .parsing import Usage, describe_packs, pick_data_usage, usage_from_offers

if TYPE_CHECKING:
    from pathlib import Path

    from .config import Settings
    from .humanize import Humanizer
    from .state import State

log = logging.getLogger(__name__)

# Overview layout (recon 2026-11-01): each tile is
#   <one-stack class="usage-meter" role="group">
#     <one-usage-meter>  "Noch" "44,7 GB" "von 50 GB"
#       <one-button slot="footer">"Inland"</one-button>
#       <one-button slot="action"><one-icon name="plus"> "1 GB"</one-button>   <- free rebook
TILE_SELECTOR = "one-stack.usage-meter"
ACTION_BUTTON_SELECTOR = 'one-button[slot="action"]'
OFFERS_API = "/selfcare-dashboard/v1/offers"
CLAIM_LABEL_RE = re.compile(r"1\s*GB|nachbuchen|hinzubuchen|dazubuchen|aufladen", re.I)
CONFIRM_LABEL_RE = re.compile(r"^\s*(jetzt\s+)?(kostenlos\s+)?(buchen|bestätigen|nachbuchen|ja|weiter)\b", re.I)
SUCCESS_RE = re.compile(r"erfolgreich|wurde gebucht|hinzugefügt|ist jetzt aktiv", re.I)

# Text of every element in the page, shadow roots included (fallback reader).
DEEP_TEXTS_JS = r"""
() => {
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      if (t && t.length < 200 && /\bvon\b/.test(t) && /(MB|GB)/i.test(t)) out.push(t);
    }
  };
  walk(document);
  return out;
}
"""

# Label + disabled state of a <one-button> (label may live in its shadow root).
BUTTON_INFO_JS = r"""
(el) => {
  const inner = el.shadowRoot ? el.shadowRoot.querySelector('button, a') : null;
  const text = [el.innerText, el.getAttribute('label'), el.getAttribute('aria-label'),
                inner ? inner.innerText : '', inner ? inner.getAttribute('aria-label') : '',
                el.shadowRoot ? el.shadowRoot.textContent : '']
               .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  const disabled = el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true' ||
                   !!(inner && (inner.disabled || inner.getAttribute('aria-disabled') === 'true'));
  const plus = !!el.querySelector('one-icon[name="plus"]') ||
               !!(el.shadowRoot && el.shadowRoot.querySelector('one-icon[name="plus"]'));
  return { text: text.slice(0, 160), disabled, plus };
}
"""


class UsageNotFound(Exception):
    pass


class ClaimButtonNotFound(Exception):
    pass


@dataclass
class ClaimResult:
    clicked: bool
    verified: bool
    before: Usage
    after: Usage | None
    button_text: str = ""

    @property
    def gained_mb(self) -> float:
        return (self.after.remaining_mb - self.before.remaining_mb) if self.after else 0.0


class Portal:
    """Everything that touches the website. The loop only talks to this."""

    def __init__(self, settings: Settings, h: Humanizer, state: State) -> None:
        self.s = settings
        self.h = h
        self.state = state
        self.browser = BrowserSession(settings)
        self._nav_at = 0.0  # when the overview was last (re)loaded

    @property
    def page(self) -> Any:
        assert self.browser.page is not None
        return self.browser.page

    async def prepare(self) -> dict[str, Any]:
        """Browser up, overview loaded. Returns route/logged_in/browser_started for the history."""
        started_before = self.browser.started_at
        await self.browser.ensure()
        started = self.browser.started_at != started_before
        self._nav_at = time.time()
        info = await ensure_on_overview(self.page, self.s, self.h, self.state)
        await self.browser.save_session()
        return {**info, "browser_started": started}

    async def _reload(self) -> None:
        """Hard refresh (cache bypassed) and mark the moment, so only newer data counts."""
        self._nav_at = time.time()
        await hard_reload(self.page)
        await settle(self.page)

    # ------------------------------------------------------------- reading
    async def _fresh_api_usage(self, timeout_s: float) -> Usage | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            data = self.browser.latest_api(OFFERS_API, since=self._nav_at, fresh_only=True)
            usage = usage_from_offers(data)
            if usage:
                return usage
            await asyncio.sleep(0.5)
        return None

    async def read_usage(self, timeout_s: float = 20.0) -> Usage:
        """Exact numbers from the portal's API, fetched over the network after the last page load.

        Never trusts cached values: if no fresh API response shows up (cache hit, in-app
        navigation, slow page), it hard-refreshes once and waits again. Page text is the
        last resort, and only right after a hard refresh.
        """
        usage = await self._fresh_api_usage(timeout_s / 2)
        if usage:
            return usage
        cached = self.browser.latest_api(OFFERS_API, since=self._nav_at) is not None
        log.info("No fresh data from the portal API%s; hard-refreshing", " (got a cached copy)" if cached else "")
        await self._reload()
        usage = await self._fresh_api_usage(timeout_s)
        if usage:
            return usage
        dom = await self._usage_from_tiles()
        if dom:
            log.warning("Portal API not seen even after a hard refresh; using page text")
            return dom
        texts = await self.page.evaluate(DEEP_TEXTS_JS)
        picked = pick_data_usage(texts)
        if picked:
            log.warning("Data volume found only via deep-text fallback; selectors may be outdated")
            u = picked[1]
            return Usage(u.remaining_mb, u.total_mb, source="deep-text", raw=u.raw)
        raise UsageNotFound("no data volume in the offers API or on the overview")

    async def _usage_from_tiles(self) -> Usage | None:
        try:
            texts = await self.page.locator(TILE_SELECTOR).all_inner_texts()
        except Exception:  # noqa: BLE001
            return None
        picked = pick_data_usage(texts)
        return picked[1] if picked else None

    def packs_summary(self) -> list[dict[str, Any]]:
        return describe_packs(self.browser.latest_api(OFFERS_API, since=0))

    # ------------------------------------------------------------ claiming
    async def _button_info(self, loc: Any) -> dict[str, Any]:
        return await loc.evaluate(BUTTON_INFO_JS)

    async def find_claim_button(self) -> tuple[Any, str]:
        """The '+ 1 GB' action button inside the domestic data tile."""
        tiles = self.page.locator(TILE_SELECTOR)
        try:
            await tiles.first.wait_for(state="visible", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        picked = pick_data_usage(await tiles.all_inner_texts())
        if picked:
            buttons = tiles.nth(picked[0]).locator(ACTION_BUTTON_SELECTOR).filter(visible=True)
            if await buttons.count() >= 1:
                info = await self._button_info(buttons.first)
                if info["disabled"]:
                    raise ClaimButtonNotFound(f"rebook button {info['text']!r} is disabled")
                return buttons.first, info["text"]

        # Fallback: best-scoring action button anywhere on the page.
        buttons = self.page.locator(ACTION_BUTTON_SELECTOR).filter(visible=True)
        best: tuple[int, Any, str] | None = None
        count = await buttons.count()
        for i in range(count):
            b = buttons.nth(i)
            info = await self._button_info(b)
            if info["disabled"]:
                continue
            score = (2 if CLAIM_LABEL_RE.search(info["text"]) else 0) + (1 if info["plus"] else 0)
            if score >= 2 and (best is None or score > best[0]):
                best = (score, b, info["text"])
        if best:
            log.warning("Rebook button found only via fallback search; selectors may be outdated")
            return best[1], best[2]
        raise ClaimButtonNotFound(f"no enabled rebook button ({count} action buttons visible)")

    async def _maybe_confirm(self) -> None:
        """Handle a confirmation dialog if one pops up."""
        dialog_buttons = (
            self.page.locator('[role="dialog"], one-dialog, one-modal, dialog')
            .locator("one-button, button")
            .filter(has_text=CONFIRM_LABEL_RE)
            .filter(visible=True)
            .first
        )
        try:
            await dialog_buttons.wait_for(state="visible", timeout=6000)
        except Exception:  # noqa: BLE001
            return
        await self.h.pause(0.7, 2.0)
        await self.h.click(self.page, dialog_buttons)
        log.info("Confirmed booking dialog")

    async def claim(self, dry_run: bool) -> ClaimResult:
        before = await self.read_usage()
        button, label = await self.find_claim_button()
        if dry_run:
            log.info("DRY RUN: would click rebook button %r at %s", label, before.short())
            return ClaimResult(clicked=False, verified=False, before=before, after=None, button_text=label)

        log.info("Clicking rebook button %r (remaining %s via %s)", label, before.short(), before.source)
        log.info("Data packs before: %s", self.packs_summary())
        await self.h.click(self.page, button)
        await self.h.pause(1.0, 2.5)
        await self._maybe_confirm()

        try:
            await self.page.get_by_text(SUCCESS_RE).first.wait_for(state="visible", timeout=10_000)
            log.info("Portal shows a success message")
        except Exception:  # noqa: BLE001
            pass

        after: Usage | None = None
        for wait_lo, wait_hi in ((4, 9), (20, 40)):  # the display can lag
            await self.h.pause(wait_lo, wait_hi)
            await self._reload()
            try:
                after = await self.read_usage()
            except UsageNotFound:
                continue
            if after.remaining_mb - before.remaining_mb >= self.s.claim_verify_min_gain_mb:
                log.info("Data packs after: %s", self.packs_summary())
                return ClaimResult(True, True, before, after, label)
        log.info("Data packs after (unverified): %s", self.packs_summary())
        return ClaimResult(True, False, before, after, label)

    # --------------------------------------------------------------- misc
    async def snapshot(self) -> None:
        """Viewport JPEG of the overview for the dashboard (no resize, so the page can't notice)."""
        out = self.s.state_dir / "snapshots"
        try:
            out.mkdir(parents=True, exist_ok=True)
            tmp = out / "latest.tmp.jpg"
            await self.page.screenshot(path=str(tmp), type="jpeg", quality=72, timeout=10_000)
            os.replace(tmp, out / "latest.jpg")
        except Exception as exc:  # noqa: BLE001
            log.debug("Snapshot failed: %s", exc)

    async def idle(self) -> None:
        await self.h.dwell(self.page)

    async def dump_debug(self, label: str) -> Path | None:
        return await self.browser.dump_debug(label)

    async def recycle(self) -> None:
        await self.browser.recycle()

    async def close(self) -> None:
        await self.browser.close()
