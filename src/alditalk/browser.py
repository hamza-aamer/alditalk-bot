"""Browser lifecycle: one persistent Chrome profile = one stable 'device'."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# Fixed identity. Do NOT randomize these per session.
VIEWPORT = {"width": 1536, "height": 864}
SCREEN = {"width": 1920, "height": 1080}
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--lang=de-DE",
    "--accept-lang=de-DE,de,en-US,en",  # native languages list, like a real German Chrome
    f"--window-size={VIEWPORT['width']},{VIEWPORT['height'] + 96}",
]
# Only the portal's own data API ("BFF"); never the login server (it returns session tokens).
CAPTURE_HOST = "www.alditalk-kundenportal.de"
CAPTURE_PATH_PREFIX = "/scs/bff/"
MAX_DEBUG_DUMPS = 10

# Serializes the DOM *including open shadow roots* (page.content() skips them).
# Input values are never included.
DEEP_DUMP_JS = r"""
() => {
  const KEEP = ['id','class','slot','name','type','role','placeholder','href','label',
                'data-testid','aria-label','aria-valuenow','aria-valuemax','aria-disabled',
                'disabled','title','variant','icon'];
  const out = [];
  const walk = (node, depth) => {
    if (depth > 60) return;
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        const t = child.textContent.replace(/\s+/g, ' ').trim();
        if (t) out.push('  '.repeat(depth) + JSON.stringify(t.slice(0, 300)));
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const tag = child.tagName.toLowerCase();
      if (['script','style','noscript','svg','path','link','meta'].includes(tag) &&
          !(tag === 'svg' && child.hasAttribute('aria-valuenow'))) continue;
      const attrs = KEEP.filter(a => child.hasAttribute(a))
        .map(a => `${a}="${String(child.getAttribute(a)).slice(0, 120)}"`).join(' ');
      out.push('  '.repeat(depth) + `<${tag}${attrs ? ' ' + attrs : ''}>`);
      if (child.shadowRoot) {
        out.push('  '.repeat(depth + 1) + '#shadow-root');
        walk(child.shadowRoot, depth + 2);
      }
      walk(child, depth + 1);
    }
  };
  walk(document.documentElement, 0);
  return out.join('\n');
}
"""


class BrowserSession:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._started_at = 0.0
        self.api: dict[str, tuple[float, Any, bool]] = {}  # endpoint path -> (time, json, from_network)

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def cookie_file(self) -> Path:
        return self.s.state_dir / "session-cookies.json"

    @property
    def profile_dir(self) -> Path:
        return self.s.state_dir / "profile"

    @property
    def debug_dir(self) -> Path:
        return self.s.state_dir / "debug"

    async def start(self) -> Page:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # Stale Chrome locks from a killed container would block startup.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (self.profile_dir / name).unlink(missing_ok=True)

        self._pw = await async_playwright().start()
        opts: dict[str, Any] = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.s.headless,
            timezone_id=self.s.timezone,
            viewport=VIEWPORT,
            screen=SCREEN,
            ignore_default_args=["--enable-automation"],
            args=CHROME_ARGS,
        )
        channel = self.s.browser_channel
        try:
            if channel and channel != "chromium":
                self.context = await self._pw.chromium.launch_persistent_context(channel=channel, **opts)
            else:
                self.context = await self._pw.chromium.launch_persistent_context(**opts)
        except PlaywrightError as exc:
            if channel == "chromium" or "found" not in str(exc).lower() and "exist" not in str(exc).lower():
                raise
            log.warning("Browser channel %r not installed; falling back to bundled Chromium", channel)
            self.context = await self._pw.chromium.launch_persistent_context(**opts)

        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        for extra in pages[1:]:
            await extra.close()
        self.page.set_default_timeout(30_000)
        self.page.on("response", self._capture_response)
        await self._restore_session_cookies()
        self._started_at = time.monotonic()
        log.info("Browser started (channel=%s, headless=%s)", channel, self.s.headless)
        return self.page

    async def save_session(self) -> None:
        """Chrome drops session cookies on exit; keep them so a restart needn't log in again."""
        if self.context is None:
            return
        try:
            cookies = [c for c in await self.context.cookies() if c.get("expires", -1) in (-1, None)]
            fd = os.open(self.cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cookies, fh)
        except Exception as exc:  # noqa: BLE001
            log.debug("Saving session cookies failed: %s", exc)

    async def _restore_session_cookies(self) -> None:
        try:
            cookies = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        try:
            if cookies and self.context is not None:
                await self.context.add_cookies(cookies)
                log.info("Restored %d session cookies", len(cookies))
        except Exception as exc:  # noqa: BLE001
            log.debug("Restoring session cookies failed: %s", exc)

    async def close(self) -> None:
        await self.save_session()
        try:
            if self.context is not None:
                await self.context.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("Context close failed: %s", exc)
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("Playwright stop failed: %s", exc)
        self._pw = self.context = self.page = None

    async def recycle(self) -> None:
        log.info("Recycling browser")
        await self.close()

    async def ensure(self) -> Page:
        """Running browser with a usable page; recycles old sessions."""
        age_h = (time.monotonic() - self._started_at) / 3600
        if self.context is not None and age_h > self.s.browser_recycle_hours:
            log.info("Browser is %.1f h old", age_h)
            await self.recycle()
        if self.context is None or self.page is None:
            return await self.start()
        if self.page.is_closed():
            self.page = await self.context.new_page()
            self.page.on("response", self._capture_response)
        return self.page

    async def _capture_response(self, response: Any) -> None:
        """Remember the latest JSON of each portal API endpoint the page loads itself."""
        try:
            if response.request.resource_type not in ("xhr", "fetch") or response.status != 200:
                return
            url = urlparse(response.url)
            if url.netloc != CAPTURE_HOST or not url.path.startswith(CAPTURE_PATH_PREFIX):
                return
            if "json" not in (response.headers.get("content-type") or ""):
                return
            # No server address means Chrome answered from its cache (or a service worker): not fresh.
            from_network = (await response.server_addr()) is not None and not response.from_service_worker
            self.api[url.path] = (time.time(), await response.json(), from_network)
        except Exception:  # noqa: BLE001 - bodies of redirects etc. are unavailable
            return

    async def deep_dump(self) -> str:
        assert self.page is not None
        return await self.page.evaluate(DEEP_DUMP_JS)

    async def dump_debug(self, label: str) -> Path | None:
        """Screenshot + shadow-DOM-aware HTML outline into state/debug/."""
        if self.page is None or self.page.is_closed():
            return None
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stem = self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
        try:
            await self.page.screenshot(path=f"{stem}.png", full_page=True, timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            log.debug("Screenshot failed: %s", exc)
        try:
            outline = await self.deep_dump()
            Path(f"{stem}.txt").write_text(f"URL: {self.page.url}\n\n{outline}", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("DOM dump failed: %s", exc)
        self._prune_debug()
        return stem

    def latest_api(self, path_suffix: str, since: float, fresh_only: bool = False) -> Any | None:
        """JSON of the newest captured endpoint ending in `path_suffix`, if fetched after `since`.

        With fresh_only, responses Chrome served from its cache are ignored.
        """
        hits = [
            (t, body)
            for path, (t, body, net) in self.api.items()
            if path.endswith(path_suffix) and t >= since and (net or not fresh_only)
        ]
        return max(hits, key=lambda x: x[0])[1] if hits else None

    def save_api(self, path: Path) -> None:
        """For recon only. Contains personal data: stays local in state/ (gitignored)."""
        data = {p: body for p, (_, body, _net) in self.api.items()}
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def _prune_debug(self) -> None:
        stems = sorted({p.with_suffix("") for p in self.debug_dir.glob("*-*") if p.suffix in (".png", ".txt")})
        for old in stems[:-MAX_DEBUG_DUMPS]:
            for suffix in (".png", ".txt"):
                Path(f"{old}{suffix}").unlink(missing_ok=True)
