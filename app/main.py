"""Entrypoint. One process: web server, background sweep, job queue."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import aiohttp
from aiohttp import web

from .access import AccessService
from .config import Config, ConfigError, load_config
from .db import Database
from .handlers import UpdateHandler
from .scheduler import Scheduler
from .stripe_client import StripeClient
from .telegram import TelegramClient, TelegramError
from .webhooks import WebhookRoutes

log = logging.getLogger("gatekeeper")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def preflight(cfg: Config, tg: TelegramClient) -> None:
    """Fail loudly at boot rather than quietly at 03:00."""
    me = await tg.get_me()
    log.info("Connected to Telegram as @%s", me.get("username"))

    try:
        member = await tg.get_chat_member(cfg.telegram_chat_id, me["id"])
    except TelegramError as exc:
        raise ConfigError(
            f"Cannot read the gated chat {cfg.telegram_chat_id}: {exc.description}\n"
            f"Add the bot to the group first, then check TELEGRAM_CHAT_ID. "
            f"Send /whereami in the group to have the bot print the right value."
        ) from exc

    if member.get("status") != "administrator":
        raise ConfigError(
            f"The bot is in the chat but is not an administrator, so it cannot "
            f"create invite links or remove anyone. Promote it and give it "
            f"'Invite users via link' and 'Ban users'."
        )
    if not member.get("can_invite_users"):
        raise ConfigError(
            "The bot is an admin but lacks 'Invite users via link', so it cannot "
            "issue invites."
        )
    if not member.get("can_restrict_members"):
        log.warning(
            "The bot lacks 'Ban users', so it can invite but cannot remove. "
            "Report mode will still work; enforcement will not."
        )

    if cfg.enforcing:
        log.warning(
            "ENFORCEMENT_MODE=enforce - members WILL be removed. "
            "Run in 'report' first if you have not already."
        )
    else:
        log.info(
            "ENFORCEMENT_MODE=report - nobody will be removed, decisions are "
            "logged only. Use /stats to see what enforcing would do."
        )


async def polling_loop(tg: TelegramClient, handler: UpdateHandler) -> None:
    """Fallback when no public URL is configured."""
    log.info("No PUBLIC_BASE_URL set - falling back to long polling")
    await tg.delete_webhook()
    offset = 0
    while True:
        try:
            updates = await tg.get_updates(offset)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("getUpdates failed: %s", exc)
            await asyncio.sleep(5)
            continue
        for update in updates or []:
            offset = update["update_id"] + 1
            await handler.handle(update)


async def run() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nConfiguration problem:\n\n  {exc}\n", file=sys.stderr)
        raise SystemExit(2)

    configure_logging(cfg.log_level)

    db = Database(cfg.db_path)
    await db.connect()
    log.info("Database ready at %s", cfg.db_path)

    session = aiohttp.ClientSession()
    tg = TelegramClient(cfg.telegram_bot_token, session)
    stripe = StripeClient(cfg.stripe_api_key, session)
    access = AccessService(cfg, db, tg, stripe)
    handler = UpdateHandler(cfg, db, tg, stripe, access)
    scheduler = Scheduler(cfg, db, tg, access, handler)

    poller: Optional[asyncio.Task] = None
    runner: Optional[web.AppRunner] = None

    try:
        try:
            await preflight(cfg, tg)
        except ConfigError as exc:
            print(f"\nStartup check failed:\n\n  {exc}\n", file=sys.stderr)
            raise SystemExit(2)

        app = web.Application()
        WebhookRoutes(cfg, db, stripe, access, handler).register(app)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", cfg.port)
        await site.start()
        log.info("Listening on port %s", cfg.port)

        if cfg.use_webhook:
            url = f"{cfg.public_base_url}{cfg.telegram_webhook_path}"
            await tg.set_webhook(url, cfg.telegram_webhook_secret)
            log.info("Telegram webhook registered")
            log.info(
                "Point your Stripe webhook endpoint at %s/stripe",
                cfg.public_base_url,
            )
        else:
            poller = asyncio.create_task(polling_loop(tg, handler), name="polling")

        scheduler.start()
        log.info("Gatekeeper is up")

        await asyncio.Event().wait()

    finally:
        log.info("Shutting down")
        await scheduler.stop()
        if poller:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
        if runner:
            await runner.cleanup()
        await session.close()
        await db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
