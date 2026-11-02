"""Telegram messages and the optional healthchecks.io dead-man's switch.

Never raises: a broken notifier must not take the bot down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self._token = settings.telegram_token.get_secret_value() if settings.telegram_token else None
        self._chat_id = settings.telegram_chat_id
        self._hc_url = settings.healthcheck_url

    @property
    def telegram_enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def send(self, html_text: str, silent: bool = False) -> bool:
        """Send an HTML-formatted Telegram message. Escape dynamic parts yourself."""
        if not self.telegram_enabled:
            log.debug("Telegram not configured; message: %s", html_text)
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, data=payload)
            if r.status_code != 200:
                log.warning("Telegram returned %s: %s", r.status_code, self._scrub(r.text[:200]))
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram send failed: %s", self._scrub(f"{type(exc).__name__}: {exc}"))
            return False

    async def ping(self, fail: bool = False) -> None:
        if not self._hc_url:
            return
        url = self._hc_url.rstrip("/") + ("/fail" if fail else "")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(url)
        except Exception as exc:  # noqa: BLE001
            log.debug("Healthcheck ping failed: %s", exc)

    async def get_chat_ids(self) -> list[tuple[str, str]]:
        """(chat_id, name) pairs from recent bot updates; used for setup."""
        if not self._token:
            raise RuntimeError("TELEGRAM_TOKEN is not set")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"https://api.telegram.org/bot{self._token}/getUpdates")
        r.raise_for_status()
        seen: dict[str, str] = {}
        for upd in r.json().get("result", []):
            msg = upd.get("message") or upd.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
                seen[str(chat["id"])] = name
        return list(seen.items())
