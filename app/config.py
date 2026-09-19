"""Configuration.

Everything the system is allowed to decide on its own is bounded by a value in
here.  The guardrail defaults are deliberately conservative placeholders --
they are *not* legal advice and must be reviewed against local labor rules,
union agreements and your own policy before this runs against real staff.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- infrastructure -------------------------------------------------
    database_url: str = "sqlite:///./managers.db"
    business_tz: str = "UTC"
    public_base_url: str = "http://localhost:8000"

    # --- messaging ------------------------------------------------------
    dry_run: bool = True
    """When true (the default), no message ever leaves the process."""

    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_manager_channel: str = "#managers"
    coverage_link_secret: str = "dev-insecure-change-me"
    coverage_link_ttl_minutes: int = 180

    # --- autonomy switches ----------------------------------------------
    auto_reassign_enabled: bool = True
    """Reassign a task to an on-shift colleague without asking a manager."""

    auto_coverage_requests_enabled: bool = True
    """Message off-shift staff to ask for cover without asking a manager."""

    # --- hard guardrails: no automatic action may breach these -----------
    max_daily_hours: float = 10.0
    max_weekly_hours: float = 48.0
    min_rest_hours: float = 11.0
    quiet_hours_start: int = 21
    quiet_hours_end: int = 7
    quiet_hours_override_for_critical: bool = True
    """Critical work (a driver is coming) may break quiet hours; routine work may not."""

    max_coverage_asks_per_day: int = 3
    """Anti-spam: nobody gets pinged for cover more than this many times a day."""

    # --- timing ----------------------------------------------------------
    coverage_response_timeout_minutes: int = 10
    escalation_lead_minutes: int = 15
    """With no coverage this close to pickup, page a manager regardless of timeouts."""

    critical_pickup_grace_minutes: int = 30
    """A pickup this soon after the leave starts still counts as a collision."""

    coverage_batch_size: int = 3
    """How many people are asked in the first wave."""

    # --- soft scoring weights (tune without touching code) ---------------
    weight_continuity: float = 40.0
    weight_on_shift: float = 30.0
    weight_idle: float = 20.0
    weight_proficiency: float = 10.0
    weight_fairness: float = 15.0

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_bot_token) and not self.dry_run


@lru_cache
def get_settings() -> Settings:
    return Settings()
