"""Typed settings, loaded from environment variables and `.env`."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Account -----------------------------------------------------------
    aldi_phone: str = Field(min_length=5)
    aldi_password: SecretStr

    # --- Behaviour ---------------------------------------------------------
    threshold_mb: float = 1024.0  # rebook is only offered below 1 GB
    dry_run: bool = False
    headless: bool = False  # headful under Xvfb looks more like a real browser
    browser_channel: str = "chrome"  # "chrome" (real Google Chrome) or "chromium"
    state_dir: Path = Path("state")
    timezone: str = "Europe/Berlin"

    # --- Polling (seconds). Base = used when consumption rate is unknown,
    #     cap = longest sleep allowed in that tier. Everything gets jittered.
    tier_high_mb: float = 5120.0
    tier_mid_mb: float = 2048.0
    poll_base_high_s: float = 600.0
    poll_base_mid_s: float = 180.0
    poll_base_low_s: float = 75.0
    poll_cap_high_s: float = 1800.0
    poll_cap_mid_s: float = 600.0
    poll_cap_low_s: float = 180.0
    min_interval_s: float = 45.0

    # --- Quiet hours (local time): long sleeps unless data is being used fast
    quiet_start_hour: int = Field(default=1, ge=0, le=23)
    quiet_end_hour: int = Field(default=7, ge=0, le=23)
    quiet_min_s: float = 1200.0
    quiet_max_s: float = 2400.0
    quiet_busy_rate_mb_min: float = 5.0

    # --- Refresh: True = Ctrl+Shift+R every time (~1.2 MB per check, measured 2026-11-02);
    #     False = normal reload (~30 KB). Either way readings must come from the network, not the cache.
    hard_refresh: bool = True

    # --- Session keep-alive: the portal logs out after ~20 min idle (measured 2026-11-02).
    #     Outside quiet hours, never wait longer than this, so it rarely has to log in again.
    keep_session_alive: bool = True
    session_keepalive_min_s: float = 660.0
    session_keepalive_max_s: float = 960.0

    # --- Claiming ----------------------------------------------------------
    claim_delay_min_s: float = 20.0
    claim_delay_max_s: float = 150.0
    claim_delay_floor_mb: float = 400.0  # below this, claim right away
    claim_cooldown_s: float = 180.0
    max_claims_per_hour: int = 5
    claim_verify_min_gain_mb: float = 800.0

    # --- Login & robustness ---------------------------------------------------
    max_login_attempts_per_day: int = 3
    bad_credentials_pause_s: float = 1800.0
    browser_recycle_hours: float = 6.0
    cycle_timeout_s: float = 420.0
    max_consecutive_failures: int = 8

    # --- Notifications -----------------------------------------------------
    telegram_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    healthcheck_url: str | None = None
    daily_summary_hour: int = Field(default=9, ge=0, le=23)

    @field_validator("telegram_token", "telegram_chat_id", "healthcheck_url", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        out = [self.aldi_password.get_secret_value(), self.aldi_phone]
        if self.telegram_token:
            out.append(self.telegram_token.get_secret_value())
        return [s for s in out if s and len(s) >= 4]
