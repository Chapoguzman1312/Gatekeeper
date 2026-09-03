"""Telegram update handling: member commands, admin commands, join/leave events."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .access import AccessService
from .config import Config
from .db import Database, now
from .entitlements import Access, evaluate_member
from .stripe_client import StripeClient
from .telegram import PRESENT_STATUSES, TelegramClient

log = logging.getLogger(__name__)

MEMBER_HELP = (
    "What I can do:\n\n"
    "/start - get your payment link, or your invite if you've already paid\n"
    "/status - check when your access runs to\n"
    "/link - get a fresh invite link if the last one expired\n"
    "/help - this message"
)

ADMIN_HELP = (
    "\n\nAdmin only:\n"
    "/stats - membership and subscription numbers\n"
    "/audit - the last 20 things I did\n"
    "/sync - re-check everyone against Stripe now\n"
    "/grant <user_id> - let someone in manually (comped members, refunds)\n"
    "/revoke <user_id> - remove someone manually\n"
    "/whereami - the chat id of the group you send this in"
)


def _fmt_date(ts: Optional[int]) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y")


class UpdateHandler:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        telegram: TelegramClient,
        stripe: StripeClient,
        access: AccessService,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.tg = telegram
        self.stripe = stripe
        self.access = access

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.cfg.admin_user_ids

    # -- entrypoint ---------------------------------------------------------

    async def handle(self, update: Dict[str, Any]) -> None:
        try:
            if "message" in update:
                await self._on_message(update["message"])
            elif "chat_member" in update:
                await self._on_chat_member(update["chat_member"])
            elif "my_chat_member" in update:
                await self._on_my_chat_member(update["my_chat_member"])
        except Exception:
            log.exception("Failed handling update %s", update.get("update_id"))

    # -- messages -----------------------------------------------------------

    async def _on_message(self, message: Dict[str, Any]) -> None:
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        sender = message.get("from") or {}
        user_id = sender.get("id")
        if user_id is None:
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        is_private = chat.get("type") == "private"

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()

        # /whereami is the one command meant to be used inside the group.
        if command == "/whereami":
            if self._is_admin(user_id):
                await self.tg.send_message(
                    chat_id,
                    f"This chat's id is {chat_id}\n\n"
                    f"Put it in TELEGRAM_CHAT_ID and restart me.",
                )
            return

        if not is_private:
            return

        handlers = {
            "/start": self._cmd_start,
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/link": self._cmd_link,
            "/whoami": self._cmd_whoami,
            "/stats": self._cmd_stats,
            "/audit": self._cmd_audit,
            "/sync": self._cmd_sync,
            "/grant": self._cmd_grant,
            "/revoke": self._cmd_revoke,
        }
        handler = handlers.get(command)
        if handler is None:
            await self.tg.send_message(user_id, MEMBER_HELP)
            return
        await handler(user_id, sender, argument)

    # -- member commands ----------------------------------------------------

    async def _cmd_start(
        self, user_id: int, sender: Dict[str, Any], argument: str
    ) -> None:
        member = await self.db.get_member(user_id)

        if member:
            entitlement = evaluate_member(member, self.cfg.grace_period_days)
            if entitlement.allows_entry:
                await self._send_invite_or_confirm(user_id, member)
                return

        link = await self.access.start_checkout(
            user_id, sender.get("username"), sender.get("first_name")
        )
        await self.tg.send_message(
            user_id,
            "Here's your link to join:\n\n"
            f"{link}\n\n"
            "Once the payment goes through I'll send your invite here "
            "automatically - usually within a few seconds.",
        )

    async def _cmd_help(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        text = MEMBER_HELP + (ADMIN_HELP if self._is_admin(user_id) else "")
        await self.tg.send_message(user_id, text)

    async def _cmd_whoami(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        await self.tg.send_message(
            user_id,
            f"Your Telegram user id is {user_id}\n\n"
            f"Put it in ADMIN_USER_IDS to give yourself admin commands.",
        )

    async def _cmd_status(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        member = await self.db.get_member(user_id)
        if member is None:
            await self.tg.send_message(
                user_id, "I have no subscription on file for you. Send /start to join."
            )
            return

        entitlement = evaluate_member(member, self.cfg.grace_period_days)
        until = _fmt_date(entitlement.entitled_until)

        if entitlement.access is Access.ENTITLED:
            body = f"Active. Your access runs to {until}."
        elif entitlement.access is Access.GRACE:
            body = (
                f"Your last payment didn't go through. You keep access until "
                f"{until} while Stripe retries the card."
            )
        elif entitlement.access is Access.REVOKED:
            body = "Not active. Send /start if you'd like to rejoin."
        else:
            body = "I'm not sure right now - give me a few minutes and try again."

        await self.tg.send_message(user_id, body)

    async def _cmd_link(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        member = await self.db.get_member(user_id)
        if member is None:
            await self.tg.send_message(
                user_id, "No subscription on file. Send /start to join."
            )
            return

        entitlement = evaluate_member(member, self.cfg.grace_period_days)
        if not entitlement.allows_entry:
            await self.tg.send_message(
                user_id,
                "Your subscription isn't active, so I can't issue an invite. "
                "Send /start to resubscribe.",
            )
            return

        await self._send_invite_or_confirm(user_id, member)

    async def _send_invite_or_confirm(
        self, user_id: int, member: Dict[str, Any]
    ) -> None:
        if await self.tg.is_in_chat(self.cfg.telegram_chat_id, user_id):
            await self.db.upsert_member(user_id, in_chat=1)
            await self.tg.send_message(user_id, "You're already in the group.")
            return

        invite = await self.tg.create_single_use_invite(
            self.cfg.telegram_chat_id, name=f"u{user_id}"
        )
        await self.db.record("invite_reissued", user_id)
        await self.tg.send_message(
            user_id,
            f"Here's your invite:\n\n{invite}\n\n"
            "It works once and expires in 24 hours.",
        )

    # -- admin commands -----------------------------------------------------

    async def _cmd_stats(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        if not self._is_admin(user_id):
            return

        counts = await self.db.count_by_status()
        members = await self.db.all_members()
        in_chat = sum(1 for m in members if m.get("in_chat"))
        month_ago = now() - 30 * 24 * 3600

        joined = await self.db.count_actions_since("joined_chat", month_ago)
        removed = await self.db.count_actions_since("removed", month_ago)
        would = await self.db.count_actions_since("would_remove", month_ago)

        entitled = counts.get(Access.ENTITLED.value, 0)
        grace = counts.get(Access.GRACE.value, 0)
        revoked = counts.get(Access.REVOKED.value, 0)

        lines = [
            f"Mode: {self.cfg.enforcement_mode.upper()}",
            "",
            f"In the group, known to me: {in_chat}",
            f"Paying now: {entitled}",
            f"Payment failing (in grace): {grace}",
            f"Lapsed: {revoked}",
            "",
            f"Last 30 days - joined: {joined}, removed: {removed}",
        ]
        if not self.cfg.enforcing:
            lines.append(f"Would have removed (report mode): {would}")
            if would:
                lines.append("")
                lines.append(
                    "Switch ENFORCEMENT_MODE to 'enforce' when you're happy with "
                    "that number."
                )

        await self.tg.send_message(user_id, "\n".join(lines))

    async def _cmd_audit(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        if not self._is_admin(user_id):
            return
        entries = await self.db.recent_audit(20)
        if not entries:
            await self.tg.send_message(user_id, "Nothing logged yet.")
            return
        lines = []
        for entry in entries:
            stamp = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).strftime(
                "%d %b %H:%M"
            )
            marker = "~" if entry["simulated"] else "-"
            subject = entry["subject_id"] or ""
            detail = f" ({entry['detail']})" if entry["detail"] else ""
            lines.append(f"{marker} {stamp} {entry['action']} {subject}{detail}")
        await self.tg.send_message(user_id, "\n".join(lines))

    async def _cmd_sync(self, user_id: int, sender: Dict[str, Any], _: str) -> None:
        if not self._is_admin(user_id):
            return
        await self.tg.send_message(user_id, "Re-checking everyone against Stripe...")
        summary = await self.run_sweep()
        await self.tg.send_message(user_id, summary)

    async def _cmd_grant(
        self, user_id: int, sender: Dict[str, Any], argument: str
    ) -> None:
        if not self._is_admin(user_id):
            return
        target = _parse_user_id(argument)
        if target is None:
            await self.tg.send_message(user_id, "Usage: /grant <telegram_user_id>")
            return

        from .entitlements import Entitlement

        # Comped access: a year, no Stripe subscription behind it.
        entitlement = Entitlement(
            Access.ENTITLED, now() + 365 * 24 * 3600, "granted manually by admin"
        )
        invite = await self.access.grant(target, entitlement)
        await self.db.record("manual_grant", target, f"by {user_id}")
        await self.tg.send_message(
            user_id,
            f"Granted {target} a year of access."
            + (f"\nInvite: {invite}" if invite else ""),
        )

    async def _cmd_revoke(
        self, user_id: int, sender: Dict[str, Any], argument: str
    ) -> None:
        if not self._is_admin(user_id):
            return
        target = _parse_user_id(argument)
        if target is None:
            await self.tg.send_message(user_id, "Usage: /revoke <telegram_user_id>")
            return
        removed = await self.access.revoke(target, f"manual revoke by {user_id}")
        await self.db.record("manual_revoke", target, f"by {user_id}")
        if removed:
            await self.tg.send_message(user_id, f"Removed {target}.")
        elif not self.cfg.enforcing:
            await self.tg.send_message(
                user_id,
                f"Report mode: logged that {target} would be removed, but left "
                f"them in place.",
            )
        else:
            await self.tg.send_message(
                user_id, f"Did not remove {target} - they're an admin or not in the group."
            )

    # -- membership events --------------------------------------------------

    async def _on_chat_member(self, event: Dict[str, Any]) -> None:
        """Someone's membership of the gated group changed."""
        chat = event.get("chat") or {}
        if chat.get("id") != self.cfg.telegram_chat_id:
            return

        user = event.get("new_chat_member", {}).get("user") or {}
        user_id = user.get("id")
        if user_id is None or user.get("is_bot"):
            return

        status = event.get("new_chat_member", {}).get("status")
        present = status in PRESENT_STATUSES

        await self.db.upsert_member(
            user_id,
            in_chat=1 if present else 0,
            username=user.get("username"),
            first_name=user.get("first_name"),
            **({"joined_at": now()} if present else {}),
        )
        await self.db.record("joined_chat" if present else "left_chat", user_id, status)

        if present:
            member = await self.db.get_member(user_id)
            if member and member.get("status") == Access.UNKNOWN.value:
                # Someone got in without ever paying - an old invite link, or
                # they were added by hand. Flag it rather than acting.
                await self.access.alert_admins(
                    f"User {user_id} (@{user.get('username') or 'no username'}) "
                    f"joined the group but has no subscription on file."
                )

    async def _on_my_chat_member(self, event: Dict[str, Any]) -> None:
        """The bot's own status in a chat changed."""
        chat = event.get("chat") or {}
        status = event.get("new_chat_member", {}).get("status")
        log.info("Bot status in chat %s is now %s", chat.get("id"), status)
        if chat.get("id") == self.cfg.telegram_chat_id and status not in (
            "administrator",
        ):
            await self.access.alert_admins(
                "I am no longer an administrator of the gated group, so I cannot "
                "issue invites or remove anyone. Nothing will be enforced until "
                "that is fixed."
            )

    # -- the sweep ----------------------------------------------------------

    async def run_sweep(self) -> str:
        """Re-check every member against Stripe and apply the result.

        Webhooks are the fast path; this is the one that makes the system
        correct, because webhooks get missed, retried, and dropped.
        """
        from .entitlements import needs_stripe_refresh

        members = await self.db.all_members()
        tally = {"kept": 0, "grace": 0, "removed": 0, "flagged": 0, "skipped": 0}

        for member in members:
            user_id = member["telegram_user_id"]

            if needs_stripe_refresh(member):
                entitlement = await self.access.sync_member(member)
            else:
                entitlement = evaluate_member(member, self.cfg.grace_period_days)

            # Only act on people actually in the group, plus anyone whose
            # entitlement just returned so they can be let back in.
            if not member.get("in_chat") and not entitlement.allows_entry:
                tally["skipped"] += 1
                continue

            outcome = await self.access.apply(user_id, entitlement)
            tally[outcome] = tally.get(outcome, 0) + 1

        await self.db.purge_stale_links()
        await self.db.purge_old_events()

        verb = "removed" if self.cfg.enforcing else "flagged for removal"
        return (
            f"Sweep done over {len(members)} members.\n"
            f"Kept: {tally['kept']}, in grace: {tally['grace']}, "
            f"{verb}: {tally['removed'] + tally['flagged']}, "
            f"skipped: {tally['skipped']}"
        )


def _parse_user_id(argument: str) -> Optional[int]:
    argument = argument.strip()
    if not argument:
        return None
    try:
        return int(argument)
    except ValueError:
        return None
