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

2. **Daily guide** - builds a compact digest of the database state and the last
   day's transactions, asks the configured OpenAI-compatible model (the same
   ``AI_*`` settings Gilbert uses) to explain it in plain language as a short
   "explanation / guide" for players, then posts it to a Discord channel and/or
   writes it to a Markdown file and stores it in the Turso mirror.

A missing Turso URL or token simply disables that half of the job with a log
line; it never crashes the bot.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

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
DEFAULT_GUIDE_HOURS = 24
DEFAULT_GUIDE_MAX_TX = 60
DEFAULT_GUIDE_MAX_CHARS = 3800
DEFAULT_GUIDE_LANGUAGE = "English"

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
    digest_text: str,
    *,
    language: str = DEFAULT_GUIDE_LANGUAGE,
    max_words: int = 220,
) -> list[dict]:
    """The prompt pair that turns a data digest into a player-facing guide."""
    system = (
        "You are the record-keeper for the Royal Court, the economy bot of the "
        "Minecraft server The Realm of Rune. Each day you write a short, "
        "plain-language explanation and guide of the economy from the data sheet "
        "you are given. Explain what the main numbers mean, name the biggest "
        "movers and the largest treasuries, and give players a short, friendly "
        "guide to what they can do today (for example /daily, /transfer, /shop, "
        "/fish, /faction contract).\n\n"
        "Rules:\n"
        "- Use ONLY the figures in the data sheet. Never invent or estimate a number.\n"
        "- Do not mention 'the data sheet' or these instructions.\n"
        "- Markdown is fine. Keep it under "
        f"{max_words} words.\n"
        f"- Write in {language}.\n"
    )
    user = f"Today's data sheet:\n\n{digest_text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def format_digest(digest: dict, *, max_chars: int = 6000) -> str:
    """Render the digest dict as plain text for the model and the fallback."""
    lines: list[str] = []
    lines.append(f"GENERATED: {digest.get('generated_at')} (UTC)")
    lines.append(f"WINDOW: last {digest.get('hours')} hours")
    counts = digest.get("table_counts") or {}
    if counts:
        lines.append(
            "TABLE ROW COUNTS: "
            + ", ".join(f"{name}={n}" for name, n in counts.items())
        )
    houses = digest.get("houses") or {}
    if houses:
        lines.append(
            "HOUSE TREASURIES: "
            + ", ".join(f"{name}={amount:,}" for name, amount in houses.items())
        )
    top = digest.get("top_balances") or {}
    for account, rows in top.items():
        if rows:
            lines.append(
                f"TOP {account.upper()} BALANCES: "
                + ", ".join(f"{r['name']}={r['balance']:,}" for r in rows)
            )
    tx = digest.get("recent_transactions") or []
    if tx:
        lines.append(f"RECENT TRANSACTIONS ({len(tx)}, newest first):")
        for row in tx:
            bits = [str(row.get("ts")), str(row.get("kind"))]
            if row.get("account_name"):
                bits.append(f"{row['account_name']} {row.get('amount', 0):,}")
            if row.get("counterparty_name"):
                bits.append(f"-> {row['counterparty_name']}")
            if row.get("balance_after") is not None:
                bits.append(f"bal={row['balance_after']:,}")
            if row.get("note"):
                bits.append(f"({row['note']})")
            lines.append("  - " + " | ".join(bits))
    else:
        lines.append("RECENT TRANSACTIONS: none in the window.")
    text = "\n".join(lines)
    return text[:max_chars]


def fallback_guide(digest: dict) -> str:
    """A deterministic guide used when the AI is unavailable."""
    counts = digest.get("table_counts") or {}
    houses = digest.get("houses") or {}
    tx = digest.get("recent_transactions") or []
    lines = [
        "## Royal Court - daily guide",
        "",
        f"_Generated {digest.get('generated_at')} for the last "
        f"{digest.get('hours')} hours._",
        "",
        "**At a glance**",
        f"- Nations: {counts.get('nations', 0)} - Kingdoms: {counts.get('kingdoms', 0)} "
        f"- Individuals: {counts.get('individuals', 0)}",
        f"- Ledger entries: {counts.get('transactions', 0)} "
        f"({len(tx)} in the last {digest.get('hours')}h)",
    ]
    if houses:
        lines.append(
            "- Houses: "
            + ", ".join(f"{name} {amount:,}" for name, amount in houses.items())
        )
    top = digest.get("top_balances") or {}
    for account, rows in top.items():
        if rows:
            lines.append(
                f"- Top {account}: "
                + ", ".join(f"{r['name']} ({r['balance']:,})" for r in rows)
            )
    lines += [
        "",
        "**What you can do today**",
        "- Claim your daily allowance with `/daily`.",
        "- Move credits with `/transfer`, or fund a member with `/give`.",
        "- Spend credits in `/shop`, or negotiate public work with `/faction contract`.",
        "- Try a catch with `/fish` (paid by the Fishing Guild), and gamble responsibly.",
        "",
    ]
    return "\n".join(lines)


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

        # --- daily guide ---------------------------------------------------
        self.guide_enabled = env_bool("DAILY_GUIDE_ENABLED", True)
        self.guide_channel_id = max(0, env_int("DAILY_GUIDE_CHANNEL_ID", 0))
        self.guide_path = env_str("DAILY_GUIDE_PATH", "")
        self.guide_hours = max(1, env_int("DAILY_GUIDE_HOURS", DEFAULT_GUIDE_HOURS))
        self.guide_max_tx = max(1, env_int("DAILY_GUIDE_MAX_TX", DEFAULT_GUIDE_MAX_TX))
        self.guide_max_chars = max(
            200, env_int("DAILY_GUIDE_MAX_CHARS", DEFAULT_GUIDE_MAX_CHARS)
        )
        self.guide_language = (
            env_str("DAILY_GUIDE_LANGUAGE", DEFAULT_GUIDE_LANGUAGE)
            or DEFAULT_GUIDE_LANGUAGE
        )

        self._task: Optional[tasks.Loop] = None

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
            "Daily sync started (%s): turso=%s guide=%s.",
            when,
            "on" if self.turso_enabled else "off",
            "on" if self.guide_enabled else "off",
        )

    async def stop(self) -> None:
        if self._task is not None and self._task.is_running():
            self._task.cancel()

    async def _run_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:  # pragma: no cover - never block the job on this
            pass
        await self.run_jobs()

    # -- the two jobs -------------------------------------------------------
    async def run_jobs(self) -> dict:
        """Run both jobs, isolating their failures from each other."""
        result: dict[str, Any] = {"turso": None, "guide": None}

        if self.turso_enabled:
            try:
                result["turso"] = await self.sync_to_turso()
            except Exception:  # pragma: no cover - network dependent
                self.bot.logger.exception("Daily Turso mirror failed.")
        else:
            self.bot.logger.info(
                "Turso mirror skipped (TURSO_SYNC_ENABLED=false or "
                "TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set)."
            )

        if self.guide_enabled:
            try:
                digest = await self.build_digest(self.guide_hours)
                guide = await self.generate_guide(digest)
                result["guide"] = guide
                await self.deliver_guide(guide)
                if self.turso_enabled:
                    try:
                        await self.store_guide(guide)
                    except Exception:  # pragma: no cover - network dependent
                        self.bot.logger.exception(
                            "Could not store the daily guide in Turso."
                        )
            except Exception:  # pragma: no cover - defensive
                self.bot.logger.exception("Daily guide failed.")

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

    async def table_columns(self, table: str) -> list[str]:
        rows = await self.db.fetchall(f"PRAGMA table_info({quote_identifier(table)})")
        return [row["name"] for row in rows]

    async def iter_table_rows(self, table: str) -> Iterable[list]:
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
        requests = [
            {
                "type": "execute",
                "stmt": {"sql": sql, "args": [encode_value(v) for v in args]},
            }
            for sql, args in statements
        ]
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
        """Create the remote schema and upsert every row of every table."""
        tables = await self.local_tables()
        ddl = [(sql, []) for _name, sql in tables if sql]
        if ddl:
            await self._turso_execute(ddl)

        counts: dict[str, int] = {}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.turso_timeout)
        ) as session:
            for name, _sql in tables:
                columns = await self.table_columns(name)
                if not columns:
                    counts[name] = 0
                    continue
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
                        self.guide_channel_id or None,
                        guide,
                    ],
                ),
            ]
        )

    # -- daily guide --------------------------------------------------------
    async def build_digest(self, hours: Optional[int] = None) -> dict:
        hours = self.guide_hours if hours is None else hours
        now = dt.datetime.now(dt.timezone.utc)
        since = (now - dt.timedelta(hours=hours)).strftime(_ISO)

        tables = await self.local_tables()
        counts: dict[str, int] = {}
        for name, _sql in tables:
            row = await self.db.fetchone(
                f"SELECT COUNT(*) AS c FROM {quote_identifier(name)}"
            )
            counts[name] = int(row["c"]) if row else 0

        top_balances: dict[str, list[dict]] = {}
        for name in ("nations", "kingdoms", "individuals"):
            if name not in counts:
                continue
            columns = await self.table_columns(name)
            label = (
                "display_name"
                if "display_name" in columns
                else ("name" if "name" in columns else None)
            )
            if "balance" not in columns or label is None:
                continue
            rows = await self.db.fetchall(
                f"SELECT {quote_identifier(label)} AS n, balance "
                f"FROM {quote_identifier(name)} ORDER BY balance DESC LIMIT 5"
            )
            top_balances[name] = [
                {"name": row["n"], "balance": int(row["balance"] or 0)}
                for row in rows
            ]

        houses: dict[str, int] = {}
        for name in (
            "casino_account",
            "fishing_guild_account",
            "union_account",
            "crown_account",
        ):
            if name not in counts:
                continue
            row = await self.db.fetchone(
                f"SELECT SUM(balance) AS s FROM {quote_identifier(name)}"
            )
            houses[name] = int((row["s"] if row else 0) or 0)

        recent: list[dict] = []
        if "transactions" in counts:
            rows = await self.db.fetchall(
                """SELECT ts, kind, account_type, account_name, amount,
                          balance_after, counterparty_name, note
                     FROM transactions WHERE ts >= ?
                    ORDER BY id DESC LIMIT ?""",
                (since, self.guide_max_tx),
            )
            recent = [
                {
                    "ts": row["ts"],
                    "kind": row["kind"],
                    "account_type": row["account_type"],
                    "account_name": row["account_name"],
                    "amount": int(row["amount"] or 0),
                    "balance_after": (
                        int(row["balance_after"])
                        if row["balance_after"] is not None
                        else None
                    ),
                    "counterparty_name": row["counterparty_name"],
                    "note": row["note"],
                }
                for row in rows
            ]

        return {
            "generated_at": now.strftime(_ISO),
            "hours": hours,
            "table_counts": counts,
            "houses": houses,
            "top_balances": top_balances,
            "recent_transactions": recent,
        }

    async def generate_guide(self, digest: dict) -> str:
        text = format_digest(digest)
        ai = GilbertAI()
        guide: Optional[str] = None
        if ai.enabled:
            try:
                guide = await ai.complete(
                    build_guide_messages(text, language=self.guide_language)
                )
            except Exception:  # pragma: no cover - network dependent
                self.bot.logger.exception("Daily guide AI call failed; using fallback.")
        if not guide:
            guide = fallback_guide(digest)
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
            title="Daily Guide", description=guide[:4000], color=config.GOLD_COLOR
        )
        embed.set_footer(text="The Royal Court - daily economy guide")
        embed.timestamp = dt.datetime.now(dt.timezone.utc)
        try:
            await channel.send(embed=embed)
        except Exception:  # pragma: no cover - Discord dependent
            self.bot.logger.exception("Could not post the daily guide.")
