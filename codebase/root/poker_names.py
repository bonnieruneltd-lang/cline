"""Minecraft-themed poker bot names, using Gilbert's AI credentials.

``poker.py`` stays pure: this module owns the optional AI call and the curated
fallback pool. ``Casino.__init__`` starts a background refresh, and ``/poker``
samples three names synchronously, so an AI request can never delay or expire an
interaction. The AI request uses ``GilbertAI`` and therefore the same
``AI_API_KEY`` / ``AI_API_URL`` / ``AI_MODEL`` environment as Gilbert.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import random
import re
import time

import config
from gilbert.ai import GilbertAI

log = logging.getLogger("royalcourt.poker.names")

_REFRESH_SECONDS = 6 * 60 * 60
_AI_TIMEOUT_SECONDS = 8.0
_pool: list[str] = []
_last_refresh = 0.0
_task: asyncio.Task | None = None
_ai: GilbertAI | None = None

_SYSTEM_PROMPT = (
    "You name Minecraft characters for a poker table. Reply ONLY with a JSON "
    "array of strings. No prose, no markdown fences."
)
_USER_PROMPT = (
    "Generate 16 unique, short Minecraft-themed poker bot names. Mix mobs, "
    "blocks, biomes, redstone, enchanting and villager words. Each name is "
    "2-4 words and under 32 characters. Examples: Creeper Cass, Redstone Rex, "
    "Ender Ellie. Return only a JSON array of strings."
)
_BLOCKED = {"name", "bot", "player", "example", "unknown", "null", "none"}


def _client() -> GilbertAI:
    global _ai
    if _ai is None:
        _ai = GilbertAI()
    return _ai


def _clean_name(value) -> str:
    if not isinstance(value, str):
        return ""
    name = " ".join(value.strip().strip("\"'`").split())
    name = name.lstrip("-*•0123456789. )(").strip()
    if not 2 <= len(name) <= 32:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 '_-]*", name):
        return ""
    if name.casefold() in _BLOCKED:
        return ""
    return name


def parse_names(raw: str | None, limit: int = 24) -> list[str]:
    """Turn a model reply into clean, unique names.

    Accepts a JSON array, a ``{"names": [...]}`` object, a fenced JSON block, a
    Python-style list, quoted items or bullet/numbered lines. Anything that does
    not look like a short name is dropped.
    """
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    data = None
    for loader in (json.loads, ast.literal_eval):
        try:
            data = loader(text)
            break
        except (ValueError, SyntaxError, TypeError):
            continue
    if isinstance(data, dict):
        data = data.get("names") or data.get("bots") or data.get("players")
    if isinstance(data, list):
        values = data
    else:
        quoted = re.findall(r'"([^"\n]{2,40})"', text)
        values = quoted or [line for line in text.splitlines() if line.strip()]

    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _clean_name(value)
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
            if len(names) >= limit:
                break
    return names


def fallback_names(
    count: int = 3, rng: random.Random | random = random
) -> tuple[str, ...]:
    """Pick from the curated Minecraft-themed pool, never the old medieval names."""
    pool = list(config.POKER_BOT_NAME_POOL)
    if not pool:
        return tuple(f"Bot {index + 1}" for index in range(count))
    if len(pool) >= count:
        return tuple(rng.sample(pool, count))
    return tuple((pool * ((count + len(pool) - 1) // len(pool)))[:count])


async def _refresh() -> None:
    global _pool, _last_refresh
    client = _client()
    if not client.enabled:
        _last_refresh = time.monotonic()
        return
    try:
        reply = await asyncio.wait_for(
            client.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _USER_PROMPT},
                ]
            ),
            timeout=_AI_TIMEOUT_SECONDS,
        )
        names = parse_names(reply)
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("Poker name refresh failed: %s", exc)
        names = []
    if names:
        _pool = names
        log.info("Poker name pool refreshed with %d AI names.", len(names))
    _last_refresh = time.monotonic()


def start_refresh() -> None:
    """Start a background AI refresh if the event loop is running."""
    global _task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_refresh())


def table_names(
    count: int = 3, rng: random.Random | random = random
) -> tuple[str, ...]:
    """Names for one table, instantly. AI names when warm, themed fallback otherwise."""
    if not _pool or time.monotonic() - _last_refresh > _REFRESH_SECONDS:
        start_refresh()
    pool = list(_pool)
    if len(pool) >= count:
        return tuple(rng.sample(pool, count))
    return fallback_names(count, rng)
