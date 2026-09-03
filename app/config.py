"""Environment-backed configuration with validation at startup.

Every setting is read once, here, so a misconfigured deployment fails loudly on
boot rather than silently doing nothing at 03:00 on a Sunday.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


REPORT = "report"
ENFORCE = "enforce"


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or set {name} as a secret on your host."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _parse_admin_ids(raw: str) -> List[int]:
    ids: List[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"ADMIN_USER_IDS must be numeric Telegram user ids, got {chunk!r}"
            ) from exc
    if not ids:
        raise ConfigError(
            "ADMIN_USER_IDS is empty. Send /whoami to the bot to find your id."
        )
    return ids


def _parse_drip(raw: str) -> List[Tuple[int, str]]:
    """Parse "0|Welcome;;24|Day two" into [(0, "Welcome"), (24, "Day two")]."""
    steps: List[Tuple[int, str]] = []
    if not raw:
        return steps
    for entry in raw.split(";;"):
        entry = entry.strip()
        if not entry:
            continue
        head, sep, message = entry.partition("|")
        if not sep or not message.strip():
            raise ConfigError(
                f"ONBOARDING_DRIP entry {entry!r} should look like '24|Your message'"
            )
        try:
            hours = int(head.strip())
        except ValueError as exc:
            raise ConfigError(
                f"ONBOARDING_DRIP entry {entry!r} needs a whole number of hours "
                f"before the '|'"
            ) from exc
        if hours < 0:
            raise ConfigError("ONBOARDING_DRIP hours cannot be negative")
        steps.append((hours, message.strip()))
    return sorted(steps, key=lambda step: step[0])


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_webhook_secret: str
    admin_user_ids: List[int]

    stripe_api_key: str
    stripe_webhook_secret: str
    stripe_payment_link: str

    enforcement_mode: str
    grace_period_days: int
    reconcile_interval_minutes: int
    onboarding_drip: List[Tuple[int, str]] = field(default_factory=list)

    public_base_url: str = ""
    port: int = 8080
    db_path: str = "gatekeeper.db"
    log_level: str = "INFO"

    @property
    def enforcing(self) -> bool:
        return self.enforcement_mode == ENFORCE

    @property
    def use_webhook(self) -> bool:
        return bool(self.public_base_url)

    @property
    def telegram_webhook_path(self) -> str:
        # The secret is also in the path so stray internet scans never reach
        # the handler at all.
        return f"/telegram/{self.telegram_webhook_secret}"


def load_config() -> Config:
    mode = _optional("ENFORCEMENT_MODE", REPORT).lower()
    if mode not in (REPORT, ENFORCE):
        raise ConfigError(
            f"ENFORCEMENT_MODE must be '{REPORT}' or '{ENFORCE}', got {mode!r}"
        )

    chat_id_raw = _require("TELEGRAM_CHAT_ID")
    try:
        chat_id = int(chat_id_raw)
    except ValueError as exc:
        raise ConfigError(
            f"TELEGRAM_CHAT_ID must be numeric (supergroups look like "
            f"-1001234567890), got {chat_id_raw!r}"
        ) from exc

    base_url = _optional("PUBLIC_BASE_URL").rstrip("/")
    if base_url and not base_url.startswith("https://"):
        raise ConfigError(
            "PUBLIC_BASE_URL must be an https:// address - Telegram refuses "
            "plain http webhooks."
        )

    webhook_secret = _require("TELEGRAM_WEBHOOK_SECRET")
    # Telegram only accepts these characters in secret_token, and the secret
    # also forms part of our URL path.
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", webhook_secret):
        raise ConfigError(
            "TELEGRAM_WEBHOOK_SECRET must be 16-256 characters of letters, "
            "digits, underscore or hyphen. Generate one with: "
            "openssl rand -hex 32"
        )

    grace = _int("GRACE_PERIOD_DAYS", 3)
    if grace < 0:
        raise ConfigError("GRACE_PERIOD_DAYS cannot be negative")

    interval = _int("RECONCILE_INTERVAL_MINUTES", 60)
    if interval < 1:
        raise ConfigError("RECONCILE_INTERVAL_MINUTES must be at least 1")

    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=chat_id,
        telegram_webhook_secret=webhook_secret,
        admin_user_ids=_parse_admin_ids(_require("ADMIN_USER_IDS")),
        stripe_api_key=_require("STRIPE_API_KEY"),
        stripe_webhook_secret=_require("STRIPE_WEBHOOK_SECRET"),
        stripe_payment_link=_require("STRIPE_PAYMENT_LINK"),
        enforcement_mode=mode,
        grace_period_days=grace,
        reconcile_interval_minutes=interval,
        onboarding_drip=_parse_drip(_optional("ONBOARDING_DRIP")),
        public_base_url=base_url,
        port=_int("PORT", 8080),
        db_path=_optional("DB_PATH", "gatekeeper.db"),
        log_level=_optional("LOG_LEVEL", "INFO").upper(),
    )
