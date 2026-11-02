"""CLI entry point: `python -m alditalk [run|once|recon|test-notify|telegram-chat-id|fingerprint-check]`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import logging.handlers
import signal
import sys
from datetime import datetime
from pathlib import Path

from .config import Settings


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


def setup_logging(settings: Settings, verbose: bool) -> None:
    log_dir = settings.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    redact = RedactFilter(settings.secrets)
    stream = logging.StreamHandler(sys.stdout)
    rotating = logging.handlers.RotatingFileHandler(
        log_dir / "alditalk.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in (stream, rotating):
        handler.setFormatter(fmt)
        handler.addFilter(redact)
        root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextlib.contextmanager
def single_instance(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(state_dir / "bot.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another alditalk instance is already running (state/bot.lock).")
    try:
        yield
    finally:
        fh.close()


def build(settings: Settings):
    from .history import History
    from .humanize import Humanizer
    from .loop import Bot
    from .notify import Notifier
    from .portal import Portal
    from .state import State

    state = State.load(settings.state_dir / "state.json")
    h = Humanizer(settings)
    portal = Portal(settings, h, state)
    notifier = Notifier(settings)
    bot = Bot(settings, portal, h, state, notifier, history=History(settings.state_dir))
    return bot, portal, notifier


async def cmd_run(settings: Settings) -> None:
    bot, portal, _ = build(settings)
    try:
        await bot.run_forever()
    finally:
        await portal.close()


async def cmd_once(settings: Settings) -> None:
    bot, portal, _ = build(settings)
    try:
        seconds = await bot.cycle(dwell=False)
        usage = bot.last_usage
        print(f"Remaining: {usage.short() if usage else '?'}  |  next check would be in {seconds:.0f} s")
    finally:
        await portal.close()


async def cmd_recon(settings: Settings) -> None:
    """Log in once and save what the overview looks like, for selector tuning."""
    _, portal, _ = build(settings)
    out = settings.state_dir / "recon" / f"{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    try:
        await portal.prepare()
        await asyncio.sleep(6)
        page = portal.page
        await page.screenshot(path=str(out / "overview.png"), full_page=True)
        (out / "overview.txt").write_text(f"URL: {page.url}\n\n{await portal.browser.deep_dump()}", encoding="utf-8")
        try:
            usage = await portal.read_usage(timeout_s=10)
            print(f"Parsed usage: {usage}")
        except Exception as exc:  # noqa: BLE001
            print(f"Usage NOT parsed: {exc}")
        try:
            _, label = await portal.find_claim_button()
            print(f"Rebook button candidate: {label!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"Rebook button not found: {exc}")
        portal.browser.save_api(out / "api.json")
        print(f"Recon saved to {out}")
    finally:
        await portal.close()


async def cmd_test_notify(settings: Settings) -> None:
    from .notify import Notifier

    ok = await Notifier(settings).send("👋 Test message from your ALDI TALK bot.")
    print("Telegram OK" if ok else "Telegram NOT sent (check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")


async def cmd_chat_id(settings: Settings) -> None:
    from .notify import Notifier

    chats = await Notifier(settings).get_chat_ids()
    if not chats:
        print("No chats found. Send /start to your bot in Telegram, then run this again.")
    for chat_id, name in chats:
        print(f"{chat_id}\t{name}")


async def cmd_fingerprint(settings: Settings) -> None:
    from .browser import BrowserSession

    session = BrowserSession(settings)
    try:
        page = await session.start()
        from .auth import goto, settle

        await goto(page, "https://bot.sannysoft.com/")
        await settle(page)
        await asyncio.sleep(3)
        out = settings.state_dir / "debug" / "fingerprint.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out), full_page=True)
        info = await page.evaluate(
            """() => ({webdriver: navigator.webdriver, userAgent: navigator.userAgent,
                      languages: navigator.languages, plugins: navigator.plugins.length,
                      chrome: typeof window.chrome, platform: navigator.platform,
                      hardwareConcurrency: navigator.hardwareConcurrency,
                      screen: [screen.width, screen.height], tz: Intl.DateTimeFormat().resolvedOptions().timeZone})"""
        )
        failed = await page.locator("td.failed").all_inner_texts()
        print(json.dumps(info, indent=1))
        print(f"sannysoft failed checks: {failed or 'none'}")
        print(f"Screenshot: {out}")
    finally:
        await session.close()


COMMANDS = {
    "run": cmd_run,
    "once": cmd_once,
    "recon": cmd_recon,
    "test-notify": cmd_test_notify,
    "telegram-chat-id": cmd_chat_id,
    "fingerprint-check": cmd_fingerprint,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="alditalk", description="Auto-claim the free ALDI TALK 1 GB rebook.")
    parser.add_argument("command", nargs="?", default="run", choices=sorted([*COMMANDS, "dashboard"]))
    parser.add_argument("--port", type=int, help="dashboard port (default 8377)")
    parser.add_argument("--dry-run", action="store_true", help="never click the rebook button")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--headless", action="store_true", help="run without a window")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.command == "dashboard":  # needs no credentials: reads state/ only
        from .dashboard import serve

        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )
        try:
            serve(port=args.port)
        except KeyboardInterrupt:
            pass
        return

    overrides: dict[str, object] = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.headed:
        overrides["headless"] = False
    if args.headless:
        overrides["headless"] = True
    settings = Settings(**overrides)  # type: ignore[arg-type]
    setup_logging(settings, args.verbose)

    async def runner() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)  # type: ignore[union-attr]
        await COMMANDS[args.command](settings)

    needs_lock = args.command in ("run", "once", "recon")
    with single_instance(settings.state_dir) if needs_lock else contextlib.nullcontext():
        try:
            asyncio.run(runner())
        except asyncio.CancelledError:
            logging.getLogger("alditalk").info("Stopped")


if __name__ == "__main__":
    main()
