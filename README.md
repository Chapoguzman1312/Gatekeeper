# Gatekeeper

Subscription gating for paid Telegram communities. Someone pays in Stripe, the
bot lets them in. Their card fails, they get a warning. It keeps failing, they
come out.

The problem it solves is the one every paid-group owner has and nobody fixes by
hand: members cancel and stay in the group forever. Six months in, half the room
isn't paying and the owner has no idea which half.

---

## The demo that sells this

Run it in **report mode** against a group that already exists. It removes
nobody. It reads the membership, matches it against Stripe, and tells the owner
a number they have never seen:

```
Mode: REPORT

In the group, known to me: 340
Paying now: 211
Payment failing (in grace): 12
Lapsed: 117

Would have removed (report mode): 117
```

That gap is the pitch. You don't have to describe the value; they can multiply
117 by their monthly price themselves. Only switch `ENFORCEMENT_MODE` to
`enforce` once they've seen the number and asked you to.

---

## How it works

```
member DMs /start
    -> bot mints a token, hands back the Stripe Payment Link with it attached
    -> member pays
    -> Stripe fires checkout.session.completed with that token
    -> bot matches token to Telegram user, creates a single-use invite, DMs it
    -> member joins; onboarding drip starts

card fails
    -> Stripe fires invoice.payment_failed
    -> bot warns the member, starts the grace clock
    -> grace expires, bot re-checks Stripe, removes them if it's still failing

every hour, regardless
    -> the sweep re-reads everyone from Stripe and fixes whatever the
       webhooks missed
```

The sweep is the part most implementations skip, and it's the part that makes
the difference between a demo and something you can charge a retainer for.
Webhooks get missed, delayed, and delivered twice. The sweep means the group is
correct within the hour no matter what the network did.

---

## Setup

### 1. The bot

Talk to [@BotFather](https://t.me/BotFather), `/newbot`, keep the token.

Add the bot to your group and **promote it to administrator** with:

- *Invite users via link* — without this it cannot let anyone in
- *Ban users* — without this it cannot remove anyone

Then send `/whereami` in the group to get the chat id, and `/whoami` to the bot
in a private chat to get your own user id.

### 2. Stripe

Create a subscription **Payment Link** and copy its URL.

Add a webhook endpoint pointing at `https://your-app-url/stripe`, subscribed to:

```
checkout.session.completed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
invoice.paid
invoice.payment_succeeded
invoice.payment_failed
```

Copy the signing secret (`whsec_...`). A restricted API key with read access to
Customers and Subscriptions is enough — this service never writes to Stripe.

### 3. Configure

```bash
cp .env.example .env
# fill it in
```

Generate the Telegram webhook secret with `openssl rand -hex 32`.

Leave `ENFORCEMENT_MODE=report` for the first run. Always.

### 4. Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

On startup it checks that it can see the group, that it's an admin, and that it
has the permissions it needs — and refuses to start with a specific error if
not, rather than silently doing nothing.

---

## Deploying on a free tier

**The one thing that will bite you:** the SQLite file must live on a mounted
volume. Free-tier filesystems are ephemeral, and if the database is wiped on
redeploy the bot forgets who paid — then the next sweep looks at a group full of
people it has no record of. `fly.toml` mounts a volume at `/data` for exactly
this reason. If you deploy anywhere else, do the equivalent or move to Postgres.

**Fly.io:**

```bash
fly launch --no-deploy
fly volumes create gatekeeper_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... STRIPE_API_KEY=... STRIPE_WEBHOOK_SECRET=...
fly deploy
```

Set `min_machines_running = 1` (already in `fly.toml`) — if the machine sleeps,
the hourly sweep doesn't run and the drip messages don't send.

**Railway / Render:** the `Procfile` works as-is. Attach a persistent disk and
point `DB_PATH` at it.

**No public URL?** Leave `PUBLIC_BASE_URL` blank and the bot falls back to long
polling. Telegram works fine that way, but Stripe webhooks still need a
reachable HTTPS endpoint, so this is really only useful for local development
(pair it with `stripe listen --forward-to localhost:8080/stripe`).

---

## Commands

**Members**

| | |
|---|---|
| `/start` | Payment link, or their invite if they've already paid |
| `/status` | When their access runs to |
| `/link` | A fresh invite if the last one expired |

**Admins** (whoever is listed in `ADMIN_USER_IDS`)

| | |
|---|---|
| `/stats` | The numbers above |
| `/audit` | The last 20 things the bot did — `~` marks simulated actions |
| `/sync` | Force a full sweep now |
| `/grant <user_id>` | Comp someone a year — refunds, moderators, friends |
| `/revoke <user_id>` | Remove someone by hand |
| `/whereami` | The chat id, sent from inside the group |

---

## Design decisions worth knowing

**Nobody is removed on uncertainty.** If Stripe is unreachable, returns a status
the code doesn't recognise, or the cached entitlement has expired without a
fresh sync, the decision is `UNKNOWN` and nothing happens. Removing a paying
member costs you the client; leaving a lapsed one an extra hour costs nothing.

**Grace runs from the end of the paid period, not from now.** Otherwise a
customer whose card failed three weeks ago earns a fresh grace window every time
the sweep runs, and never gets removed.

**Cancelled ≠ expired.** Someone who cancels on the 3rd but has paid to the 28th
keeps access until the 28th. They bought that time.

**Admins are never removed**, regardless of subscription state — checked twice,
against the config list and against Telegram's own admin list.

**Stripe events are idempotent.** Event ids are recorded before processing;
redelivery is a no-op. On a processing failure the id is cleared so Stripe's
retry isn't mistaken for a duplicate.

**Removal is a kick, not a ban.** `banChatMember` followed by
`unbanChatMember`, so a returning customer can rejoin.

**Unmatched payments are never dropped silently.** If a payment arrives that
can't be tied to a Telegram user, the admins get a DM with the customer's email
so nobody who paid is left standing outside.

---

## Tests

```bash
python -m unittest discover tests -v
```

40 tests, standard library only — no dev dependencies. They cover the two things
that would actually cost money if they were wrong: Stripe signature verification
(forged, tampered, replayed, rotated secrets) and the entitlement rule (grace
windows, cancellations, unknown statuses).

The HTTP clients aren't unit tested. Test those against Stripe's test mode and
`stripe trigger`, which exercises the real event shapes.

---

## What isn't here yet

Honest list, in the order I'd add them:

- **Discord.** The access rule and storage are platform-agnostic; it needs a
  second adapter alongside `telegram.py`.
- **A billing portal link.** Right now a member with a failing card has to find
  their own way back to Stripe. `/billing` returning a customer portal session
  would close that loop and cut a lot of support messages.
- **Postgres.** SQLite is right up to a few thousand members. Past that, or if
  you want to run more than one instance, swap the storage layer.
- **Per-tier access.** One group, one price today. Multiple tiers mapping to
  multiple groups is a schema change, not a rewrite.
- **A web dashboard.** The `/stats` output is enough to sell with. Owners will
  ask for a page eventually.
