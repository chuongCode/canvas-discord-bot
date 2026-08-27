"""Discord bot: reminder delivery, slash commands and the two polling loops."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from .canvas_client import CanvasClient
from .config import Config
from .database import Database
from .formatting import (
    build_reminder_text,
    format_due,
    format_due_short,
    humanize_minutes,
    local,
    submission_status_text,
)
from .models import Assignment, to_iso, utcnow
from .reminder_service import ReminderService

log = logging.getLogger(__name__)

MAX_LIST_ITEMS = 10


def owner_only():
    """Restrict a command to the bot application's owner (this is a personal bot)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        client = interaction.client
        if isinstance(client, CanvasReminderBot) and await client.is_owner(interaction.user):
            return True
        raise app_commands.CheckFailure("This command is restricted to the bot owner.")

    return app_commands.check(predicate)


class CanvasReminderBot(discord.Client):
    """A plain gateway client plus a slash-command tree.

    Deliberately not ``commands.Bot``: there are no prefix commands here, and
    that class warns about the privileged message-content intent we neither
    need nor want.
    """

    def __init__(self, config: Config, db: Database, canvas: CanvasClient) -> None:
        # Least privilege: no message content, no members, no presence intents.
        super().__init__(intents=discord.Intents(guilds=True))
        self.tree = app_commands.CommandTree(self)
        self.config = config
        self.db = db
        self.canvas = canvas
        self._owner_ids: set[int] | None = None
        self.service = ReminderService(config, db, canvas, self._deliver_reminder)
        self._commands_synced = False
        self._started_at = utcnow()

        self.sync_loop.change_interval(seconds=config.canvas_sync_interval_seconds)
        self.reminder_loop.change_interval(seconds=config.reminder_interval_seconds)

    # ------------------------------------------------------------ lifecycle

    async def setup_hook(self) -> None:
        self._register_commands()

        # Rebuild the in-memory view before anything can evaluate against it.
        # A bot that starts with an empty cache would see every threshold as
        # un-sent, so this failing is fatal rather than something to log past.
        await asyncio.to_thread(self.service.hydrate)

        # Verify Canvas and prime the cache before the first reminder tick.
        # A Canvas outage here is survivable: the loops keep retrying.
        try:
            profile = await self.canvas.verify_token()
            log.info(
                "Canvas authenticated as %s (id=%s)",
                profile.get("name", "unknown"), profile.get("id", "?"),
            )
            self.service.canvas_healthy = True
        except Exception as exc:
            log.error("Canvas verification failed at startup: %s", exc)
            self.service.canvas_healthy = False

        try:
            await self.service.sync_canvas()
        except Exception:
            log.exception("Initial Canvas sync failed; continuing with cached state")

        self.sync_loop.start()
        self.reminder_loop.start()

    async def on_ready(self) -> None:
        log.info("Connected to Discord as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        if not self._commands_synced:
            await self._sync_commands()
            self._commands_synced = True
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="Canvas deadlines"
                )
            )
        except discord.DiscordException:
            pass

    async def close(self) -> None:
        for loop in (self.sync_loop, self.reminder_loop):
            if loop.is_running():
                loop.cancel()
        await super().close()

    # ---------------------------------------------------------------- health

    async def health_snapshot(self) -> dict[str, Any]:
        """Non-sensitive status for GET /health.

        **Answers entirely from memory — no PostgreSQL query.** Probing the
        database on every request would defeat scale-to-zero: an uptime monitor
        calling this every few minutes would keep the compute awake around the
        clock, which is the opposite of what a keep-alive endpoint should cost.

        ``database`` therefore reports the outcome of the last real operation
        rather than a live probe, with ``database_last_contact`` saying when
        that was. After a long quiet spell it means "healthy when we last had
        reason to talk to it", which is the honest answer.

        Deliberately free of tokens, channel/guild IDs, database URLs, course
        names and assignment titles: this endpoint is reachable by anyone with
        the URL.
        """
        now = utcnow()
        cache = self.service.cache
        latency = self.latency
        if self.db.healthy is None:
            database = "unknown"
        else:
            database = "ok" if self.db.healthy else "error"

        payload: dict[str, Any] = {
            "status": "ok",
            "bot": "running",
            "discord_connected": bool(not self.is_closed() and self.ws is not None),
            "discord_ready": self.is_ready(),
            "discord_latency_ms": round(latency * 1000, 1) if math.isfinite(latency) else None,
            "canvas_healthy": self.service.canvas_healthy,
            "uptime_seconds": int((now - self._started_at).total_seconds()),
            "sync_loop_running": self.sync_loop.is_running(),
            "reminder_loop_running": self.reminder_loop.is_running(),
            "timezone": self.config.timezone_name,
            "timestamp": to_iso(now),
            "database": database,
            "database_last_contact": to_iso(self.db.last_contact_at),
            "cache_hydrated": cache.hydrated,
            "monitored_assignments": cache.monitored_count(),
            "reminders_delivered": cache.delivered_count,
            "last_canvas_sync": to_iso(cache.last_sync_at),
            "last_complete_canvas_sync": to_iso(cache.last_complete_sync_at),
        }

        if self.is_closed():
            payload["status"] = "stopped"
            payload["bot"] = "stopped"
        elif (
            database == "error"
            or not cache.hydrated
            or not payload["discord_ready"]
            or self.service.canvas_healthy is False
        ):
            # Still serving 200: the process is alive and its loops keep
            # retrying. The body is where a monitor learns the detail.
            payload["status"] = "degraded"
        return payload

    async def _sync_commands(self) -> None:
        """Register slash commands, scoped to one guild for instant availability."""
        guild_id = self.config.discord_guild_id
        if guild_id is None:
            channel = await self._resolve_channel()
            guild = getattr(channel, "guild", None)
            guild_id = getattr(guild, "id", None)
        try:
            if guild_id is not None:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Registered %d slash command(s) in guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Registered %d global slash command(s) (may take ~1h)", len(synced))
        except discord.DiscordException as exc:
            log.error("Slash command registration failed: %s", exc)

    async def _resolve_channel(self) -> discord.abc.Messageable | None:
        channel = self.get_channel(self.config.discord_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.discord_channel_id)
            except discord.DiscordException as exc:
                log.error("Cannot access DISCORD_CHANNEL_ID %s: %s", self.config.discord_channel_id, exc)
                return None
        if not isinstance(channel, discord.abc.Messageable):
            log.error("DISCORD_CHANNEL_ID %s is not a messageable channel", self.config.discord_channel_id)
            return None
        return channel

    async def is_owner(self, user: discord.abc.User) -> bool:
        """True for the application owner (or any member of its team)."""
        if self._owner_ids is None:
            try:
                info = self.application or await self.application_info()
            except discord.DiscordException as exc:
                log.warning("Could not resolve the application owner: %s", exc)
                return False
            owners: set[int] = set()
            if info.team is not None:
                owners = {member.id for member in info.team.members}
            elif info.owner is not None:
                owners = {info.owner.id}
            self._owner_ids = owners
        return user.id in self._owner_ids

    # --------------------------------------------------------------- loops

    @tasks.loop(seconds=300)
    async def sync_loop(self) -> None:
        try:
            await self.service.sync_canvas()
        except Exception:
            log.exception("Unexpected error during Canvas sync loop")

    @sync_loop.before_loop
    async def _before_sync(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=45)
    async def reminder_loop(self) -> None:
        try:
            await self.service.evaluate_reminders()
        except Exception:
            log.exception("Unexpected error during reminder evaluation")

    @reminder_loop.before_loop
    async def _before_reminders(self) -> None:
        await self.wait_until_ready()

    # ---------------------------------------------------------- delivery

    async def _deliver_reminder(
        self, assignment: Assignment, threshold_minutes: int, minutes_remaining: float
    ) -> bool:
        """Send one reminder. Returns False on failure so it is retried later."""
        channel = await self._resolve_channel()
        if channel is None:
            return False

        text = build_reminder_text(assignment, threshold_minutes, minutes_remaining)
        embed = discord.Embed(
            title=text.title,
            colour=discord.Colour(text.colour),
            url=assignment.html_url,
            description=f"[Open in Canvas]({assignment.html_url})",
        )
        embed.add_field(name="Course", value=assignment.course_name[:1024], inline=True)
        embed.add_field(name="Assignment", value=assignment.name[:1024], inline=True)
        if assignment.due_at is not None:
            embed.add_field(
                name="Due", value=format_due(assignment.due_at, self.config.tz), inline=False
            )
        embed.add_field(name="Status", value=submission_status_text(assignment), inline=True)
        if assignment.points_possible:
            embed.add_field(name="Points", value=f"{assignment.points_possible:g}", inline=True)
        embed.set_footer(
            text=f"{self.config.timezone_name} · reminder at {humanize_minutes(threshold_minutes)}"
        )

        # Escalate presentation: a plain-text line only when it is nearly due.
        content = None
        if threshold_minutes <= 15:
            content = f"**Due in {humanize_minutes(minutes_remaining)}** — {assignment.name}"

        try:
            await channel.send(content=content, embed=embed)
        except discord.DiscordException as exc:
            log.warning("Discord send failed for '%s': %s", assignment.name, exc)
            return False
        log.info(
            "Sent %s-minute reminder for '%s' (%s)",
            threshold_minutes, assignment.name, assignment.course_name,
        )
        return True

    # ------------------------------------------------------------ commands

    def _list_embed(self, title: str, assignments: Sequence[Assignment], empty: str) -> discord.Embed:
        now = utcnow()
        embed = discord.Embed(title=title, colour=discord.Colour(0x3584E4))
        if not assignments:
            embed.description = empty
            return embed
        for assignment in assignments[:MAX_LIST_ITEMS]:
            remaining = assignment.minutes_remaining(now)
            when = (
                format_due_short(assignment.due_at, self.config.tz)
                if assignment.due_at
                else "No due date"
            )
            timing = (
                f"in {humanize_minutes(remaining)}"
                if remaining is not None and remaining > 0
                else "past due"
            )
            embed.add_field(
                name=assignment.name[:250],
                value=(
                    f"{assignment.course_name} — {when} · {timing}\n"
                    f"Status: {submission_status_text(assignment)}\n"
                    f"[Open in Canvas]({assignment.html_url})"
                )[:1024],
                inline=False,
            )
        if len(assignments) > MAX_LIST_ITEMS:
            embed.set_footer(text=f"+{len(assignments) - MAX_LIST_ITEMS} more")
        return embed

    def _register_commands(self) -> None:
        config = self.config

        @self.tree.command(name="due", description="Next incomplete assignments by deadline")
        @app_commands.describe(count="How many to show (1-10, default 5)")
        async def due(interaction: discord.Interaction, count: int = 5) -> None:
            count = max(1, min(MAX_LIST_ITEMS, count))
            items = self.service.incomplete_assignments()[:count]
            await interaction.response.send_message(
                embed=self._list_embed("📋 Upcoming assignments", items, "Nothing due. Enjoy it."),
                ephemeral=False,
            )

        @self.tree.command(name="today", description="Incomplete assignments due today")
        async def today(interaction: discord.Interaction) -> None:
            now = utcnow()
            local_now = local(now, config.tz)
            end_local = (local_now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            items = [
                a
                for a in self.service.incomplete_assignments(
                    until=end_local, include_past_due=True
                )
                if a.due_at is not None and a.due_at >= start_local
            ]
            await interaction.response.send_message(
                embed=self._list_embed(
                    f"📅 Due today ({local_now:%A, %b} {local_now.day})",
                    items,
                    "Nothing due today.",
                )
            )

        @self.tree.command(name="week", description="Incomplete assignments due in the next 7 days")
        async def week(interaction: discord.Interaction) -> None:
            now = utcnow()
            items = self.service.incomplete_assignments(now=now, until=now + timedelta(days=7))
            await interaction.response.send_message(
                embed=self._list_embed("🗓️ Next 7 days", items, "Nothing due this week.")
            )

        @self.tree.command(name="sync", description="Force an immediate Canvas synchronisation")
        @owner_only()
        async def sync(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await self.service.sync_canvas()
            colour = 0x2EC27E if result.ok else 0xE01B24
            embed = discord.Embed(
                title="✅ Canvas sync succeeded" if result.ok else "❌ Canvas sync failed",
                description=result.summary(),
                colour=discord.Colour(colour),
            )
            if result.courses_failed:
                embed.add_field(
                    name="Courses not read", value=", ".join(result.courses_failed)[:1024], inline=False
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="status", description="Bot health information")
        async def status(interaction: discord.Interaction) -> None:
            now = utcnow()
            last_sync = self.service.last_sync_at()
            healthy = self.service.canvas_healthy
            if healthy is None:
                canvas_state = "⚪ Unknown"
            else:
                canvas_state = "🟢 Connected" if healthy else "🔴 Unreachable"
            if last_sync is None:
                sync_text = "never"
            else:
                sync_text = (
                    f"{format_due(last_sync, config.tz)} "
                    f"({humanize_minutes((now - last_sync).total_seconds() / 60)} ago)"
                )

            embed = discord.Embed(title="🤖 Bot status", colour=discord.Colour(0x3584E4))
            embed.add_field(name="Canvas", value=canvas_state, inline=True)
            embed.add_field(
                name="Monitored assignments",
                value=str(self.service.cache.monitored_count()),
                inline=True,
            )
            embed.add_field(
                name="Incomplete & upcoming",
                value=str(len(self.service.incomplete_assignments())),
                inline=True,
            )
            embed.add_field(name="Last successful sync", value=sync_text, inline=False)
            embed.add_field(name="Timezone", value=config.timezone_name, inline=True)
            embed.add_field(
                name="Intervals",
                value=(
                    f"sync {config.canvas_sync_interval_seconds}s · "
                    f"eval {config.reminder_interval_seconds}s"
                ),
                inline=True,
            )
            embed.add_field(
                name="Uptime",
                value=humanize_minutes((now - self._started_at).total_seconds() / 60),
                inline=True,
            )
            embed.add_field(
                name="Reminders delivered",
                value=str(self.service.cache.delivered_count),
                inline=True,
            )
            error = self.service.last_error()
            if error:
                embed.add_field(name="Last sync issue", value=error[:1024], inline=False)
            embed.set_footer(text=f"Thresholds: {', '.join(str(t) for t in config.thresholds_minutes)} min")
            await interaction.response.send_message(embed=embed)

        @self.tree.error
        async def on_tree_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            if isinstance(error, app_commands.CheckFailure):
                message = "You are not allowed to run that command."
            else:
                log.exception("Slash command error", exc_info=error)
                message = "Something went wrong running that command. Check the logs."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.DiscordException:
                pass
