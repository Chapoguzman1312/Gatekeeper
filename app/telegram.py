"""A thin Telegram Bot API client.

Deliberately not a bot framework. This service needs eight API methods, and
eight hand-written methods are easier to reason about - and to keep working
across Telegram's API changes - than a dependency that wraps all two hundred.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Statuses Telegram reports for a chat member. The first three mean the person
# is currently inside the chat.
PRESENT_STATUSES = {"creator", "administrator", "member", "restricted"}
ABSENT_STATUSES = {"left", "kicked"}
PROTECTED_STATUSES = {"creator", "administrator"}


class TelegramError(RuntimeError):
    def __init__(self, method: str, description: str, error_code: Optional[int] = None):
        super().__init__(f"{method} failed: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code


class TelegramClient:
    def __init__(self, token: str, session: aiohttp.ClientSession) -> None:
        self._token = token
        self._session = session

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        payload = {k: v for k, v in params.items() if v is not None}

        for attempt in range(4):
            try:
                async with self._session.post(url, json=payload, timeout=30) as resp:
                    body = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 3:
                    raise TelegramError(method, f"network error: {exc}") from exc
                await asyncio.sleep(2**attempt)
                continue

            if body.get("ok"):
                return body.get("result")

            description = body.get("description", "unknown error")
            code = body.get("error_code")

            # 429: Telegram tells us exactly how long to wait.
            if code == 429:
                retry_after = int(
                    body.get("parameters", {}).get("retry_after", 2**attempt)
                )
                log.warning("Rate limited on %s, sleeping %ss", method, retry_after)
                await asyncio.sleep(retry_after)
                continue

            # 5xx is Telegram's problem and usually transient.
            if code and 500 <= code < 600 and attempt < 3:
                await asyncio.sleep(2**attempt)
                continue

            raise TelegramError(method, description, code)

        raise TelegramError(method, "exhausted retries")

    # -- identity -----------------------------------------------------------

    async def get_me(self) -> Dict[str, Any]:
        return await self._call("getMe")

    # -- messaging ----------------------------------------------------------

    async def send_message(
        self,
        chat_id: int,
        text: str,
        disable_preview: bool = True,
        parse_mode: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Send a DM or group message.

        Returns None instead of raising when the user has never started the bot
        or has blocked it - a normal, expected state that must not abort a
        webhook or a sweep.
        """
        try:
            return await self._call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                link_preview_options={"is_disabled": True} if disable_preview else None,
            )
        except TelegramError as exc:
            if exc.error_code in (400, 403):
                log.info("Cannot message %s: %s", chat_id, exc.description)
                return None
            raise

    # -- membership ---------------------------------------------------------

    async def get_chat_member(self, chat_id: int, user_id: int) -> Dict[str, Any]:
        return await self._call("getChatMember", chat_id=chat_id, user_id=user_id)

    async def is_in_chat(self, chat_id: int, user_id: int) -> Optional[bool]:
        """True/False, or None when Telegram will not tell us."""
        try:
            member = await self.get_chat_member(chat_id, user_id)
        except TelegramError as exc:
            if exc.error_code == 400:
                return None
            raise
        return member.get("status") in PRESENT_STATUSES

    async def is_protected(self, chat_id: int, user_id: int) -> bool:
        """Owners and admins are never removed, whatever Stripe says."""
        try:
            member = await self.get_chat_member(chat_id, user_id)
        except TelegramError:
            return False
        return member.get("status") in PROTECTED_STATUSES

    async def create_single_use_invite(
        self, chat_id: int, name: str = "", expires_in: int = 24 * 3600
    ) -> str:
        result = await self._call(
            "createChatInviteLink",
            chat_id=chat_id,
            name=name[:32] or None,
            member_limit=1,
            expire_date=int(time.time()) + expires_in,
        )
        return result["invite_link"]

    async def revoke_invite(self, chat_id: int, invite_link: str) -> None:
        try:
            await self._call(
                "revokeChatInviteLink", chat_id=chat_id, invite_link=invite_link
            )
        except TelegramError as exc:
            log.info("Could not revoke invite: %s", exc.description)

    async def remove_member(self, chat_id: int, user_id: int) -> None:
        """Kick without a permanent ban, so they can rejoin if they pay again.

        banChatMember followed by unbanChatMember is the documented way to do
        this: the ban ejects them, the unban clears the block.
        """
        await self._call("banChatMember", chat_id=chat_id, user_id=user_id)
        await self._call(
            "unbanChatMember", chat_id=chat_id, user_id=user_id, only_if_banned=True
        )

    # -- updates ------------------------------------------------------------

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self._call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=["message", "chat_member", "my_chat_member"],
            drop_pending_updates=True,
        )

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", drop_pending_updates=False)

    async def get_updates(self, offset: int, timeout: int = 30) -> Any:
        return await self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "chat_member", "my_chat_member"],
        )
