# Canvas → Discord Assignment Reminder Bot

A single-process personal bot that reads your Canvas LMS account and posts
escalating deadline reminders to one Discord channel.

**Zero manual assignment entry.** You never type a course ID, assignment name
or due date. The bot discovers your active courses, pulls their assignments,
notices when an instructor moves a deadline, checks whether you have already
submitted, and reminds you only about work that is actually outstanding.

```
🚨 Assignment due in 28 minutes

Course: CS 240          Assignment: Project 3
Due: Friday, September 4 at 11:59 PM
Status: Not submitted
[Open in Canvas]
```

---

## Contents

1. [How it works](#how-it-works)
2. [Create a Canvas access token](#1-create-a-canvas-access-token)
3. [Create a Discord application and bot](#2-create-a-discord-application-and-bot)
4. [Invite the bot to your server](#3-invite-the-bot-to-your-server)
5. [Get the channel ID](#4-enable-developer-mode-and-get-the-channel-id)
6. [Configure `.env`](#5-configure-env)
7. [Run locally](#6-run-locally)
8. [Run the tests](#7-run-the-tests)
9. [Run with Docker](#8-run-with-docker)
10. [**Deploy to Render**](#9-deploy-to-render)
11. [Persistence and why PostgreSQL](#10-persistence-and-why-postgresql)
12. [Why the bot goes quiet](#11-why-the-bot-goes-quiet)
13. [Slash commands](#slash-commands)
14. [Health endpoint](#health-endpoint)
15. [Reminder algorithm](#reminder-algorithm)
16. [Configuration reference](#configuration-reference)
17. [Canvas API notes and limitations](#canvas-api-notes-and-limitations)
18. [Security notes](#security-notes)
19. [Troubleshooting](#troubleshooting)

---

## How it works

Three things run concurrently in **one process**, on one event loop:

| Component | Default interval | Job |
| --- | --- | --- |
| **Canvas sync** | every 5 minutes | Discover active courses, fetch assignments + your submission state, write to PostgreSQL **only when something changed** |
| **Reminder evaluation** | every 45 seconds | Decide what crossed a threshold from an in-memory cache, post to Discord |
| **HTTP health server** | on request | Answer `GET /health` from memory so a host can probe the service and an uptime monitor can keep it awake |

Canvas is never hit on the reminder path, so evaluation stays cheap and the
bot keeps working through a Canvas outage using cached state. The two loops run
on their own schedule and are entirely independent of HTTP traffic — no request
is needed to make a reminder fire, and no request can delay one.

**An idle bot issues no database statements at all.** Both loops read an
in-memory mirror of the persisted state, and PostgreSQL is written only when an
assignment genuinely changes or a reminder is claimed. That lets a database
with scale-to-zero (Neon and friends) actually stay asleep — see
[why the bot goes quiet](#11-why-the-bot-goes-quiet).

```
                       ┌──────────────── one process ────────────────┐
Canvas REST API ──sync (5 min)──> PostgreSQL ──evaluate (45 s)──> Discord channel
                       │              ^                              │
uptime monitor ──GET /health──────────┘  (reminder history,          │
                       │                  survives every redeploy)   │
                       └─────────────────────────────────────────────┘
```

### Project layout

```
app/
  main.py             entrypoint, startup sequence, signal handling
  config.py           environment parsing/validation, reminder thresholds
  canvas_client.py    async Canvas REST client: pagination, retries, backoff
  models.py           dataclasses, Canvas payload normalisation, UTC handling
  database.py         PostgreSQL schema, queries, reminder claim/release, retry
  cache.py            in-memory mirror the loops read, rebuilt at startup
  reminder_service.py Canvas sync + threshold-crossing reminder logic
  discord_bot.py      gateway client, slash commands, embeds, the two loops
  health.py           the aiohttp /health endpoint
  formatting.py       pure timezone/human-readable rendering
tests/                206 tests, no Canvas/Discord network access required
```

---

## 1. Create a Canvas access token

1. Log in to Canvas.
2. **Account → Settings**.
3. Scroll to **Approved Integrations** → **+ New Access Token**.
4. Purpose: `Discord reminder bot`. Leave the expiry blank for no expiry, or
   set a date and plan to rotate it.
5. Click **Generate Token** and copy it immediately — Canvas shows it once.

The token carries your full user permissions, so treat it like a password.
This bot only ever issues `GET` requests.

Your `CANVAS_BASE_URL` is the root of your Canvas site, e.g.
`https://canvas.university.edu` or `https://university.instructure.com`.
Do **not** include `/api/v1` (it is stripped automatically if you do).

Verify your token quickly:

```bash
curl -H "Authorization: Bearer $CANVAS_API_TOKEN" \
     "$CANVAS_BASE_URL/api/v1/users/self/profile"
```

## 2. Create a Discord application and bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Name it (e.g. `Canvas Reminders`) → **Create**.
3. Open the **Bot** tab → **Reset Token** → **Copy**. This is `DISCORD_BOT_TOKEN`.
4. **Leave every Privileged Gateway Intent OFF.** This bot requests only the
   non-privileged `guilds` intent; it never reads message content.
5. Optional: turn **Public Bot** off so nobody else can invite it.

## 3. Invite the bot to your server

**OAuth2 → URL Generator**:

- **Scopes:** `bot`, `applications.commands`
- **Bot Permissions:** `View Channels`, `Send Messages`, `Embed Links`

That is the complete least-privilege set. It needs no message history, no
mention-everyone, no manage-messages, and no administrator.

Copy the generated URL, open it, pick your server, authorise.

Then make sure the bot can actually see the target channel: in
**Channel Settings → Permissions**, confirm the bot's role has *View Channel*,
*Send Messages* and *Embed Links*.

## 4. Enable Developer Mode and get the channel ID

1. Discord → **User Settings → Advanced → Developer Mode: ON**.
2. Right-click the channel you want reminders in → **Copy Channel ID**.

That numeric ID is `DISCORD_CHANNEL_ID`.

`DISCORD_GUILD_ID` is optional. If you leave it blank the bot derives the
server from the channel at startup and registers slash commands there, which
makes them appear immediately instead of taking up to an hour to propagate
globally.

## 5. Configure `.env`

```bash
cp .env.example .env
```

Fill in at minimum:

```ini
CANVAS_BASE_URL=https://university.instructure.com
CANVAS_API_TOKEN=your_canvas_token
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_CHANNEL_ID=123456789012345678
DATABASE_URL=postgresql://phat:phat@localhost:5432/phat
TIMEZONE=America/Chicago
```

Everything else has a working default — see
[Configuration reference](#configuration-reference).

`.env` is gitignored. Never commit it.

## 6. Run locally

Requires **Python 3.12+** and a **PostgreSQL 13+** you can write to. There is no
SQLite fallback — see [why](#10-persistence-and-why-postgresql).

### Get a local PostgreSQL

The repo ships one, so this is a single command:

```bash
docker compose up -d db     # postgres:16 on localhost:5432, user/pass/db = phat
```

Prefer a native install? Any of these work; just point `DATABASE_URL` at it.

```bash
# macOS
brew install postgresql@16 && brew services start postgresql@16
createdb phat && psql phat -c "CREATE USER phat WITH PASSWORD 'phat'; \
  GRANT ALL ON DATABASE phat TO phat; GRANT ALL ON SCHEMA public TO phat;"

# Debian/Ubuntu
sudo apt install postgresql && sudo -u postgres createuser -P phat && \
  sudo -u postgres createdb -O phat phat
```

A free hosted database (including the Render one you create below) works too —
paste its external URL into `DATABASE_URL`. Note that sharing one database
between your laptop and Render means they share reminder history, which is
usually **not** what you want; use a separate local database.

### Start the bot

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m app.main
```

There is no migration step. The schema is created on first connect, and doing
so is idempotent and safe when two instances start at once.

Healthy startup looks like:

```
INFO  app: Starting Canvas reminder bot — canvas=https://... tz=America/Chicago db=postgresql://phat:***@localhost:5432/phat health=0.0.0.0:8080
INFO  app.database: PostgreSQL ready at postgresql://phat:***@localhost:5432/phat (schema v1)
INFO  app.health: Health endpoint listening on http://0.0.0.0:8080/health
INFO  app.discord_bot: Canvas authenticated as Your Name (id=12345)
INFO  app.reminder_service: Canvas sync complete — 5 course(s), 23 assignment(s) tracked
INFO  app.discord_bot: Connected to Discord as Canvas Reminders#1234
INFO  app.discord_bot: Registered 5 slash command(s) in guild 987654321
```

Check the endpoint:

```bash
curl -s localhost:8080/health | python -m json.tool
```

Stop with `Ctrl-C`; the bot closes the gateway, the HTTP listener, the Canvas
client and the database pool cleanly.

`PyNaCl is not installed, voice will NOT be supported` is a harmless
discord.py notice — this bot has nothing to do with voice.

## 7. Run the tests

```bash
pip install -r requirements-dev.txt
pytest                     # 206 tests
pytest -v                  # see every case by name
ruff check .               # lint
mypy                       # type check (config in pyproject.toml)
```

The persistence tests run against a **real PostgreSQL**, never a mock. The dev
requirements include `pgserver`, which ships a PostgreSQL binary as a wheel, so
the suite starts a throwaway server for you and needs no system install. Each
test gets its own schema and drops it afterwards.

To test against a specific server instead — CI with a Postgres service, or a
version you want to pin — set `TEST_DATABASE_URL`:

```bash
TEST_DATABASE_URL=postgresql://phat:phat@localhost:5432/phat pytest
```

If neither is available the persistence tests skip loudly rather than pretending
to have passed.

The suite covers threshold crossing, duplicate suppression, restart and downtime
behaviour, changed due dates, submitted assignments, timezone/DST conversion,
sorting, Canvas pagination/retry/auth handling, PostgreSQL persistence and
reconnection, exactly-once delivery across a simulated redeploy, `/health`
status codes and payload, `$PORT` handling, and clean startup/shutdown of the
real entrypoint under SIGTERM.

## 8. Run with Docker

Compose brings up PostgreSQL and the bot together:

```bash
cp .env.example .env        # fill in your Canvas/Discord values
docker compose up -d --build
docker compose logs -f
curl -s localhost:8080/health
```

Compose overrides `DATABASE_URL` to reach the `db` service on the compose
network, so the value in your `.env` is only used when you run the bot directly
on the host.

Or plain Docker against a database you already have:

```bash
docker build -t canvas-reminder-bot .
docker run -d --name canvas-bot --restart unless-stopped \
  --env-file .env \
  -e DATABASE_URL="postgresql://user:password@your-db-host:5432/phat" \
  -p 8080:8080 \
  canvas-reminder-bot
```

The image runs as a non-root user, writes nothing to its own filesystem, and
exposes one port for the health endpoint.

## 9. Deploy to Render

The bot is deployed as a **Web Service**, not a Background Worker, because a
free Web Service is the tier that gets a public URL — and that URL is what an
external uptime monitor pings to keep the instance awake.

> Render spins a free Web Service down after **15 minutes** with no inbound
> HTTP traffic, and a sleeping bot sends no reminders. Step 5 is not optional.

### Summary

| Setting | Value |
| --- | --- |
| Service type | **Web Service** (free plan) |
| Language / runtime | **Python 3** |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python -m app.main` |
| Health Check Path | `/health` |
| Database | A **Render PostgreSQL** instance (free plan) |
| Migration step | **None** — the schema is created on first connect |
| Keep-alive | An **external** uptime monitor calling `/health` every 10 minutes |

### 1. Create the PostgreSQL database first

Render dashboard → **New → Postgres**.

- Name: `phat-discord-bot-db`
- Database / User: anything (`phat` is fine)
- Region: **the same region you will use for the web service**
- Plan: **Free**

Wait for it to reach *Available*, then copy the **Internal Database URL** from
its Info tab. Internal, not external: it is faster, does not leave Render's
network, and does not count against bandwidth.

> Render's free PostgreSQL plan **expires after 30 days**. Put a reminder in
> your calendar to create a fresh instance and re-point `DATABASE_URL`, or move
> to a paid plan. When you migrate, see
> [moving the database](#moving-to-a-new-database) below.

### 2. Create the Web Service

Render dashboard → **New → Web Service** → connect this repository.

| Field | Value |
| --- | --- |
| **Language** | `Python 3` |
| **Branch** | `main` (or whichever you deploy) |
| **Region** | same as the database |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python -m app.main` |
| **Instance Type** | `Free` |

### 3. Set the environment variables

**Environment → Environment Variables.** Required:

| Name | Notes |
| --- | --- |
| `CANVAS_BASE_URL` | e.g. `https://university.instructure.com` |
| `CANVAS_API_TOKEN` | **secret** |
| `DISCORD_BOT_TOKEN` | **secret** |
| `DISCORD_CHANNEL_ID` | numeric channel ID |
| `DATABASE_URL` | the Internal Database URL from step 1 — **secret** |

Optional but recommended:

| Name | Notes |
| --- | --- |
| `DISCORD_GUILD_ID` | instant slash-command registration |
| `TIMEZONE` | e.g. `America/Chicago` |
| `PYTHON_VERSION` | e.g. `3.12.7`, to pin the runtime |
| `LOG_LEVEL` | `INFO` |

Every remaining variable in the
[configuration reference](#configuration-reference) is optional and may be set
here too.

**Do not set `PORT`.** Render injects it and the app binds `0.0.0.0:$PORT`
automatically. Setting it yourself will make the service unreachable.

Prefer to link the database rather than paste it? Add `DATABASE_URL` with
**Add from database** and pick `phat-discord-bot-db` → *Internal Connection
String*; Render then keeps it in sync for you.

### 4. Set the health check path

**Settings → Health Check Path** → `/health`.

Render then probes that path on every deploy and will not swap traffic to a
container that does not answer.

### 5. Point an external uptime monitor at it (required)

Nothing inside the app pings itself; a sleeping process cannot wake itself, and
self-pinging would be pointless load. Use any external monitor:

- **UptimeRobot** (free): New Monitor → HTTP(s) → URL
  `https://<your-service>.onrender.com/health` → interval **5 or 10 minutes**.
- **Better Stack**, **Cronitor**, **Healthchecks.io**, or a GitHub Actions
  scheduled workflow running `curl` all work equally well.

Anything under Render's 15-minute idle window keeps the instance awake. Ten
minutes gives you headroom.

`/health` answers `200` whenever the process is alive, including while Canvas
or Discord are having a bad day, so a transient outage will not page you or
cause Render to recycle a container that is still working. See
[health endpoint](#health-endpoint).

### 6. Deploy and verify

```bash
curl -s https://<your-service>.onrender.com/health | python -m json.tool
```

You should see `"status": "ok"`, `"discord_connected": true`,
`"database": "ok"` and a recent `last_canvas_sync`. Render's log stream should
show the same startup sequence as the local run.

### Deploying with the blueprint instead

`render.yaml` in this repo declares the same service and database. Render
dashboard → **New → Blueprint** → pick the repo. It prompts for the secret
values (they are marked `sync: false`, so they are never stored in git) and
wires `DATABASE_URL` to the database automatically.

### What Render does *not* need

- **No persistent disk.** All state is in PostgreSQL; the container filesystem
  is disposable.
- **No migration or release command.** The schema is created on connect.
- **No Dockerfile.** The native Python runtime is used. (The Dockerfile is kept
  for local Compose and other hosts; if you would rather deploy it, choose
  runtime **Docker** and leave the build and start commands blank.)
- **No `PORT` variable**, and no `Procfile`.

---

## 10. Persistence and why PostgreSQL

Render free Web Services have an **ephemeral filesystem**: everything written
to disk is discarded on every deploy, restart and idle spin-down. A SQLite file
there would lose the reminder history constantly, and on the next tick the bot
would fire one catch-up reminder per still-pending assignment. So SQLite has
been replaced by PostgreSQL outright, configured through `DATABASE_URL`.

There is deliberately **no automatic fallback** to a local file. A missing or
malformed `DATABASE_URL` is a startup error with exit code `2`, because a bot
that silently degrades to throwaway storage is worse than one that refuses to
start.

### What is stored

| Table | Contents |
| --- | --- |
| `assignments` | One row per monitored assignment: deadline, submission state, active flag, timestamps |
| `reminders_sent` | One row per `(assignment_id, due_at, threshold_minutes)` — the exactly-once ledger |
| `app_state` | Schema version, last successful sync, last sync error |

Timestamps are stored as canonical ISO-8601 UTC strings rather than
`timestamptz`. Reminder identity includes the due date, and keeping it as the
exact string the app produces means the key can never drift on a round trip.
The format sorts lexicographically in deadline order, so ordering and range
queries still behave.

### Exactly-once across restarts and redeploys

Delivery is **claim-then-send**. Before the Discord call, the bot `INSERT`s the
`(assignment, due date, threshold)` row with `ON CONFLICT DO NOTHING RETURNING`.
Only the transaction that wins the insert may send; if the send fails the claim
is deleted so the next tick retries it, and once a message is delivered the row
is marked permanently.

That matters on Render specifically: during a deploy the old and new containers
run **at the same time** against the same database. Both evaluate the same tick,
both see the same crossed threshold, and exactly one of them wins the insert.
Without the claim, you would get two identical reminders on every deploy.

Every method is a single transaction, so a container killed mid-call leaves no
half-written state.

### Moving to a new database

Because the free database expires, expect to do this at least once. `pg_dump`
the old one and restore into the new one to carry the history over:

```bash
pg_dump "$OLD_EXTERNAL_URL" --no-owner --no-privileges -Fc -f bot.dump
pg_restore -d "$NEW_EXTERNAL_URL" --no-owner --no-privileges bot.dump
```

Then update `DATABASE_URL` on the web service.

Starting fresh instead? Set `SUPPRESS_REMINDERS_ON_FIRST_RUN=true` for the
first deploy against the empty database. The bot records a silent baseline
rather than burst-notifying every pending assignment, which is what you want
mid-semester. Remove it afterwards.

Rows for retired assignments and reminders older than 60 days are pruned at
startup, so the database stays small indefinitely and comfortably inside the
free plan's storage.

---

## 11. Why the bot goes quiet

Managed PostgreSQL plans increasingly bill by *compute time* and suspend the
database when it is idle — Neon's free plan sleeps after about five minutes of
inactivity. A bot that ran one query every 45 seconds would keep that compute
awake permanently and burn the whole monthly allowance doing nothing.

So the loops do not read the database on a timer. They read
[`app/cache.py`](app/cache.py), an in-memory mirror rebuilt from PostgreSQL at
startup, and the database is touched only when something real happens.

| Event | PostgreSQL statements |
| --- | --- |
| Reminder tick, nothing due | **0** |
| Canvas sync, nothing changed | **0** |
| `GET /health` | **0** |
| `/due`, `/today`, `/week`, `/status` | **0** |
| Canvas sync, one assignment changed | 1 upsert + 1 batched state write |
| A reminder threshold crossing | claim, then mark delivered |
| Startup | schema check, 4 hydration reads, prune |

Measured on the real application: **6m47s idle — spanning a full 5-minute
Canvas sync cycle — produced zero statements, and 40 consecutive `/health`
requests produced zero statements.**

### What is still authoritative

The cache never decides whether a reminder may be sent. That is always
`INSERT ... ON CONFLICT DO NOTHING RETURNING` against `reminders_sent`, exactly
as before. A stale cache can cost a wasted claim attempt that returns nothing;
it cannot produce a duplicate or lose a reminder. Two instances overlapping
during a deploy still race for that insert, and exactly one wins.

Everything the loops rely on is rebuilt from PostgreSQL at startup, so a
restart, redeploy or crash resumes with the database's view — never a guess.

### The sync timestamp

`last_successful_sync_at` is **not** written every five minutes. Writing it on a
timer is precisely the heartbeat that keeps a database awake, and nothing about
reminder correctness depends on it — it is reported by `/status` and `/health`
only. It lives in memory and is persisted alongside a real change, so after a
restart `/status` shows the last *meaningful* sync rather than "never".

### Pool settings for a sleeping database

```python
ConnectionPool(min_size=0, max_size=4, max_idle=30, check=check_connection)
```

- `min_size=0` — no connection is held open across a quiet period, so nothing
  on our side keeps the compute awake and there is no socket to be severed when
  it suspends.
- `max_idle=30` — after a burst of work the pool returns to zero within
  30 seconds.
- `check=check_connection` — a connection dropped while the database slept is
  detected and replaced before the query runs, rather than failing it.
- Every operation is retried up to three times with jittered backoff on
  `psycopg.OperationalError`, which is what a cold start looks like from the
  client. Programming errors and constraint violations are not retried.

### Caveats

- **A cold wake costs latency.** The first query after a suspend waits for the
  database to start, typically under a second but occasionally a few. Database
  calls are synchronous, so that briefly blocks the event loop. It is well
  inside Discord's ~41-second heartbeat, and it only happens when there is real
  work to do.
- **Out-of-band edits are invisible until restart.** If you change
  `assignments` or `reminders_sent` with `psql` while the bot is running, it
  will not notice — the cache is only rebuilt at startup and updated by the
  bot's own writes. Restart the service after any manual surgery.
- **`/health` reports the last observed database state**, not a live probe, and
  says so via `database_last_contact`. After a quiet hour it means "healthy the
  last time we had reason to talk to it". Probing on every request is exactly
  what this design removes.
- **Compute is billed while awake, in minimum slices.** Each wake restarts the
  provider's idle timer, so a handful of clustered reminders costs about the
  same as one. Check your provider's current allowance on their dashboard;
  plan limits change.

---

## Slash commands

| Command | Who | What |
| --- | --- | --- |
| `/due [count]` | anyone in the server | Next incomplete assignments by deadline (default 5, max 10) |
| `/today` | anyone | Incomplete assignments due today in your configured timezone |
| `/week` | anyone | Incomplete assignments due in the next 7 days |
| `/sync` | **bot owner only** | Force an immediate Canvas sync and report the result (ephemeral) |
| `/status` | anyone | Canvas connectivity, last successful sync, monitored assignment count, timezone, intervals, uptime, thresholds |

`/sync` is restricted to the Discord account that owns the bot application
(resolved from Discord's application info at runtime — no extra config), since
it triggers outbound API calls. The read-only commands are unrestricted because
a personal bot normally lives in a private server.

---

## Health endpoint

The HTTP server runs on the same event loop as the Discord client and the two
polling loops. It uses `aiohttp`, which discord.py already depends on, so it
adds no new package.

| Path | Purpose |
| --- | --- |
| `GET /health` | The canonical endpoint. Point Render's health check and your uptime monitor here. |
| `GET /healthz` | Alias. |
| `GET /` | Alias, because monitors and platform probes often hit the root. |

```json
{
  "status": "ok",
  "bot": "running",
  "discord_connected": true,
  "discord_ready": true,
  "discord_latency_ms": 41.7,
  "canvas_healthy": true,
  "uptime_seconds": 8412,
  "sync_loop_running": true,
  "reminder_loop_running": true,
  "timezone": "America/Chicago",
  "timestamp": "2025-09-04T17:05:11Z",
  "database": "ok",
  "monitored_assignments": 23,
  "reminders_delivered": 41,
  "last_canvas_sync": "2025-09-04T17:02:44Z",
  "last_complete_canvas_sync": "2025-09-04T17:02:44Z"
}
```

The payload carries no tokens, no channel or guild IDs, no database URL, and no
course or assignment names. Anyone with the URL can read it, so it says only
whether things are working — never what you are working on.

### Status codes

| Code | `status` | Meaning |
| --- | --- | --- |
| `200` | `ok` | Everything is connected and syncing. |
| `200` | `degraded` | The process and its loops are alive, but Discord is reconnecting, Canvas is unreachable, or the database did not answer. |
| `503` | `stopped` | The bot has shut down. |

`degraded` deliberately still returns `200`. The loops retry on their own, and
a Canvas outage is not a reason for Render to recycle a container or for an
uptime monitor to page you. Read the body when you want the detail. Only a bot
that has actually stopped fails the check.

The endpoint is also defensive about itself: the database is queried on a worker
thread and the whole snapshot is capped at five seconds, so a wedged connection
cannot stall the Discord heartbeat or hang the probe.

### Failure behaviour

If the port cannot be bound at startup — already in use, or not permitted — the
process logs the reason and **exits non-zero** instead of starting the bot. On a
platform that routes by port, a bot running behind an endpoint nobody can reach
looks healthy while being useless, so a failed deploy is the honest outcome.

On `SIGTERM` the listener stops first and in-flight requests drain, then the
gateway closes, then the Canvas client and database pool. If the gateway does
not close within ten seconds it is cancelled, so the process always exits well
inside Render's kill window.

---

## Reminder algorithm

Defaults, from `app/config.py` — change them there or via
`REMINDER_THRESHOLDS_MINUTES`:

```
12h · 5h · 2h · 1h · 30m · 15m · 10m · 5m · 1m
```

### Threshold crossing, not equality

A threshold `T` is **crossed** once the time remaining falls to or below `T`.
There is no `remaining == 30 minutes` check anywhere, so a sleeping, delayed
or restarting process cannot miss a reminder.

> **The brief's edge case.** An assignment is due at 11:59 PM. The bot checks
> at 10:57 PM (62 minutes left — the 1-hour mark is not yet crossed) and again
> at 11:02 PM (57 minutes left — crossed). The 1-hour reminder goes out at
> 11:02 PM, and every later tick is silent until the 30-minute mark.

### Exactly-once delivery

Each `(assignment ID, due-date version, threshold)` triple is delivered at
most once, recorded in the `reminders_sent` table. The row is claimed *before*
the message is sent and released if the send fails, so restarting, redeploying,
crash-looping — or two instances overlapping during a deploy — resends nothing.
See [exactly-once across restarts and redeploys](#exactly-once-across-restarts-and-redeploys).

### Catch-up without a backlog

If several thresholds were crossed while the bot was down, it delivers only
the **smallest crossed threshold** — the most recently crossed, and therefore
most urgent — and records the rest as skipped so they can never fire late.
Back online with 20 minutes remaining, you get one "due in 20 minutes"
message, not four stale ones.

The message headline always states the *actual* time remaining rather than the
threshold's name, so a 30-minute reminder that fires late reads "due in 20
minutes" instead of lying to you.

### Due-date changes

Reminder identity includes the due date. When an instructor moves an
assignment from Monday 11:59 PM to Wednesday 11:59 PM, the Wednesday deadline
is a brand-new schedule: already-sent thresholds are not carried over, and the
12-hour reminder fires again relative to the new deadline. Because the new
deadline is far away, nothing fires immediately.

### When reminders stop

- The assignment is submitted, graded, pending review or excused.
- The assignment disappears from Canvas, is deleted, or is unpublished.
- Its due date is removed, or moves outside the monitoring window.
- The course is no longer an active enrollment.
- The deadline has passed.

### Failure behaviour

- A Discord send failure leaves the threshold **unrecorded**, so the next tick
  retries it.
- A Canvas failure aborts the sync and leaves cached state **completely
  untouched**. A temporary outage can never be read as "everything was deleted
  or completed".
- If one course fails to load but others succeed, only the successful courses'
  assignments are reconciled; the failed course keeps its cached rows.
- If Canvas returns an assignment with no submission object, the previously
  known submission state is preserved rather than downgraded to "not
  submitted".

---

## Configuration reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `CANVAS_BASE_URL` | *required* | Canvas root URL, no `/api/v1` |
| `CANVAS_API_TOKEN` | *required* | Canvas personal access token |
| `DISCORD_BOT_TOKEN` | *required* | Discord bot token |
| `DISCORD_CHANNEL_ID` | *required* | Channel that receives reminders |
| `DISCORD_GUILD_ID` | derived from the channel | Guild for instant slash-command registration |
| `DATABASE_URL` | *required* | PostgreSQL connection URL. `postgres://` and `postgresql+psycopg://` are accepted and normalised |
| `TIMEZONE` | `America/Chicago` | IANA zone, **display only** |
| `PORT` | `8080` | Port for the health server. **Render sets this — do not set it yourself** |
| `HTTP_HOST` | `0.0.0.0` | Interface for the health server. Must stay `0.0.0.0` on Render |
| `CANVAS_SYNC_INTERVAL_SECONDS` | `300` | Canvas poll interval (min 30) |
| `REMINDER_INTERVAL_SECONDS` | `45` | Local evaluation interval (min 5) |
| `REMINDER_THRESHOLDS_MINUTES` | `720,300,120,60,30,15,10,5,1` | Reminder thresholds |
| `MONITOR_LOOKAHEAD_DAYS` | `30` | Only track assignments due within this window |
| `MONITOR_PAST_GRACE_HOURS` | `24` | Keep recently-past assignments cached (no reminders) |
| `CANVAS_STUDENT_ENROLLMENTS_ONLY` | `true` | Only courses where you are enrolled as a student |
| `SUPPRESS_REMINDERS_ON_FIRST_RUN` | `false` | Silent baseline on a brand-new database |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Invalid values are rejected at startup with a clear message and exit code `2`,
before any network connection is made. That includes a missing `DATABASE_URL`,
a non-PostgreSQL URL (a SQLite path is rejected with an explanation), and a
`PORT` outside 1–65535.

### Time handling

Canvas timestamps are parsed into timezone-aware UTC and stored as canonical
ISO-8601 UTC strings. Every deadline comparison happens in UTC. `TIMEZONE` is
applied only when rendering text for Discord, using `zoneinfo`, so daylight
saving transitions are handled correctly. Naive datetimes raise rather than
silently drifting.

---

## Canvas API notes and limitations

Discovered while building against the Canvas REST API:

- **Endpoints used.** `GET /users/self/profile` (auth check),
  `GET /courses?enrollment_state=active&state[]=available` (discovery), and
  `GET /courses/:id/assignments?include[]=submission&include[]=all_dates`.
  All read-only.
- **`include[]=submission` is the efficient path to submission state.** It
  returns *your* submission inline with each assignment — one request per
  course instead of one per assignment.
- **Pagination is by `Link` header, not a page count.** Canvas returns
  `rel="next"` links; the client requests `per_page=100` and follows `next`
  until it is absent, with a 100-page safety valve.
- **HTTP 403 is overloaded.** Canvas returns 403 both for genuine
  authorisation failures and for rate limiting (body contains "Rate Limit
  Exceeded"). The client inspects the body: rate limits back off and retry,
  auth failures fail fast without pointless retries.
- **Assignments with no online submission type** (`on_paper`, `none`) report
  `workflow_state: "unsubmitted"` forever unless a teacher enters a grade. You
  will keep getting reminders for these until they are graded or the deadline
  passes — which is usually what you want for a paper deadline, but it does
  mean you cannot "check them off" from Canvas.
- **Section and student overrides.** The bot uses the top-level `due_at`,
  which Canvas already personalises for the calling user on the assignments
  endpoint. `include[]=all_dates` is requested for completeness, but per-section
  override resolution beyond Canvas's own default is not implemented.
- **`enrollment_state=active` still lists courses whose term has ended** at
  some institutions until enrollment is concluded. The
  `MONITOR_LOOKAHEAD_DAYS` window keeps stale courses from generating noise.
- **Teacher/TA enrollments are excluded by default.** With
  `CANVAS_STUDENT_ENROLLMENTS_ONLY=true`, only student enrollments are
  considered, since "your submission" is meaningless in a course you teach. If
  you see no courses at all, try setting it to `false`.
- **Undated assignments are invisible to this bot by design.** No due date
  means no threshold to cross.
- **Quizzes and discussions** appear on the assignments endpoint when they are
  graded and have a due date; ungraded ones do not.
- **Token scope.** A Canvas personal access token inherits all of your
  permissions — Canvas has no way to mint a read-only token from the UI.

---

## Security notes

- Tokens and the database URL are read from environment variables only, never
  hardcoded. `render.yaml` marks every secret `sync: false`, so no credential
  is ever committed.
- `.env` is gitignored (and dockerignored); only `.env.example` is committed.
- Nothing logs a token: the Canvas client strips query strings before logging
  URLs, credentials travel in the `Authorization` header, and the `httpx`
  logger is pinned to `WARNING`.
- Startup config errors print to stderr *without* echoing values. The database
  URL is password-redacted everywhere it is logged.
- Least-privilege Discord: three permissions, one non-privileged intent, zero
  privileged intents.
- The only public surface is `GET /health`, which is read-only, takes no
  parameters, and returns nothing sensitive. There are no other routes, no
  writes, and no way to trigger a Canvas or Discord action over HTTP.
- No browser automation and no HTML scraping — the REST API only.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Configuration error: Missing required environment variable` | Fill in `.env`; the name of the missing key is printed |
| `Discord rejected DISCORD_BOT_TOKEN` | Token is wrong or was reset — copy it again from the Bot tab |
| `Canvas rejected the API token (HTTP 401)` | Token wrong, expired or revoked — generate a new one |
| Slash commands do not appear | Re-invite with the `applications.commands` scope; set `DISCORD_GUILD_ID` for instant registration |
| `Cannot access DISCORD_CHANNEL_ID` | Wrong ID, or the bot's role lacks *View Channel* on that channel |
| `Canvas reported no active courses` | Between terms, or you hold non-student enrollments — try `CANVAS_STUDENT_ENROLLMENTS_ONLY=false` |
| Reminders repeat after a redeploy | `DATABASE_URL` points somewhere disposable, or a different database than last deploy — see [persistence](#10-persistence-and-why-postgresql) |
| A submitted assignment still reminds | It has no online submission type; Canvas reports it unsubmitted until graded |
| `Configuration error: DATABASE_URL must be a PostgreSQL URL` | You passed a SQLite path or another scheme. There is no SQLite mode — see [persistence](#10-persistence-and-why-postgresql) |
| `Could not connect to DATABASE_URL` | Wrong URL, or the database is asleep/expired. On Render check the Postgres instance is *Available* and that you used the **Internal** URL from the **same region** |
| Render deploy fails its health check | The service must be a **Web Service** with Health Check Path `/health`. If you set `PORT` yourself, remove it — Render injects it |
| Render says "no open ports detected" | Same cause: `HTTP_HOST` must be `0.0.0.0` and `PORT` must come from Render |
| Bot goes quiet after ~15 minutes | The free instance spun down. Add an external uptime monitor on `/health` — [step 5](#5-point-an-external-uptime-monitor-at-it-required) |
| Everything works, then stops ~30 days in | Render's free PostgreSQL expired. Create a new one and re-point `DATABASE_URL` — see [moving the database](#moving-to-a-new-database) |
| Two identical reminders on the same deploy | Should not happen (claim-then-send). If it does, confirm both instances share one `DATABASE_URL` |
| `/health` returns `"status": "degraded"` | Read the body: `database`, `cache_hydrated`, `discord_ready` and `canvas_healthy` say which part is unhappy. The loops keep retrying on their own |
| `/status` shows an old "last successful sync" | Expected after a restart with no Canvas changes since: the timestamp is persisted only alongside a real change, never as a heartbeat — see [why the bot goes quiet](#11-why-the-bot-goes-quiet) |
| A row edited by hand in `psql` has no effect | The loops read an in-memory cache. Restart the service after any manual database change |
| Neon/similar compute never scales to zero | Something else is polling it. The bot itself issues no statements when idle — verify with `log_statement = 'all'` |
