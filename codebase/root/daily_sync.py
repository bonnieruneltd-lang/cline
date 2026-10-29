"""
daily_sync.py
=============
Once-a-day maintenance for the Royal Court bot.

Two independent jobs run once per day (the schedule itself is an environment
variable). Nothing here is hard-bound: the Turso endpoint and token, the run
time, the AI endpoint/model and where the guide is delivered are all read from
the environment at start-up, so they can be changed from the hosting panel
without touching this file.

1. **Turso mirror** - copies *every row of every table*, including the
   append-only ``transactions`` audit log, from the local SQLite database into a
   remote Turso / libSQL database. The remote schema is created from the local
   ``sqlite_master`` DDL and each row is upserted with ``INSERT OR REPLACE``, so
   a run only ever brings the mirror up to date and never duplicates a row. The
   mirror is a backup/reader copy: it is never used as the bot's primary store.

2. **Bot guide** - collects the bot's own surface (every slash command with its
   arguments and description, each loaded module's purpose, the fixed economy
   constants and the verification scripts), asks the configured
   OpenAI-compatible model (the shared ``CHATBOT_*`` config Gilbert and Albert
   use) to write a plain-language explanation of *how the bot works and how to
   understand it*, then posts it to a Discord channel and/or writes it to a
   Markdown file and stores it in the Turso mirror (``daily_guides``).

A missing Turso URL or token simply disables that half of the job with a log
line; it never crashes the bot.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence

import aiohttp
import discord
from discord.ext import tasks

import config
from gilbert.ai import GilbertAI

# ---------------------------------------------------------------------------
# Environment plumbing. Every value is optional so a deployment can turn each
# feature on, off or point it somewhere else without code changes.
# ---------------------------------------------------------------------------
DEFAULT_HOUR_UTC = 6
DEFAULT_BATCH_SIZE = 100
DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT = 60.0
DEFAULT_GUIDE_MAX_CHARS = 8000
DEFAULT_GUIDE_SOURCE_CHARS = 24000
DEFAULT_GUIDE_LANGUAGE = "English"
DEFAULT_TURSO_TEMP_DIR = "turso-tmp"

_ISO = "%Y-%m-%dT%H:%M:%S"


class TursoError(RuntimeError):
    """Raised when the Turso/libSQL pipeline rejects a statement."""


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y", "enabled")


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, ""))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, ""))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in checks/check_daily_sync.py)
# ---------------------------------------------------------------------------
def libsql_http_endpoint(url: str) -> str:
    """Turn a ``libsql://`` connection string into its HTTP pipeline endpoint.

    Turso speaks the libSQL HTTP protocol at ``https://<host>/v2/pipeline``.
    ``libsql://`` and ``wss://`` map to ``https://``; ``ws://`` maps to
    ``http://`` (local sqld). An empty value stays empty so callers can treat it
    as "not configured".
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("libsql://"):
        raw = "https://" + raw[len("libsql://"):]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    elif raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    raw = raw.rstrip("/")
    if raw.endswith("/v2/pipeline"):
        return raw
    return raw + "/v2/pipeline"


def quote_identifier(name: str) -> str:
    """Quote a SQLite identifier so names with spaces/quotes stay safe."""
    return '"' + str(name).replace('"', '""') + '"'


def insert_statement(table: str, columns: Sequence[str]) -> str:
    cols = ", ".join(quote_identifier(c) for c in columns)
    marks = ", ".join("?" for _ in columns)
    return (
        f"INSERT OR REPLACE INTO {quote_identifier(table)} "
        f"({cols}) VALUES ({marks})"
    )


def ensure_create_if_not_exists(sql: str) -> str:
    """Make a stored ``CREATE TABLE`` idempotent for repeated mirror runs.

    SQLite drops the ``IF NOT EXISTS`` clause when it stores a table's DDL in
    ``sqlite_master``, so replaying it verbatim fails the second time a mirror
    runs. This puts the clause back.
    """
    statement = (sql or "").strip()
    if not statement:
        return statement
    if re.match(r"(?is)^create\s+table\s+if\s+not\s+exists\b", statement):
        return statement
    return re.sub(
        r"(?is)^(create\s+(?:temp\s+|temporary\s+)?table)\s+",
        r"\1 IF NOT EXISTS ",
        statement,
        count=1,
    )


def encode_value(value: Any) -> dict:
    """Encode a Python value as a libSQL typed pipeline argument."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": repr(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "blob",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    return {"type": "text", "value": str(value)}


def build_guide_messages(
    source_text: str,
    *,
    language: str = DEFAULT_GUIDE_LANGUAGE,
    max_words: int = 900,
) -> list[dict]:
    """The prompt pair that turns the bot's own surface into a how-it-works guide."""
    system = (
        "You are writing the definitive, plain-language guide to how the Royal "
        "Court bot works, for the server owner, admins and players of the "
        "Minecraft server The Realm of Rune.\n\n"
        "Explain, in a way a newcomer can follow:\n"
        "1. What the bot is, and the core concepts: nations, kingdoms, individuals, "
        "treasuries, and the Casino / Fishing Guild / Workers' Union / The Crown "
        "house accounts.\n"
        "2. How credits move: admin bank deposit/withdraw, transfers and /give, the "
        "14-day payouts, /daily, shop purchases, gambling and fishing.\n"
        "3. How the two factions and their AI negotiators work (Gilbert for the "
        "Workers' Union, Albert for The Crown), including the 25/75 contract "
        "tranches, quotas and admin approvals.\n"
        "4. The command surface, grouped by area, and what each group is for.\n"
        "5. The rules that keep the economy sound: credits are never minted by "
        "gambling or fishing, individual caps, treasury-funded payouts and the "
        "append-only ledger.\n"
        "6. How to read the data: the transactions ledger, balances, and the daily "
        "Turso mirror.\n\n"
        "Rules:\n"
        "- Use ONLY the material provided. Do not invent commands, numbers or tables.\n"
        "- Do not mention 'the material' or these instructions.\n"
        "- Markdown with short sections and bullets. Keep it under "
        f"{max_words} words.\n"
        f"- Write in {language}.\n"
    )
    user = f"Material about the bot:\n\n{source_text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def format_source(parts: dict, *, max_chars: int = 24000) -> str:
    """Render the collected bot surface as plain text for the model."""
    sections: list[str] = [f"GENERATED: {parts.get('generated_at')} (UTC)"]

    commands = parts.get("commands") or []
    sections.append(
        f"COMMAND SURFACE ({len(commands)} slash commands):\n" + "\n".join(commands)
    )

    modules = parts.get("modules") or []
    sections.append("MODULES (what each file is for):\n" + "\n".join(modules))

    constants = parts.get("constants") or []
    sections.append("ECONOMY CONSTANTS:\n" + "\n".join(constants))

    checks = parts.get("checks") or []
    sections.append(
        f"VERIFICATION SCRIPTS ({len(checks)}): " + ", ".join(checks)
    )

    text = "\n\n".join(sections)
    return text[:max_chars]


def fallback_guide(parts: dict) -> str:
    """A deterministic how-it-works guide used when the AI is unavailable."""
    commands = parts.get("commands") or []
    groups: dict[str, int] = {}
    for line in commands:
        name = line.lstrip("/").split(" ", 1)[0].split("(", 1)[0]
        groups[name] = groups.get(name, 0) + 1

    lines = [
        "# How the Royal Court works",
        "",
        f"_Generated {parts.get('generated_at')} without the AI model._",
        "",
        "## Command surface",
        f"The bot exposes **{len(commands)}** slash commands across "
        f"{len(groups)} groups:",
    ]
    for name, count in sorted(groups.items()):
        lines.append(f"- `/{name}` - {count} command(s)")
    lines += ["", "## Modules", "Each file owns one part of the economy:"]
    for module in parts.get("modules") or []:
        lines.append(f"- {module}")
    lines += ["", "## Economy constants", "The fixed numbers that shape payouts and prices:"]
    for constant in parts.get("constants") or []:
        lines.append(f"- `{constant}`")
    checks = parts.get("checks") or []
    if checks:
        lines += [
            "",
            "## Verification",
            f"{len(checks)} standalone checks prove the money rules: "
            + ", ".join(checks),
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source material: what the bot actually exposes, read from the running bot.
# ---------------------------------------------------------------------------
ROOT_MODULES = (
    "bot",
    "config",
    "database",
    "api",
    "daily_sync",
    "factions",
    "albert_prompt",
    "account_transfers",
    "payouts",
    "daily",
    "coinflip",
    "allin",
    "blackjack",
    "poker",
    "casino",
    "fishing",
    "creation",
    "gambling_ui",
    "checks",
    "utils",
    "contract_limits",
)


def collect_commands(bot) -> list[str]:
    """Every slash command the bot currently exposes, with args and description."""
    tree = getattr(bot, "tree", None)
    if tree is None:
        return []
    lines: list[str] = []
    for command in tree.walk_commands():
        description = " ".join((getattr(command, "description", "") or "").split())
        params = []
        for parameter in getattr(command, "parameters", []) or []:
            optional = "" if getattr(parameter, "required", True) else "?"
            params.append(f"{parameter.name}{optional}")
        suffix = f" ({', '.join(params)})" if params else ""
        lines.append(f"/{command.qualified_name}{suffix} - {description}")
    return sorted(lines)


def collect_modules() -> list[str]:
    """Loaded modules and their one-line purpose, straight from docstrings."""
    import sys

    lines: list[str] = []
    for name, module in sorted(sys.modules.items()):
        if module is None:
            continue
        top = name.split(".")[0]
        if top not in ("cogs", "shop", "gilbert") and name not in ROOT_MODULES:
            continue
        doc = getattr(module, "__doc__", None)
        if not doc:
            continue
        first = " ".join(doc.strip().splitlines()[0].split())
        if first:
            lines.append(f"{name}: {first}")
    return lines


def collect_constants() -> list[str]:
    """The fixed economy numbers from config.py."""
    lines: list[str] = []
    for name, value in sorted(vars(config).items()):
        if not name.isupper():
            continue
        if isinstance(value, bool):
            lines.append(f"{name} = {str(value).lower()}")
        elif isinstance(value, (int, float, str)):
            lines.append(f"{name} = {value}")
    return lines


def collect_checks() -> list[str]:
    """The standalone verification scripts that ship with the bot."""
    directory = Path(__file__).resolve().parent / "checks"
    try:
        return sorted(path.name for path in directory.glob("check_*.py"))
    except OSError:  # pragma: no cover - defensive
        return []


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------
class DailySync:
    """The once-a-day Turso mirror and AI guide job."""

    def __init__(self, bot) -> None:
        self.bot = bot

        # --- schedule ------------------------------------------------------
        self.enabled = env_bool("DAILY_TASK_ENABLED", True)
        self.hour_utc = min(23, max(0, env_int("DAILY_TASK_HOUR_UTC", DEFAULT_HOUR_UTC)))
        # 0 => run once a day at hour_utc. >0 => fixed interval (useful for tests).
        self.interval_seconds = max(0, env_int("DAILY_TASK_INTERVAL_SECONDS", 0))
        # Also run once when the bot becomes ready (so a restart refreshes the
        # mirror and guide instead of waiting for the next scheduled hour).
        self.run_on_start = env_bool("DAILY_TASK_RUN_ON_START", True)
        self.turso_run_on_start = env_bool("TURSO_RUN_ON_START", True)
        self.guide_run_on_start = env_bool("DAILY_GUIDE_RUN_ON_START", True)
        self.start_delay = max(0, env_int("DAILY_TASK_START_DELAY_SECONDS", 5))

        # --- Turso / libSQL mirror ----------------------------------------
        self.turso_url = env_str("TURSO_DATABASE_URL", "")
        self.turso_token = env_str("TURSO_AUTH_TOKEN", "")
        self.turso_endpoint = libsql_http_endpoint(self.turso_url)
        self.turso_sync_enabled = env_bool("TURSO_SYNC_ENABLED", True)
        self.turso_batch = max(1, env_int("TURSO_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE))
        self.turso_page = max(1, env_int("TURSO_SYNC_PAGE_SIZE", DEFAULT_PAGE_SIZE))
        self.turso_timeout = max(1.0, env_float("TURSO_TIMEOUT", DEFAULT_TIMEOUT))
        self.turso_exclude = {
            name.strip()
            for name in env_str("TURSO_SYNC_EXCLUDE_TABLES", "").split(",")
            if name.strip()
        }
        # The mirror has its own runtime (schedule) and its own temp directory,
        # separate from the guide job, so either can be tuned on its own.
        self.turso_run_enabled = env_bool("TURSO_RUN_ENABLED", True)
        self.turso_run_hour = min(
            23, max(0, env_int("TURSO_RUN_HOUR_UTC", self.hour_utc))
        )
        self.turso_run_interval = max(0, env_int("TURSO_RUN_INTERVAL_SECONDS", 0))
        self.turso_temp_dir = env_str("TURSO_TEMP_DIR", DEFAULT_TURSO_TEMP_DIR)

        # --- daily guide ---------------------------------------------------
        self.guide_enabled = env_bool("DAILY_GUIDE_ENABLED", True)
        self.guide_channel_id = max(0, env_int("DAILY_GUIDE_CHANNEL_ID", 0))
        self.guide_path = env_str("DAILY_GUIDE_PATH", "")
        self.guide_max_chars = max(
            400, env_int("DAILY_GUIDE_MAX_CHARS", DEFAULT_GUIDE_MAX_CHARS)
        )
        self.guide_source_chars = max(
            2000, env_int("DAILY_GUIDE_SOURCE_CHARS", DEFAULT_GUIDE_SOURCE_CHARS)
        )
        self.guide_language = (
            env_str("DAILY_GUIDE_LANGUAGE", DEFAULT_GUIDE_LANGUAGE)
            or DEFAULT_GUIDE_LANGUAGE
        )

        self._task: Optional[tasks.Loop] = None
        self._turso_task: Optional[tasks.Loop] = None
        self._startup_task: Optional[asyncio.Task] = None

    # -- configuration view -------------------------------------------------
    @property
    def turso_enabled(self) -> bool:
        return (
            self.turso_sync_enabled
            and bool(self.turso_url)
            and bool(self.turso_token)
            and bool(self.turso_endpoint)
        )

    @property
    def turso_run_configured(self) -> bool:
        """The mirror's own runtime runs only when it is enabled and configured."""
        return self.turso_enabled and self.turso_run_enabled

    @property
    def temp_dir(self) -> Path:
        """Scratch directory owned by the Turso mirror (never the bot's own)."""
        return Path(self.turso_temp_dir or DEFAULT_TURSO_TEMP_DIR).expanduser()

    @property
    def guide_configured(self) -> bool:
        """The guide needs somewhere to go, or generating it just wastes a call."""
        return bool(self.guide_channel_id or self.guide_path or self.turso_enabled)

    @property
    def db(self):
        return self.bot.db

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            self.bot.logger.info("Daily sync disabled (DAILY_TASK_ENABLED=false).")
            return
        if self._task is not None and self._task.is_running():
            return
        if self.interval_seconds > 0:
            loop = tasks.loop(seconds=self.interval_seconds)(self._run_loop)
            when = f"every {self.interval_seconds}s"
        else:
            at = dt.time(hour=self.hour_utc, tzinfo=dt.timezone.utc)
            loop = tasks.loop(time=at)(self._run_loop)
            when = f"daily at {self.hour_utc:02d}:00 UTC"
        self._task = loop
        loop.start()
        self.bot.logger.info(
            "Daily guide started (%s): guide=%s turso=%s.",
            when,
            "on" if self.guide_enabled else "off",
            "on" if self.turso_enabled else "off",
        )

        # The Turso mirror is its own runtime: own schedule, own temp directory.
        if self.turso_run_configured and (
            self._turso_task is None or not self._turso_task.is_running()
        ):
            if self.turso_run_interval > 0:
                mirror = tasks.loop(seconds=self.turso_run_interval)(self._turso_loop)
                mirror_when = f"every {self.turso_run_interval}s"
            else:
                at = dt.time(hour=self.turso_run_hour, tzinfo=dt.timezone.utc)
                mirror = tasks.loop(time=at)(self._turso_loop)
                mirror_when = f"daily at {self.turso_run_hour:02d}:00 UTC"
            self._turso_task = mirror
            mirror.start()
            self.bot.logger.info(
                "Turso mirror started (%s): temp=%s endpoint=%s.",
                mirror_when, self.temp_dir, self.turso_endpoint,
            )

        # Also run once as soon as the bot is ready (opt out per half).
        if self.run_on_start and self._startup_task is None:
            self._startup_task = asyncio.create_task(self._run_on_start())
            self.bot.logger.info(
                "Daily sync will also run once on startup (delay %ss, mirror=%s guide=%s).",
                self.start_delay,
                "on" if self.turso_run_on_start else "off",
                "on" if self.guide_run_on_start else "off",
            )

    async def stop(self) -> None:
        for task in (self._task, self._turso_task):
            if task is not None and task.is_running():
                task.cancel()
        if self._startup_task is not None and not self._startup_task.done():
            self._startup_task.cancel()

    async def _run_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:  # pragma: no cover - never block the job on this
            pass
        await self.run_guide()

    async def _turso_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:  # pragma: no cover
            pass
        try:
            await self.run_mirror()
        except Exception:  # pragma: no cover - network dependent
            self.bot.logger.exception("Daily Turso mirror failed.")

    async def _run_on_start(self) -> None:
        """One-shot run when the bot becomes ready (a restart refreshes things)."""
        try:
            await self.bot.wait_until_ready()
        except Exception:  # pragma: no cover - never block startup on this
            pass
        if self.start_delay:
            try:
                await asyncio.sleep(self.start_delay)
            except asyncio.CancelledError:
                raise
        self.bot.logger.info(
            "Daily sync: running on startup (mirror=%s guide=%s).",
            "on" if self.turso_run_on_start else "off",
            "on" if self.guide_run_on_start else "off",
        )
        if self.turso_run_on_start:
            try:
                await self.run_mirror()
            except Exception:  # pragma: no cover - network dependent
                self.bot.logger.exception("Startup Turso mirror failed.")
        if self.guide_run_on_start:
            try:
                await self.run_guide()
            except Exception:  # pragma: no cover - defensive
                self.bot.logger.exception("Startup daily guide failed.")

    # -- the two jobs -------------------------------------------------------
    async def run_mirror(self) -> Optional[dict[str, int]]:
        """Copy every table/row into Turso and write the temp checkpoint."""
        if not self.turso_enabled:
            self.bot.logger.info(
                "Turso mirror skipped (TURSO_SYNC_ENABLED=false or "
                "TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set)."
            )
            return None
        counts = await self.sync_to_turso()
        self.write_checkpoint(counts)
        return counts

    async def run_guide(self) -> Optional[str]:
        """Build and deliver the daily explanation/guide."""
        if not (self.guide_enabled and self.guide_configured):
            if self.guide_enabled:
                self.bot.logger.info(
                    "Daily guide skipped: set DAILY_GUIDE_CHANNEL_ID or "
                    "DAILY_GUIDE_PATH (or enable Turso) to give it a destination."
                )
            return None
        try:
            parts = self.build_source()
            guide = await self.generate_guide(parts)
            await self.deliver_guide(guide)
            if self.turso_enabled:
                try:
                    await self.store_guide(guide)
                except Exception:  # pragma: no cover - network dependent
                    self.bot.logger.exception(
                        "Could not store the daily guide in Turso."
                    )
            return guide
        except Exception:  # pragma: no cover - defensive
            self.bot.logger.exception("Daily guide failed.")
            return None

    async def run_jobs(self) -> dict:
        """Run both jobs once (manual/testing); the loops call them separately."""
        result: dict[str, Any] = {"turso": None, "guide": None}
        try:
            result["turso"] = await self.run_mirror()
        except Exception:  # pragma: no cover - network dependent
            self.bot.logger.exception("Daily Turso mirror failed.")
        result["guide"] = await self.run_guide()
        return result

    # -- Turso mirror -------------------------------------------------------
    async def local_tables(self) -> list[tuple[str, Optional[str]]]:
        rows = await self.db.fetchall(
            """
            SELECT name, sql FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        tables: list[tuple[str, Optional[str]]] = []
        for row in rows:
            name = row["name"]
            if name in self.turso_exclude:
                continue
            tables.append((name, row["sql"]))
        return tables

    async def table_info(self, table: str) -> list[dict]:
        rows = await self.db.fetchall(f"PRAGMA table_info({quote_identifier(table)})")
        return [
            {
                "name": row["name"],
                "type": (row["type"] or "").strip(),
                "notnull": int(row["notnull"] or 0),
                "default": row["dflt_value"],
                "pk": int(row["pk"] or 0),
            }
            for row in rows
        ]

    async def table_columns(self, table: str) -> list[str]:
        return [column["name"] for column in await self.table_info(table)]

    async def turso_columns(self, table: str, session=None) -> set[str]:
        """Column names already present on the remote table."""
        data = await self._turso_execute(
            [(f"PRAGMA table_info({quote_identifier(table)})", [])], session=session
        )
        # The request may carry a leading PRAGMA, so pick the result that
        # actually has a column list rather than trusting results[0].
        result: dict = {}
        for item in data.get("results", []):
            candidate = (item.get("response") or {}).get("result") or {}
            if "cols" in candidate:
                result = candidate
        col_names = [column.get("name") for column in result.get("cols", [])]
        index = col_names.index("name") if "name" in col_names else 1
        found: set[str] = set()
        for row in result.get("rows", []):
            cell = row[index] if index < len(row) else None
            if cell and cell.get("type") != "null" and cell.get("value"):
                found.add(cell["value"])
        return found

    async def iter_table_rows(self, table: str) -> AsyncIterator[list]:
        offset = 0
        while True:
            rows = await self.db.fetchall(
                f"SELECT * FROM {quote_identifier(table)} "
                f"ORDER BY rowid LIMIT ? OFFSET ?",
                (self.turso_page, offset),
            )
            if not rows:
                return
            yield rows
            if len(rows) < self.turso_page:
                return
            offset += self.turso_page

    async def _turso_execute(
        self, statements: Sequence[tuple[str, Sequence[Any]]], session=None
    ) -> dict:
        """Send one pipeline request to Turso and raise on any SQL error."""
        if not self.turso_endpoint:
            raise TursoError("TURSO_DATABASE_URL is not configured")
        requests = [
            {
                "type": "execute",
                "stmt": {"sql": "PRAGMA foreign_keys=OFF"},
            }
        ]
        # The mirror is a bulk copy, so referential checks belong to the source
        # database: a child row may land in an earlier batch than its parent.
        # libSQL enforces foreign keys by default, so switch them off for this
        # request. The schema still declares the constraints; they just are not
        # checked while copying.
        requests.extend(
            {
                "type": "execute",
                "stmt": {"sql": sql, "args": [encode_value(v) for v in args]},
            }
            for sql, args in statements
        )
        requests.append({"type": "close"})
        headers = {
            "Authorization": f"Bearer {self.turso_token}",
            "Content-Type": "application/json",
        }
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.turso_timeout)
            )
        try:
            async with session.post(
                self.turso_endpoint, json={"requests": requests}, headers=headers
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise TursoError(f"HTTP {resp.status}: {body[:300]}")
        finally:
            if owns_session:
                await session.close()

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise TursoError(f"invalid JSON from Turso: {body[:300]}") from exc
        for result in data.get("results", []):
            if result.get("type") == "error":
                error = result.get("error") or {}
                raise TursoError(error.get("message") or str(error))
        return data

    async def sync_to_turso(self) -> dict[str, int]:
        """Create/upgrade the remote schema and upsert every row of every table."""
        tables = await self.local_tables()
        ddl = [(ensure_create_if_not_exists(sql), []) for _name, sql in tables if sql]
        if ddl:
            await self._turso_execute(ddl)

        counts: dict[str, int] = {}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.turso_timeout)
        ) as session:
            for name, _sql in tables:
                info = await self.table_info(name)
                columns = [column["name"] for column in info]
                if not columns:
                    counts[name] = 0
                    continue
                # Reconcile schema drift: a local migration may have added a
                # column that the remote mirror does not have yet.
                remote = await self.turso_columns(name, session=session)
                missing = [column for column in info if column["name"] not in remote]
                if missing:
                    alters = [
                        (
                            f"ALTER TABLE {quote_identifier(name)} ADD COLUMN "
                            f"{quote_identifier(column['name'])}"
                            + (f" {column['type']}" if column["type"] else ""),
                            [],
                        )
                        for column in missing
                    ]
                    await self._turso_execute(alters, session=session)
                    self.bot.logger.info(
                        "Turso mirror added %d column(s) to %s.", len(missing), name
                    )
                statement = insert_statement(name, columns)
                batch: list[tuple[str, Sequence[Any]]] = []
                total = 0
                async for rows in self.iter_table_rows(name):
                    for row in rows:
                        batch.append((statement, [row[c] for c in columns]))
                        total += 1
                        if len(batch) >= self.turso_batch:
                            await self._turso_execute(batch, session=session)
                            batch = []
                if batch:
                    await self._turso_execute(batch, session=session)
                counts[name] = total

        self.bot.logger.info(
            "Turso mirror complete: %d tables, %d rows (endpoint %s).",
            len(counts), sum(counts.values()), self.turso_endpoint,
        )
        return counts

    def guide_guild_id(self) -> Optional[int]:
        """The guild a guide belongs to.

        ``DAILY_GUIDE_GUILD_ID`` wins, then ``GUILD_ID``, then the bot's only
        guild. Returns ``None`` rather than guessing when the bot serves several
        guilds and neither variable is set.
        """
        for name in ("DAILY_GUIDE_GUILD_ID", "GUILD_ID"):
            raw = env_str(name, "")
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass
        guilds = list(getattr(self.bot, "guilds", []) or [])
        return guilds[0].id if len(guilds) == 1 else None

    async def store_guide(self, guide: str) -> None:
        """Keep a copy of each generated guide in the mirror."""
        await self._turso_execute(
            [
                (
                    """CREATE TABLE IF NOT EXISTS daily_guides (
                           id         INTEGER PRIMARY KEY AUTOINCREMENT,
                           created_at TEXT    NOT NULL,
                           guild_id   INTEGER,
                           content    TEXT    NOT NULL
                       )""",
                    [],
                ),
                (
                    "INSERT INTO daily_guides (created_at, guild_id, content) "
                    "VALUES (?, ?, ?)",
                    [
                        dt.datetime.now(dt.timezone.utc).strftime(_ISO),
                        self.guide_guild_id(),
                        guide,
                    ],
                ),
            ]
        )

    def write_checkpoint(self, counts: dict[str, int]) -> None:
        """Record the last mirror run in the mirror's own temp directory."""
        try:
            directory = self.temp_dir
            directory.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_sync_at": dt.datetime.now(dt.timezone.utc).strftime(_ISO),
                "endpoint": self.turso_endpoint,
                "tables": len(counts),
                "rows": sum(counts.values()),
                "per_table": counts,
            }
            (directory / "last_sync.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            self.bot.logger.exception(
                "Could not write the Turso checkpoint to %s.", self.temp_dir
            )

    # -- daily guide --------------------------------------------------------
    def build_source(self) -> dict:
        """Collect what the bot actually exposes so the model can document it."""
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime(_ISO),
            "commands": collect_commands(self.bot),
            "modules": collect_modules(),
            "constants": collect_constants(),
            "checks": collect_checks(),
        }

    async def generate_guide(self, parts: dict) -> str:
        text = format_source(parts, max_chars=self.guide_source_chars)
        ai = GilbertAI()
        guide: Optional[str] = None
        if ai.enabled:
            try:
                guide = await ai.complete(
                    build_guide_messages(text, language=self.guide_language)
                )
            except Exception:  # pragma: no cover - network dependent
                self.bot.logger.exception("Bot guide AI call failed; using fallback.")
        if not guide:
            guide = fallback_guide(parts)
        return guide[: self.guide_max_chars]

    async def deliver_guide(self, guide: str) -> None:
        if self.guide_path:
            try:
                path = Path(self.guide_path).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(guide + "\n", encoding="utf-8")
                self.bot.logger.info("Daily guide written to %s.", path)
            except OSError:
                self.bot.logger.exception(
                    "Could not write the daily guide to %s.", self.guide_path
                )

        if not self.guide_channel_id:
            return
        try:
            ready = self.bot.is_ready()
        except Exception:  # pragma: no cover
            ready = False
        if not ready:
            self.bot.logger.info("Daily guide: bot not ready, skipping channel post.")
            return
        channel = self.bot.get_channel(self.guide_channel_id)
        if channel is None:
            self.bot.logger.warning(
                "Daily guide channel %s not found.", self.guide_channel_id
            )
            return
        embed = discord.Embed(
            title="How the Royal Court works",
            description=guide[:4000],
            color=config.GOLD_COLOR,
        )
        embed.set_footer(text="The Royal Court - bot guide")
        embed.timestamp = dt.datetime.now(dt.timezone.utc)
        try:
            await channel.send(embed=embed)
        except Exception:  # pragma: no cover - Discord dependent
            self.bot.logger.exception("Could not post the daily guide.")
