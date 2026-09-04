#!/usr/bin/env python3
"""Find the two ids you need before the bot can start.

The bot refuses to boot without a valid TELEGRAM_CHAT_ID, which makes its own
/whereami command useless for finding that id in the first place. This script
solves that chicken-and-egg: it talks to Telegram directly, with nothing
installed.

    1. Add the bot to your group AND promote it to administrator
    2. In the group, send a command like /hello  (see the note below)
    3. Send /start to the bot in a private chat
    4. py discover.py <your-bot-token>

Note on group messages: by default Telegram bots have "privacy mode" on, which
means they only see messages that are commands (starting with /) or replies to
them. A plain "hello" is invisible to the bot. Promoting the bot to admin, or
sending a /command, makes it visible.

Standard library only.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

API = "https://api.telegram.org"


def call(token: str, method: str) -> dict:
    url = f"{API}/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "description": f"HTTP {exc.code}: {body[:200]}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "description": f"network error: {exc.reason}"}


def walk(node, chats: dict, users: dict) -> None:
    """Find every chat and user object anywhere in an update, whatever its type.

    Telegram has many update shapes (message, channel_post, my_chat_member,
    chat_member, edited_message...). Rather than enumerate them, look for the
    shapes of the objects we care about wherever they appear.
    """
    if isinstance(node, dict):
        # A chat object has an id and a type of group/supergroup/private/channel
        if "id" in node and node.get("type") in (
            "private",
            "group",
            "supergroup",
            "channel",
        ):
            chats[node["id"]] = node
        # A user object has an id and an is_bot flag
        elif "id" in node and "is_bot" in node and not node.get("is_bot"):
            users[node["id"]] = node
        for value in node.values():
            walk(value, chats, users)
    elif isinstance(node, list):
        for item in node:
            walk(item, chats, users)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    token = sys.argv[1].strip()

    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"Bad token: {me.get('description')}")
        return 1
    bot = me["result"]
    print(f"Bot: @{bot.get('username')} (id {bot.get('id')})\n")

    updates = call(token, "getUpdates")
    if not updates.get("ok"):
        print(f"Could not read updates: {updates.get('description')}")
        print(
            "\nIf this mentions a webhook, clear it first by opening this in a "
            f"browser:\n  {API}/bot<YOUR_TOKEN>/deleteWebhook"
        )
        return 1

    results = updates.get("result", [])
    print(f"{len(results)} update(s) waiting.")
    if results:
        kinds = sorted(
            {key for update in results for key in update if key != "update_id"}
        )
        print(f"Types seen: {', '.join(kinds)}\n")
    else:
        print(
            "\nNothing waiting. Telegram only keeps updates for a short while, so:\n"
            "  1. make sure the bot is IN your group and promoted to admin\n"
            "  2. send a command in the group, e.g.  /hello\n"
            "     (a plain 'hello' is invisible to bots - see privacy mode)\n"
            "  3. send /start to the bot in a private chat\n"
            "then run this again straight away."
        )
        return 1

    chats: dict = {}
    users: dict = {}
    walk(results, chats, users)

    print("Chats seen")
    print("-" * 62)
    if not chats:
        print("  (none)")
    for chat_id, chat in chats.items():
        kind = chat.get("type", "?")
        title = chat.get("title") or chat.get("username") or chat.get("first_name", "")
        flag = "   <-- TELEGRAM_CHAT_ID" if kind in ("group", "supergroup") else ""
        print(f"  {chat_id:>16}  {kind:<11} {title}{flag}")

    print("\nUsers seen")
    print("-" * 62)
    if not users:
        print("  (none)")
    for user_id, user in users.items():
        name = user.get("first_name", "")
        handle = f"@{user['username']}" if user.get("username") else ""
        print(f"  {user_id:>16}  {name} {handle}   <-- ADMIN_USER_IDS")

    if not chats and not users:
        print("\nRaw updates, so we can see what Telegram actually sent:")
        print(json.dumps(results, indent=2)[:3000])

    print(
        "\nIf your group shows as 'group' rather than 'supergroup', its id will\n"
        "change when Telegram upgrades it (which happens as soon as you change\n"
        "group permissions or history settings). Promote the bot to admin first,\n"
        "then re-run this and use the id you get afterwards."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
