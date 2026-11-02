# ALDI TALK auto-rebook bot

Stays logged in to the ALDI TALK Kundenportal 24/7. Whenever remaining data drops below 1 GB,
it claims the free 1 GB ("Unlimited on Demand"). Each claim and each problem is reported to you on Telegram.

It behaves like a person: irregular check times, slower checks at night, key-by-key typing, mouse paths, and varied
navigation. Its identity stays fixed: one Chrome profile and your home IP.

## Setup

1. Fill `ALDI_PHONE` and `ALDI_PASSWORD` in `.env` (keep the single quotes). The file is `chmod 600`.
2. Telegram: send `/start` to your bot, then `uv run python -m alditalk telegram-chat-id` and put the id into
   `TELEGRAM_CHAT_ID`. Check it with `uv run python -m alditalk test-notify`.
3. Optional: create a free check on https://healthchecks.io (period 1 h, grace 30 min) and set `HEALTHCHECK_URL`.

## Commands (local, for testing)

```bash
uv run python -m alditalk once --dry-run --headed   # log in, read volume, show what it WOULD click
uv run python -m alditalk recon --headed            # dump overview DOM + API JSON into state/recon/
uv run python -m alditalk once                      # one real check (claims if < 1 GB)
uv run python -m alditalk fingerprint-check         # bot-detection self test -> state/debug/fingerprint.png
uv run pytest                                       # offline tests
```

## Run 24/7 (Docker)

```bash
docker compose up -d --build
docker compose logs -f
docker compose ps        # STATUS shows (healthy) once heartbeats arrive
```

`restart: unless-stopped` brings it back after crashes and reboots. Your state, Chrome profile, logs and debug dumps
live in `./state/`.

### Dashboard: http://localhost:8377

A second container serves a local, read-only dashboard:
- **Live status:** running, checking, claiming, retrying, or not responding, with a countdown to the next check.
- **Data left:** current Inland data and when it was last checked. Also the usage rate and when you'll hit the 1 GB line.
- **Stats for the selected range** (24 h / 7 days / 30 days): free GB claimed, data used, checks, logins, failed checks.
- **Charts:**
  - data left over time, with every claim marked;
  - data used vs. claimed per hour or day;
  - the spread of wait times between checks, which shows the randomization.
- **History tabs:** every check (how the page was opened, login, duration, result), every claim, and events
  (starts, logins, errors, recoveries).

It reads `state/history.db` (kept for 90 days) and needs no credentials. It only listens on localhost and rejects
other `Host` headers. Without Docker: `uv run python -m alditalk dashboard`.

### When it runs

The bot runs **whenever WSL is running**. When WSL starts, Docker starts and brings both containers back. When WSL
shuts down (shortly after your last WSL window closes, or when the PC sleeps), they stop. Nothing is lost.
While it's off, nothing claims for you.

- Pause without stopping: the dashboard's **Pause** button (stays paused across restarts).
- Stop completely: `docker compose stop`; bring it back with `docker compose start`.
- Optional, true 24/7: `windows/install-keepalive.ps1` (Administrator PowerShell) keeps WSL up and the PC awake on AC
  power. Undo: `Unregister-ScheduledTask -TaskName 'AldiTalk WSL keep-alive'`.

## How often it checks

Every wait is randomized. Typical ranges:

| Situation | Gap between checks |
|---|---|
| more than 5 GB left (daytime) | 11–16 min, to keep the portal session alive (it logs out after ~20 min idle) |
| 2–5 GB left | ~3 min, up to 10 min |
| under 2 GB left | 45 s – 3 min |
| night, 01:00–07:00 | 20–40 min, unless data is being used fast (the session may lapse, then one login in the morning) |

It also projects when you'll cross 1 GB and always looks again before that. Below 1 GB it claims after a short
random delay. **Check now** and **Pause/Resume** on the dashboard wake the bot within ~2 s; manual checks are
refused within 30 s of the previous check.

## Safety limits

- 1 claim per check, 3 min cooldown, max 5 claims/hour. Hitting the cap sends an alert and pauses claims.
- Max 3 failed logins per 24 h. A wrong password stops logins and alerts you, so the account doesn't get locked.
- Each check has a hard timeout. After 8 failed checks in a row the container restarts.
- Passwords, phone number and token are scrubbed from logs.

## How it reads your data

- **Primary:** the portal's own API call `…/selfcare-dashboard/v1/offers`, which the page makes itself when it loads.
  The bot reads the Inland data pack (`dataGrantAmount`: `allocated − used`, in KB) and never the EU roaming pack.
- **Fallback:** the tile text on the overview ("Noch 44,7 GB von 50 GB · Inland").
- **Claim:** the round `+ 1 GB` button (`one-button[slot="action"]`) inside the Inland tile. After clicking, the bot
  reloads the page and checks that the volume went up by ≥ 800 MB.
- **Always fresh:** each check is a hard refresh (Ctrl+Shift+R, browser cache bypassed). A reading only counts if
  the API response came over the network after that page load. Otherwise the bot hard-refreshes again before
  trusting anything. A hard refresh costs ~1.2 MB vs ~30 KB for a normal reload (the data API is never cached
  either way). Set `HARD_REFRESH=false` to save mobile data.
- Chrome drops session cookies on exit, so they're kept in `state/session-cookies.json` (`chmod 600`). That way a
  restart doesn't have to log in again.

## When something breaks

The portal changed if you get "Check failed" or "volume didn't increase" alerts. Screenshots and a DOM outline
(shadow DOM included) are in `state/debug/`. Run `recon` to see the current page, then adjust the selectors at the
top of `src/alditalk/portal.py` / `auth.py`. `state/` holds personal data (cookies, API dumps). Never share it.
