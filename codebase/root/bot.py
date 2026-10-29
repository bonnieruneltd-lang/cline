"""
bot.py
======
Entry point for the Royal Court bot.

Run with:  python bot.py

Hosting notes
-------------
* All secrets are read from environment variables (Wisp Byte "Startup" tab) or
  from a local ``.env`` file (loaded by python-dotenv). Nothing is hard-coded.
* The database path is resolved to an absolute path so the file always lands in
  a predictable, persistent location regardless of the panel's working dir.
* SIGINT/SIGTERM are handled so the panel can stop the process cleanly and the
  SQLite connection is closed (no WAL corruption on shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database
from shop.seed import seed_shop
from api import RedeemAPI
from daily_sync import DailySync

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Load .env if present. On Wisp Byte the variables are usually injected by the
# panel, so this is a convenience fallback for local development.
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "").strip()  # optional rotating log file

# Resolve the DB path to an absolute location so it is stable under any cwd.
_db_raw = os.getenv("DB_PATH", "royal_bank.db").strip() or "royal_bank.db"
DB_PATH = str(Path(_db_raw).expanduser().resolve())

GUILD_ID = os.getenv("GUILD_ID", "").strip()

COGS = [
    "cogs.nation",
    "cogs.kingdom",
    "cogs.bank",
    "cogs.transfers",
    "cogs.payout",
    "cogs.decree",
    "cogs.ledger",
    "cogs.shop",
    "cogs.kits",
    "cogs.contract",
    "cogs.union",
    "cogs.factions",
    "cogs.individual",
    "cogs.leaderboard",
    "cogs.games",
    "cogs.fishing",
]


def _configure_logging() -> logging.Logger:
    """Configure root logging once, with optional rotating file output."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if LOG_FILE:
        try:
            Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
                )
            )
        except OSError:
            # Never let logging setup crash the bot.
            pass
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("royalcourt")


logger = _configure_logging()


class RoyalCourt(commands.Bot):
    def __init__(self) -> None:
        # ``members`` and ``message_content`` are privileged intents and MUST be
        # enabled in the Discord Developer Portal:
        #   Bot -> Privileged Gateway Intents -> Server Members + Message Content.
        # Without message_content, Discord delivers empty ``message.content`` in
        # guild channels/threads, which breaks Gilbert's reply pipeline.
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!rc ", intents=intents, help_command=None)
        self.db = Database(DB_PATH)
        self.logger = logger
        self.api = RedeemAPI(self)
        self.daily = DailySync(self)

    # -- lifecycle ---------------------------------------------------------
    async def setup_hook(self) -> None:
        await self.db.connect()

        # Seed the Royal Shop catalog (idempotent; preserves admin edits).
        await seed_shop(self.db)

        # Start the HTTP redemption API used by the Forge mod. A bind failure
        # (blocked port) must never take the Discord bot down with it.
        try:
            await self.api.start()
        except Exception:
            logger.exception("Redemption API failed to start; continuing without it.")

        # Start the once-a-day Turso mirror + AI guide job. A failure here must
        # never stop the bot from logging in.
        try:
            self.daily.start()
        except Exception:
            logger.exception("Daily sync failed to start; continuing without it.")

        for ext in COGS:
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension %s", ext)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to load extension %s", ext)

        # Register the app-command error handler. discord.py looks for
        # ``tree.on_error`` (NOT a method named ``on_app_command_error``), so we
        # must assign it explicitly or errors are only logged and the user sees
        # "The application did not respond".
        self.tree.on_error = self._on_app_command_error

        # Re-register persistent views so buttons survive restarts.
        await self._register_persistent_views()

        # Sync slash commands.
        await self._sync_commands()

    async def _register_persistent_views(self) -> None:
        from cogs.bank import DrawApprovalView
        from cogs.union import UnionCommitApprovalView
        from creation import CreationApprovalView

        pending = await self.db.all_pending_draws()
        for draw in pending:
            self.add_view(DrawApprovalView(self, draw["id"]))
        if pending:
            logger.info("Re-registered %d pending draw view(s).", len(pending))

        pending_creations = await self.db.all_pending_creations()
        for creation in pending_creations:
            self.add_view(CreationApprovalView(self, creation["id"]))
        if pending_creations:
            logger.info("Re-registered %d pending creation view(s).", len(pending_creations))

        pending_commits = await self.db.all_pending_union_commits()
        for commit in pending_commits:
            self.add_view(UnionCommitApprovalView(self, commit["id"]))
        if pending_commits:
            logger.info("Re-registered %d pending Union commit view(s).", len(pending_commits))

        await self._refund_abandoned_blackjack_hands()
        await self._refund_abandoned_poker_hands()

    async def _refund_abandoned_blackjack_hands(self) -> None:
        """Return the stake on any hand that a restart cut short.

        Blackjack holds the stake while the player decides, and its buttons live
        in memory, so a restart mid-hand would leave the credits with the house
        and no result to pay. Refunding is the only safe resolution: it cannot
        create credits (the stake came from the player) and it never charges
        someone for a hand they did not get to finish.
        """
        import casino

        hands = await self.db.all_open_blackjack_hands()
        for hand in hands:
            try:
                async with self.db.transaction():
                    if not await self.db.close_blackjack_hand(int(hand["id"])):
                        continue
                    wallet = await self.db.find_individual(
                        int(hand["guild_id"]), int(hand["discord_id"])
                    )
                    if wallet is None:
                        continue
                    await casino.finish_wager(
                        self.db,
                        guild_id=int(hand["guild_id"]),
                        wallet=wallet,
                        stake=int(hand["stake"]),
                        gross=int(hand["stake"]),
                        outcome="push",
                        kind="individual_blackjack",
                        note="Blackjack interrupted by a restart: stake returned",
                    )
            except Exception:
                logger.exception("Could not refund blackjack hand %s", hand["id"])
            else:
                logger.info(
                    "Refunded abandoned blackjack hand %s (%s credits).",
                    hand["id"], hand["stake"],
                )

    async def _refund_abandoned_poker_hands(self) -> None:
        """Return the buy-in on a poker table cut short by a restart.

        The table itself is in memory, so there is no way to resume the hand.
        The row is closed before the refund, exactly like blackjack, so a crash
        during refund does not pay twice on the next start.
        """
        import casino

        hands = await self.db.all_open_poker_hands()
        for hand in hands:
            try:
                async with self.db.transaction():
                    if not await self.db.close_poker_hand(int(hand["id"])):
                        continue
                    wallet = await self.db.find_individual(
                        int(hand["guild_id"]), int(hand["discord_id"])
                    )
                    if wallet is None:
                        continue
                    await casino.finish_wager(
                        self.db,
                        guild_id=int(hand["guild_id"]),
                        wallet=wallet,
                        stake=int(hand["stake"]),
                        gross=int(hand["stake"]),
                        outcome="push",
                        kind="individual_poker",
                        note="Poker interrupted by a restart: buy-in returned",
                    )
            except Exception:
                logger.exception("Could not refund poker hand %s", hand["id"])
            else:
                logger.info(
                    "Refunded abandoned poker hand %s (%s credits).",
                    hand["id"], hand["stake"],
                )

    async def _sync_commands(self) -> None:
        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %d commands to guild %s", len(synced), GUILD_ID)
                return
            except Exception as exc:
                logger.error("Guild sync failed (%s); falling back to global sync.", exc)
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d commands globally (may take up to an hour to appear).", len(synced))
        except Exception:
            logger.exception("Global command sync failed.")

    async def on_ready(self) -> None:
        logger.info(
            "Logged in as %s (id=%s) | discord.py %s",
            self.user, getattr(self.user, "id", "?"), discord.__version__,
        )
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="over the Royal Bank ⚜"
                )
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed to set presence.")

    async def on_disconnect(self) -> None:
        logger.warning("Disconnected from Discord; the library will auto-reconnect.")

    async def on_resumed(self) -> None:
        logger.info("Session resumed with Discord.")

    async def close(self) -> None:
        """Close the DB connection and cancel background tasks cleanly."""
        logger.info("Shutting down…")
        try:
            await self.api.stop()
        except Exception:  # pragma: no cover
            logger.exception("Error while stopping the redemption API.")
        try:
            await self.daily.stop()
        except Exception:  # pragma: no cover
            logger.exception("Error while stopping the daily sync.")
        try:
            await self.db.close()
        except Exception:  # pragma: no cover
            logger.exception("Error while closing the database.")
        await super().close()

    # -- error handling ----------------------------------------------------
    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Unwrap the invocation error so we surface the real exception type.
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]

        from checks import NotAdmin
        import embeds as E

        if isinstance(error, NotAdmin):
            embed = E.error("Not Authorized", str(error))
        elif isinstance(error, app_commands.CheckFailure):
            embed = E.error("Not Authorized", "You cannot use this command.")
        else:
            logger.exception("Command error: %s", error)
            embed = E.error("Something went wrong", f"`{type(error).__name__}: {error}`")

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            # The interaction may have expired; nothing else we can do.
            pass


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "paste-your-bot-token-here":
        raise SystemExit(
            "No DISCORD_TOKEN found. Set it in the Wisp Byte 'Startup' tab "
            "(environment variables) or copy .env.example to .env and paste your token."
        )

    bot = RoyalCourt()

    # Graceful shutdown on SIGTERM (panels) and SIGINT (Ctrl-C).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
            pass

    try:
        async with bot:
            await bot.start(token)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token (LoginFailure). Double-check DISCORD_TOKEN.")
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "The bot requested privileged intents that are disabled. Enable "
            "'Server Members Intent' in the Discord Developer Portal, then restart."
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
