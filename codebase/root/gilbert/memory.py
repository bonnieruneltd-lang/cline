"""
gilbert/memory.py
=================
The negotiators' memory: a whole negotiation turned into a bounded prompt.

Two problems, two answers:

* **Forgetting.** ``contracts.history`` is a sliding window of the last
  ``CONTRACT_HISTORY_LIMIT`` messages; every turn that falls out of it used to
  be destroyed. A case negotiated over two evenings then contradicted itself:
  the collection point, the agreed quantity or the "no, not that farm" answer
  was simply gone. Storage is cheap, so the full transcript is now kept in
  ``albert_memory`` (one row per speaker line) and never pruned per case.
* **Prompt size.** The model still gets a *window*: the newest turns verbatim
  plus a condensed digest of everything older. The digest keeps the head of
  each older line, which is where proposals, coordinates and prices sit, and
  is built in-process — no extra provider call, so it cannot fail, cannot cost
  money and cannot time out mid-negotiation.

Everything here is pure: no Discord, no database, no network. The cog decides
what the turns are; :func:`build_window` decides how much of them the model
sees, and ``checks/check_gilbert.py`` exercises that decision directly.
"""
from __future__ import annotations

from typing import Iterable, Optional

# A single turn is capped even in the verbatim window: one pasted screenshot
# description should not eat the whole context.
TURN_CHARS = 2000
# Turns sent verbatim. Older ones are condensed.
RECENT_TURNS = 16
# Hard ceiling for the verbatim window, in characters (~4 chars/token).
CONTEXT_CHARS = 8000
# Ceiling for the digest block.
DIGEST_CHARS = 2000
# Characters kept per condensed line.
DIGEST_LINE_CHARS = 180
# Prior-case memory block.
PRIOR_TURNS = 8
PRIOR_CHARS = 1200

DIGEST_HEADER = (
    "EARLIER IN THIS NEGOTIATION (oldest first, condensed): this is your own "
    "record of what was already said and agreed in this case. Treat it as "
    "binding context, never as a source for balances, quotas or rules."
)
PRIOR_HEADER = (
    "PRIOR EXCHANGES WITH THIS ACCOUNT (earlier cases, newest last): your own "
    "memory of previous negotiations with this nation or kingdom. Use it only "
    "for continuity; every figure and rule still comes from the briefing."
)
# When the budget is tight, the label is shortened rather than dropped: losing
# the whole digest because its preamble does not fit would silently re-open the
# forgetting bug this module exists to close.
SHORT_DIGEST_HEADER = "EARLIER IN THIS NEGOTIATION (condensed):"

_ROLES = ("user", "assistant")


def _role(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in _ROLES else "user"


def _text(value) -> str:
    """One line of text: whitespace collapsed, never ``None``."""
    return " ".join(str(value or "").split())


def normalise(turns: Optional[Iterable], *, turn_chars: int = TURN_CHARS) -> list[dict]:
    """Clean a turn list: drop empty lines, cap each one, keep the order.

    Accepts anything list-like of ``{"role", "content"}`` mappings (the legacy
    ``contracts.history`` JSON, the ``albert_memory`` rows mapped by the cog, or
    a hand-built list in a check). Malformed entries are skipped rather than
    raising: a single bad row must not lose a negotiation.
    """
    cleaned: list[dict] = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        content = _text(turn.get("content"))
        if not content:
            continue
        if turn_chars and len(content) > turn_chars:
            content = content[: max(0, turn_chars - 1)].rstrip() + "…"
        cleaned.append({"role": _role(turn.get("role")), "content": content})
    return cleaned


def transcript_text(turns: Iterable[dict]) -> str:
    """Whole conversation as plain text (used by the digest and by checks)."""
    return "\n".join(f"{turn['role']}: {turn['content']}" for turn in turns)


def _clip(text: str, limit: int) -> str:
    """Cut to ``limit`` characters without splitting a word."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(" "), window.rfind("\n"))
    # Leave a little headroom so the ellipsis never overflows the cap.
    if cut < limit // 2:
        cut = max(0, limit - 1)
    return window[:cut].rstrip() + "…"



def digest(
    turns: Iterable[dict],
    *,
    header: str = DIGEST_HEADER,
    max_chars: int = DIGEST_CHARS,
    line_chars: int = DIGEST_LINE_CHARS,
) -> Optional[str]:
    """Condense older turns into one system block, or ``None`` when empty.

    Each line keeps its speaker and the head of what was said, so the earliest
    lines — the ones that fall out of the window first — survive. The block is
    capped at ``max_chars`` and clipped on a word boundary, never mid-word.
    """
    lines: list[str] = []
    for turn in normalise(turns):
        line = f"- {turn['role']}: {turn['content']}"
        if line_chars and len(line) > line_chars:
            line = _clip(line, line_chars)
        candidate = header + "\n" + "\n".join(lines + [line])
        if max_chars and len(candidate) > max_chars:
            # Not enough room for another line: keep what already fits.
            break
        lines.append(line)
    if not lines:
        return None
    block = header + "\n" + "\n".join(lines)
    if max_chars and len(block) > max_chars:
        room = max_chars - len(header) - 1
        if room <= 0:
            return None
        block = header + "\n" + _clip("\n".join(lines), room)
    return block


def build_window(
    turns: Optional[Iterable],
    *,
    recent: int = RECENT_TURNS,
    context_chars: int = CONTEXT_CHARS,
    digest_chars: int = DIGEST_CHARS,
) -> list[dict]:
    """The messages the model actually sees for this negotiation.

    Newest ``recent`` turns verbatim, everything older condensed into a single
    system block. If the window still exceeds ``context_chars`` the oldest
    *verbatim* turns are handed to the digest first, then the digest itself is
    shortened; the newest turn is always kept, because a reply with no question
    in it is useless.
    """
    history = normalise(turns)
    if not history:
        return []

    keep = max(1, int(recent))
    trimmed = history[max(0, len(history) - keep):]
    older = history[: len(history) - len(trimmed)]

    # Budget: move the oldest verbatim turns into the digest until it fits.
    while len(trimmed) > 1 and context_chars and _chars(trimmed) > context_chars:
        older.append(trimmed.pop(0))

    block = digest(older, max_chars=digest_chars) if older else None
    if block and context_chars:
        room = context_chars - _chars(trimmed)
        if room <= 2:
            block = None
        elif len(block) > room:
            block = digest(older, max_chars=room)
            if block is None and room > len(SHORT_DIGEST_HEADER) + 2:
                block = digest(older, header=SHORT_DIGEST_HEADER, max_chars=room)
    messages: list[dict] = []
    if block:
        messages.append({"role": "system", "content": block})
    messages.extend(trimmed)
    return messages


def prior_block(
    turns: Optional[Iterable],
    *,
    limit: int = PRIOR_TURNS,
    max_chars: int = PRIOR_CHARS,
    header: str = PRIOR_HEADER,
) -> Optional[str]:
    """A short recall block for *earlier* cases with the same account."""
    history = normalise(turns)
    if not history:
        return None
    history = history[-max(1, int(limit)):]
    return digest(history, header=header, max_chars=max_chars)


def _chars(messages: Iterable[dict]) -> int:
    return sum(len(message.get("content", "")) for message in messages)
