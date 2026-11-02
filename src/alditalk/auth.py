"""Getting to the overview page, logging in (ForgeRock SSO) when needed."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

if TYPE_CHECKING:
    from .config import Settings
    from .humanize import Humanizer
    from .state import State

log = logging.getLogger(__name__)

PORTAL_HOST = "www.alditalk-kundenportal.de"
OVERVIEW_URL = f"https://{PORTAL_HOST}/portal/auth/uebersicht/"
BOOKINGS_URL = f"https://{PORTAL_HOST}/portal/auth/buchungsuebersicht/"
HOMEPAGE_URL = "https://www.alditalk.de/"
LOGIN_HOST = "login.alditalk-kundenbetreuung.de"

LOGIN_FAILED_RE = re.compile(r"Anmeldung fehlgeschlagen|Rufnummer oder Passwort (ist )?(falsch|ungültig)", re.I)
SESSION_EXPIRED_RE = re.compile(r"Sitzung abgelaufen", re.I)


# Transient network errors (Wi-Fi/hotspot flaps, resets): retry instead of failing the check.
NET_RETRY_RE = re.compile(
    r"ERR_NETWORK_CHANGED|ERR_INTERNET_DISCONNECTED|ERR_CONNECTION_(RESET|CLOSED|ABORTED)|"
    r"ERR_NAME_NOT_RESOLVED|ERR_TIMED_OUT|ERR_NETWORK_IO_SUSPENDED|ERR_ADDRESS_UNREACHABLE"
)


async def _retry_nav(action: Any, what: str, attempts: int = 5) -> Any:
    for i in range(attempts):
        try:
            return await action()
        except PlaywrightError as exc:
            if i == attempts - 1 or not NET_RETRY_RE.search(str(exc)):
                raise
            log.info("Network hiccup on %s (%s); retrying", what, NET_RETRY_RE.search(str(exc))[0])
            await asyncio.sleep(2 + 3 * i)
    return None


async def goto(page: Any, url: str) -> Any:
    return await _retry_nav(lambda: page.goto(url, wait_until="domcontentloaded"), urlparse(url).path or url)


async def reload(page: Any) -> Any:
    return await _retry_nav(lambda: page.reload(wait_until="domcontentloaded"), "reload")


async def hard_reload(page: Any) -> Any:
    """Ctrl+Shift+R: reload while bypassing the browser cache, so no stale data is shown."""

    async def _do() -> None:
        cdp = await page.context.new_cdp_session(page)
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                await cdp.send("Page.reload", {"ignoreCache": True})
        finally:
            try:
                await cdp.detach()
            except Exception:  # noqa: BLE001
                pass

    return await _retry_nav(_do, "hard reload")


class BadCredentials(Exception):
    pass


class LoginError(Exception):
    pass


class LoginLimitReached(Exception):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"login attempt limit reached; retry in {retry_after_s / 60:.0f} min")
        self.retry_after_s = retry_after_s


def on_login_page(page: Any) -> bool:
    return urlparse(page.url).netloc == LOGIN_HOST


def on_portal(page: Any) -> bool:
    u = urlparse(page.url)
    return u.netloc == PORTAL_HOST and u.path.startswith("/portal/auth/")


def on_overview(page: Any) -> bool:
    u = urlparse(page.url)
    return u.netloc == PORTAL_HOST and u.path.rstrip("/").endswith("/portal/auth/uebersicht")


async def text_visible(page: Any, pattern: re.Pattern[str]) -> bool:
    loc = page.get_by_text(pattern)
    try:
        for i in range(min(await loc.count(), 5)):
            if await loc.nth(i).is_visible():
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def accept_consent(page: Any, h: Humanizer, timeout_ms: int = 4000) -> bool:
    """Usercentrics banner: accept, the same way every time."""
    btn = page.get_by_test_id("uc-accept-all-button").filter(visible=True).first
    try:
        await btn.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeout:
        return False
    await h.pause(0.6, 1.8)
    await h.click(page, btn)
    log.info("Cookie consent accepted")
    await h.pause(0.4, 1.0)
    return True


async def settle(page: Any) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
    except PlaywrightTimeout:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeout:
        pass


def _phone_input(page: Any) -> Any:
    return (
        page.locator('input[placeholder="Rufnummer"]')
        .or_(page.locator("#input-5"))
        .or_(page.locator('input[type="tel"]'))
        .filter(visible=True)
        .first
    )


def _password_input(page: Any) -> Any:
    return (
        page.locator('input[placeholder="Passwort eingeben"]')
        .or_(page.locator("#input-6"))
        .or_(page.locator('input[type="password"]'))
        .filter(visible=True)
        .first
    )


def _submit_button(page: Any) -> Any:
    name = re.compile(r"^\s*Anmelden\s*$")
    return (
        page.get_by_role("button", name=name)
        .or_(page.get_by_role("link", name=name))
        .or_(page.locator("one-button, a.button, button").filter(has_text=name))
        .filter(visible=True)
        .first
    )


async def _enter_via_homepage(page: Any, h: Humanizer) -> None:
    await goto(page, HOMEPAGE_URL)
    await settle(page)
    await accept_consent(page, h, timeout_ms=6000)
    await h.pause()
    link = (
        page.locator("span.nav-service__text")
        .filter(has_text="Mein ALDI TALK")
        .or_(page.get_by_role("link", name=re.compile("Mein ALDI TALK", re.I)))
        .filter(visible=True)
        .first
    )
    await h.click(page, link)
    await page.wait_for_url(re.compile(rf"https://({re.escape(LOGIN_HOST)}|{re.escape(PORTAL_HOST)})/"), timeout=30_000)


async def login(page: Any, s: Settings, h: Humanizer, state: State) -> bool:
    """Returns True if credentials were actually entered."""
    now = time.time()
    failures = state.login_failures_since(now - 86400)
    if len(failures) >= s.max_login_attempts_per_day:
        raise LoginLimitReached(retry_after_s=max(60.0, failures[0] + 86400 - now))

    if not on_login_page(page):
        entry = h.pick_login_entry()
        try:
            if entry == "homepage":
                await _enter_via_homepage(page, h)
            else:
                await goto(page, OVERVIEW_URL)
        except Exception as exc:  # noqa: BLE001
            log.info("Login entry via %s failed (%s); going direct", entry, type(exc).__name__)
            await goto(page, OVERVIEW_URL)
        await settle(page)
        if on_portal(page):
            log.info("Session still valid; no login needed")
            return False

    log.info("Logging in")
    await accept_consent(page, h, timeout_ms=5000)

    phone = _phone_input(page)
    await phone.wait_for(state="visible", timeout=30_000)
    await h.pause(0.8, 2.0)
    await h.type(page, phone, s.aldi_phone)
    await h.pause(0.8, 2.2)

    password = _password_input(page)
    await password.wait_for(state="visible", timeout=20_000)
    await h.type(page, password, s.aldi_password.get_secret_value())
    await h.pause(0.5, 1.6)

    await h.click(page, _submit_button(page))

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if on_portal(page):
            break
        if await text_visible(page, LOGIN_FAILED_RE):
            state.record_login_failure(time.time())
            state.save()
            raise BadCredentials("portal says: Anmeldung fehlgeschlagen")
        await asyncio.sleep(0.5)
    else:
        state.record_login_failure(time.time())
        state.save()
        where = urlparse(page.url)
        raise LoginError(f"login did not reach the portal (stuck at {where.netloc}{where.path})")

    await settle(page)
    await accept_consent(page, h, timeout_ms=6000)  # the portal has its own banner
    log.info("Logged in")
    return True


async def ensure_on_overview(page: Any, s: Settings, h: Humanizer, state: State) -> dict[str, Any]:
    """Load/refresh the overview in a varied way; log in if the session is gone.

    Returns {"route": how the page was opened, "logged_in": whether we had to log in}.
    """
    route = h.pick_route() if on_portal(page) else "goto"
    if route == "reload" and not on_overview(page):
        route = "goto"
    logged_in = False
    if route == "reload":
        if s.hard_refresh:
            route = "hard_reload"
            await hard_reload(page)
        else:
            await reload(page)
    elif route == "via_bookings":
        await goto(page, BOOKINGS_URL)
        await settle(page)
        await h.pause(1.5, 5.0)
        if on_portal(page):
            nav = page.get_by_role("link", name=re.compile(r"^\s*Übersicht\s*$", re.I)).filter(visible=True).first
            try:
                await h.click(page, nav, timeout_ms=4000)
            except Exception:  # noqa: BLE001
                await goto(page, OVERVIEW_URL)
    else:
        await goto(page, OVERVIEW_URL)
    await settle(page)

    if on_login_page(page) or await text_visible(page, SESSION_EXPIRED_RE):
        logged_in = await login(page, s, h, state)

    if not on_overview(page):
        await goto(page, OVERVIEW_URL)
        await settle(page)
    if on_login_page(page):
        logged_in = await login(page, s, h, state) or logged_in
        if not on_overview(page):
            await goto(page, OVERVIEW_URL)
            await settle(page)
    if not on_overview(page):
        raise LoginError(f"could not reach the overview (at {urlparse(page.url).netloc}{urlparse(page.url).path})")
    await accept_consent(page, h, timeout_ms=2000)
    return {"route": route, "logged_in": logged_in}
