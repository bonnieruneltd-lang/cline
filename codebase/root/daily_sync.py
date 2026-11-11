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
   OpenAI-compatible model (the shared ``CHATBOT_*`` config, or the guide's own
   ``DAILY_GUIDE_API_URL`` / ``DAILY_GUIDE_API_KEY`` when they are set) to write
   a plain-language explanation of *how the bot works and how to understand it*,
   then posts it to a Discord channel and/or writes it to a Markdown file and
   stores it in the Turso mirror (``daily_guides``). A failed provider call is
   retried once on ``CHATBOT_FALLBACK_*`` before ``fallback_guide()`` is used, so
   a guide key that runs dry does not cost the day's document.

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
DEFAULT_GUIDE_MAX_CHARS = 24000
DEFAULT_GUIDE_SOURCE_CHARS = 60000
DEFAULT_GUIDE_LANGUAGE = "English"
# The guide is a faithful rewrite, not a chat: a cool temperature and a budget
# big enough for the long, structured document the prompt asks for.
DEFAULT_GUIDE_TEMPERATURE = 0.25
DEFAULT_GUIDE_MAX_TOKENS = 12000
# Roughly six characters per word, used to size the "keep it under N words"
# instruction from the character cap so the two cannot drift apart.
CHARS_PER_WORD = 6
# Markdown overhead (headings, bullets, tables, blank lines) is not part of the
# word count, so a model that fills the whole character budget with words always
# overruns it. The word target is sized at 90% of the cap, which leaves room for
# the markup and keeps a compliant answer inside the limit.
GUIDE_WORD_HEADROOM = 9  # of 10
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
    max_words: int | None = None,
    max_chars: int = DEFAULT_GUIDE_MAX_CHARS,
) -> list[dict]:
    """The prompt pair that turns the bot's own surface into a how-it-works guide."""
    if max_words is None:
        max_words = max(
            600,
            (int(max_chars) * GUIDE_WORD_HEADROOM // 10) // CHARS_PER_WORD,
        )
    system = (
        "You are writing the definitive, plain-language guide to how the Royal "
        "Court bot works, for the server owner, admins and players of the "
        "Minecraft server The Realm of Rune.\n\n"
        "Write a COMPLETE document, not a summary. Cover, in this order, as "
        "Markdown sections with short paragraphs and bullets:\n"
        "1. What the bot is, the currency, and the account types: nations, "
        "kingdoms, individual wallets, and the four house accounts (Casino, "
        "Fishing Guild, Workers' Union, The Crown). Say who may spend from each.\n"
        "2. Every way credits move: admin `/bank deposit` / `/bank withdraw` / "
        "`/bank setbalance`, `/bank transfer`, `/transfer`, `/give`, `/individual "
        "transfer`, `/individual pay`, the 14-day payouts with their formulas and "
        "a worked example, `/daily` with its streak maths and a worked example, "
        "shop and kit purchases, and gambling/fishing settlements.\n"
        "3. The taxes and dues that actually apply, with the percentage, the base "
        "they are charged on, and who receives them: the individual net-worth tax, "
        "Union dues, and the Crown's soldier tax including its grace period.\n"
        "4. The two factions and their negotiators (Gilbert for the Workers' "
        "Union, Albert for The Crown): how `/faction contract` opens a case, the "
        "case-ID format, how approval and the 25/75 tranches work, who can claim, "
        "the per-cycle quota and the leverage ladder, and the fact that membership "
        "is never required.\n"
        "5. Every game with its real numbers from the material: the coinflip win "
        "rate and payout, `/allin`'s n-times payout and odds, the slot paytable "
        "with its edge, roulette's bet types and edge, blackjack's rules, poker's "
        "structure, and `/fish`'s range - plus the one rule they share: the house "
        "must be able to cover the payout, and credits are never minted.\n"
        "6. The command surface: group the commands by area (bank, nation, "
        "kingdom, individual, shop, kit, faction, contract, payout, casino, "
        "fishing, ledger, leaderboard, decree, admin) and give each command one "
        "line saying what it does. Every command in the material must appear at "
        "least once; do not drop the admin-only ones.\n"
        "7. How to read the data: the append-only ledger, balances and "
        "`/ledger` commands, the daily Turso mirror, and what the checks/ suite "
        "proves.\n"
        "8. A worked day in the life: a new player claims `/daily`, joins a "
        "nation, shops, fishes, and requests a contract, step by step with command "
        "names.\n"
        "9. An FAQ of at least six short question/answer pairs covering the "
        "confusions newcomers actually hit (cap, dues, quota, keys, what an admin "
        "must approve, why a wager was refused).\n\n"
        "Rules:\n"
        "- Use ONLY the material provided. Copy the numbers exactly; never invent "
        "a command, rate, item or table.\n"
        "- The ECONOMY RULES section is authoritative: every percentage, rate, cap, "
        "quota and edge in your guide must be taken from it word for word. The raw "
        "ECONOMY CONSTANTS are reference values only - a constant name is not a "
        "rule, and a member discount is never a fee or a tax.\n"
        "- The server is Minecraft Java Edition 1.20.1 on Forge 47.x. Mention "
        "that once where you describe the in-game side; never describe 1.21+ "
        "features.\n"
        "- Do not mention 'the material', 'the source' or these instructions.\n"
        "- Do not print raw CONFIG_CONSTANT_NAMES; translate each constant into "
        "plain English with its number.\n"
        f"- Markdown headings, short sections, bullets. Aim for {max_words} words "
        f"or fewer.\n"
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

    sections.append(
        f"PLATFORM: Minecraft Java Edition {config.MINECRAFT_VERSION} on "
        f"{config.MINECRAFT_LOADER}. {config.MINECRAFT_PLATFORM_NOTE}"
    )

    commands = parts.get("commands") or []
    sections.append(
        f"COMMAND SURFACE ({len(commands)} slash commands):\n" + "\n".join(commands)
    )

    modules = parts.get("modules") or []
    sections.append("MODULES (what each file is for):\n" + "\n".join(modules))

    rules = parts.get("rules") or []
    if rules:
        sections.append(
            "ECONOMY RULES (authoritative - state these exactly, in words):\n"
            + "\n".join(rules)
        )

    constants = parts.get("constants") or []
    sections.append(
        "ECONOMY CONSTANTS (raw values for reference; a name here is NOT a rule, "
        "and a discount is not a fee):\n" + "\n".join(constants)
    )

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


GUIDE_TRIM_NOTE = (
    "\n\n---\n\n_This guide was trimmed at the size limit; every command and its "
    "options are still listed in Discord's `/` menu._"
)


def _cut_at_boundary(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, preferring a structural break.

    A plain ``text[:limit]`` split the live guide in the middle of a word (and
    inside an inline code span), so the tail of every over-long guide was
    unusable. Paragraph, line, sentence and finally word boundaries are tried in
    that order. Only the last 40% of the window may be given up to reach one, so
    a late break cannot silently throw away most of the document.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    window = text[:limit]
    floor = int(limit * 0.6)
    for separator in ("\n\n", "\n"):
        index = window.rfind(separator)
        if index >= floor:
            return window[:index]
    for separator in (". ", "! ", "? "):
        index = window.rfind(separator)
        if index >= floor:
            return window[: index + 1]
    index = window.rfind(" ")
    return window[:index] if index > 0 else window


def _drop_open_markup(text: str) -> str:
    """Remove a fenced block or inline code span the cut left open."""
    if text.count("```") % 2:
        index = text.rfind("```")
        if index > 0:
            text = text[:index]
    if text.count("`") % 2:
        index = text.rfind("`")
        if index > 0:
            text = text[:index]
    return text


def trim_guide(text: str, max_chars: int = DEFAULT_GUIDE_MAX_CHARS) -> str:
    """Fit a generated guide into ``max_chars`` without breaking its tail.

    The guide is a document, not a chat message: cutting it at an arbitrary
    character leaves a half-written word, an unclosed ``code span`` and no sign
    that anything is missing. This trims at the last paragraph/line/sentence/word
    boundary, drops markup the cut left open, and appends
    :data:`GUIDE_TRIM_NOTE` so the result reads as intentionally shortened.
    """
    text = str(text or "")
    max_chars = max(1, int(max_chars))
    if len(text) <= max_chars:
        return text
    note = GUIDE_TRIM_NOTE
    if max_chars <= len(note):
        # Degenerate cap (the env floor is 400): the note cannot fit, so the
        # hard limit wins over readability.
        return text[:max_chars]
    body = _drop_open_markup(_cut_at_boundary(text, max_chars - len(note)))
    body = body.rstrip()
    if not body:
        return text[:max_chars]
    return (body + note)[:max_chars]


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


def collect_rules() -> list[str]:
    """Plain-English economy facts, derived from the same functions the bot runs.

    The raw constants are ambiguous to a model: ``UNION_MEMBER_DISCOUNT = 50`` is
    a shop discount, ``CONTRACTS_PER_CYCLE = {'kingdom': 3}`` is a quota, and a
    generated guide has already mis-read them as "50% dues" and "a three-cycle
    contract". This section states the rules in words and the prompt makes it
    authoritative, so a percentage in the guide can only come from here.
    """

    def pct(bps: int) -> str:
        return f"{bps / 100:g}%"

    facts: list[str] = []
    facts.append(
        f"Currency: {config.CURRENCY_NAME}. An individual wallet is capped at "
        f"{config.INDIVIDUAL_MAX_CREDITS:,} credits; nations and kingdoms have no cap."
    )
    facts.append(
        "Union dues, members only, charged every natural payout cycle on the whole "
        f"balance after the payout: nation {pct(config.UNION_TAX_BPS['nation'])}, "
        f"kingdom {pct(config.UNION_TAX_BPS['kingdom'])}. Non-members pay nothing."
    )
    facts.append(
        f"Individual net-worth tax: {config.individual_tax_label()}% of every "
        "individual wallet on each payout run, paid into the Workers' Union treasury."
    )
    facts.append(
        f"Union member shop discount: {config.UNION_MEMBER_DISCOUNT}% off "
        f"{', '.join(sorted(config.UNION_MEMBER_DISCOUNT_CATEGORIES))}. It is a "
        "discount on purchases, never a fee or a tax."
    )
    facts.append(
        f"Nation payout, every {config.PAYOUT_CYCLE_DAYS} days: {config.NATION_BASE:,} "
        f"base + {config.NATION_PER_HAPPY_WORKER} per happy worker + "
        f"{config.NATION_PER_REGULAR_WORKER} per regular worker + "
        f"{config.NATION_PER_HAPPY_SOLDIER} per happy soldier + "
        f"{config.NATION_PER_REGULAR_SOLDIER} per regular soldier."
    )
    facts.append(
        f"Kingdom payout, every {config.PAYOUT_CYCLE_DAYS} days: {config.KINGDOM_BASE:,} "
        f"base + {config.KINGDOM_PER_MEMBER_NATION:,} per member nation + "
        f"{config.KINGDOM_PER_HAPPY_WORKER} per happy worker + "
        f"{config.KINGDOM_PER_REGULAR_WORKER} per regular worker + "
        f"{config.KINGDOM_PER_HAPPY_SOLDIER} per happy soldier + "
        f"{config.KINGDOM_PER_REGULAR_SOLDIER} per regular soldier, with worker and "
        "soldier counts summed across the member nations."
    )
    facts.append(
        f"/daily pays {config.DAILY_BASE:,} credits times a multiplier: one claim per "
        f"{config.DAILY_COOLDOWN_HOURS} hours, +{config.DAILY_BP_STEP / 100:g} points "
        f"per consecutive claim to a ceiling of {config.DAILY_BP_CAP / 100:g}% "
        f"({config.DAILY_BASE * config.DAILY_BP_CAP // 10_000:,} credits), "
        f"-{config.DAILY_BP_MISS_PENALTY / 100:g} points per missed day down to a "
        f"{config.DAILY_BP_FLOOR / 100:g}% floor "
        f"({config.DAILY_BASE * config.DAILY_BP_FLOOR // 10_000:,} credits)."
    )
    facts.append(
        f"Contracts: budget {config.CONTRACT_MIN_CREDITS:,} to "
        f"{config.CONTRACT_MAX_CREDITS:,} credits; {config.CONTRACT_UPFRONT_PCT}% is "
        f"claimable at approval and {100 - config.CONTRACT_UPFRONT_PCT}% at release. "
        f"Quota per natural cycle: {config.CONTRACTS_PER_CYCLE['nation']} per nation, "
        f"{config.CONTRACTS_PER_CYCLE['kingdom']} per kingdom. The requested faction "
        "funds it from its own treasury, the supplier is never charged, and faction "
        "membership is never required."
    )
    facts.append(
        "Contract leverage by contract value: 3x the party's balance up to "
        f"{config.CONTRACT_LEVERAGE_TIERS[0][0]:,}, 2x up to "
        f"{config.CONTRACT_LEVERAGE_TIERS[1][0]:,}, then 1x. Case IDs are "
        "`UN-<TAG>-<NNN>` for a Workers' Union case and `CR-<TAG>-<NNN>` for a Crown "
        "case, each with its own counter."
    )
    facts.append(
        f"Crown soldier tax: {pct(config.CROWN_TAX_BPS)} of the regular-soldier payout "
        f"line for Crown members, after a {config.CROWN_TAX_GRACE_DAYS}-day grace period."
    )
    return facts


def collect_game_rules() -> list[str]:
    """The gambling, fishing and shop facts, with the odds the modules compute."""
    import allin
    import coinflip
    import roulette
    import slots

    facts: list[str] = []
    facts.append(
        f"Coinflip: {coinflip.win_label()}% win chance, "
        f"{config.COINFLIP_PAYOUT_MULTIPLIER}x payout, house edge "
        f"{coinflip.house_edge_bp() / 100:g}%; stakes {config.COINFLIP_MIN_STAKE:,} to "
        f"{config.COINFLIP_MAX_STAKE:,}."
    )
    facts.append(
        f"All-in: pick n from {config.ALLIN_MIN_NUMBER} to {config.ALLIN_MAX_NUMBER}; "
        f"a win pays n times the stake at 1 in 1.1n (n=10 is {allin.chance_label(10)}), "
        f"house edge {float(allin.house_edge()) * 100:.2f}%, and the whole wallet is "
        "staked."
    )
    facts.append(
        f"Slots: three weighted reels; only three of a kind pays, {slots.MAX_PAYOUT}x "
        f"at most. Return {slots.return_label()}, house edge {slots.edge_label()}, "
        f"{slots.chance_label()} spins pay. Stakes {config.SLOTS_MIN_STAKE:,} to "
        f"{config.SLOTS_MAX_STAKE:,}. Paytable: "
        + "; ".join(f"{symbol.label} {symbol.payout}x" for symbol in slots.SYMBOLS)
        + "."
    )
    straight = roulette.make_bet("straight", 0)
    facts.append(
        "Roulette: European single-zero wheel (one green zero, 1-36, eighteen red, "
        "eighteen black). Straight up pays 35:1, even money 1:1, dozens and columns "
        f"2:1, and every bet carries the same {roulette.edge_label(straight)} house "
        f"edge. Stakes {config.ROULETTE_MIN_STAKE:,} to {config.ROULETTE_MAX_STAKE:,}."
    )
    facts.append(
        "Blackjack: a natural 21 pays 3:2, any other win even money, a push returns "
        "the stake; the dealer hits to 16 and stands on all 17s. Stakes "
        f"{config.BLACKJACK_MIN_STAKE:,} to {config.BLACKJACK_MAX_STAKE:,}."
    )
    facts.append(
        f"Fishing: every catch pays {config.FISHING_MIN_CREDITS} to "
        f"{config.FISHING_MAX_CREDITS} credits, funded by the Fishing Guild."
    )
    facts.append(
        f"Royal Shop: {int(config.NATION_DISCOUNT * 100)}% off when a nation treasury "
        f"pays and {int(config.KINGDOM_DISCOUNT * 100)}% off from a kingdom treasury; "
        f"{config.SHULKER_BOX_FEE:,} credits per shulker box once an order needs more "
        f"than {config.SHOP_SLOT_LIMIT} stacks. Kits hold up to "
        f"{config.KIT_MAX_ITEM_TYPES} item types with a "
        f"{config.KIT_COOLDOWN_DAYS}-day cooldown."
    )
    facts.append(
        "Every wager and every catch settles against a house account, so the house "
        "must be able to cover the payout before the bet is allowed; no game mints "
        "credits."
    )
    return facts


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
        # The guide may run on its own model: a long, faithful rewrite wants a
        # cooler temperature and a bigger budget than a snappy negotiation
        # reply. Empty DAILY_GUIDE_MODEL means "same model as the personas".
        self.guide_model = env_str("DAILY_GUIDE_MODEL", "")
        # ...and on its own provider. The personas answer in a few seconds on a
        # fast chat model; a once-a-day document is better served by a separate
        # key, so it can run dry without taking the personas down with it.
        self.guide_api_url = env_str("DAILY_GUIDE_API_URL", "")
        self.guide_api_key = env_str("DAILY_GUIDE_API_KEY", "")
        # The shared backup provider is used by both, but the guide's own model
        # is the big tier while the personas use the fast one.
        self.guide_fallback_model = env_str("DAILY_GUIDE_FALLBACK_MODEL", "")
        self.guide_temperature = max(
            0.0, min(1.0, env_float("DAILY_GUIDE_TEMPERATURE", DEFAULT_GUIDE_TEMPERATURE))
        )
        self.guide_max_tokens = max(
            512, env_int("DAILY_GUIDE_MAX_TOKENS", DEFAULT_GUIDE_MAX_TOKENS)
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
            "rules": collect_rules() + collect_game_rules(),
            "constants": collect_constants(),
            "checks": collect_checks(),
        }

    async def generate_guide(self, parts: dict) -> str:
        text = format_source(parts, max_chars=self.guide_source_chars)
        ai = GilbertAI(
            model=self.guide_model or None,
            temperature=self.guide_temperature,
            max_tokens=self.guide_max_tokens,
            api_url=self.guide_api_url or None,
            api_key=self.guide_api_key or None,
            fallback_model=self.guide_fallback_model or None,
        )
        guide: Optional[str] = None
        if ai.enabled:
            try:
                guide = await ai.complete(
                    build_guide_messages(
                        text,
                        language=self.guide_language,
                        max_chars=self.guide_max_chars,
                    )
                )
            except Exception:  # pragma: no cover - network dependent
                self.bot.logger.exception("Bot guide AI call failed; using fallback.")
        if not guide:
            guide = fallback_guide(parts)
        return trim_guide(guide, self.guide_max_chars)

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
