"""
gilbert/ai.py
=============
Async client for an OpenAI-compatible chat-completions endpoint.

Works with OpenAI, Azure OpenAI, OpenRouter, Ollama, LM Studio, vLLM and any
other service exposing ``POST {CHATBOT_API_URL}/chat/completions``.

Shared chat-bot configuration
-----------------------------
Gilbert (Workers' Union) and Albert (The Crown) both talk through this one
client, so the settings are the **chat-bot** config: ``CHATBOT_API_KEY``,
``CHATBOT_API_URL``, ``CHATBOT_MODEL``, ``CHATBOT_TIMEOUT``,
``CHATBOT_TEMPERATURE`` and ``CHATBOT_MAX_TOKENS``. The old ``AI_*`` names are
still read as a fallback so an existing ``.env`` keeps working during migration.

Failover chain
--------------
More than one key can be configured. The primary endpoint uses
``CHATBOT_API_URL`` / ``CHATBOT_API_KEY`` / ``CHATBOT_MODEL``; backups use
``CHATBOT_FALLBACK_API_URL`` / ``_API_KEY`` / ``_MODEL`` (tier 1) and
``CHATBOT_FALLBACK_2_*`` ... ``CHATBOT_FALLBACK_9_*`` for further tiers. A
comma-separated ``*_MODELS`` list on any tier is tried model by model before the
next key is used. Whenever an endpoint produces no reply - a revoked key
(401/403), an empty balance (402), a rate limit (429), a 5xx, a timeout, or the
failure breaker being open - the same call moves to the next key. A model the
gateway rejects ("model not supported"), or one that returns no text at all,
moves to the next model on the same key. That is what keeps the personas
answering when a free-tier key runs dry, instead of silently posting the canned
fallback line for the rest of the day.

Free models
-----------
The Cline gateway marks its free models with a ``:free`` suffix.
``FREE_CLINE_MODELS`` lists the ones measured on the real persona prompt, and
they are appended as last-resort models whenever the configured endpoint is
that gateway (``CHATBOT_FREE_FALLBACKS=false`` turns this off). The daily guide
uses the same list for its own long-form summary call.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import random
import re
import time
from typing import NamedTuple

import aiohttp

logger = logging.getLogger("royalcourt.gilbert")

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
# A new ``aiohttp.ClientSession`` per reply meant a fresh connector and TCP/TLS
# handshake for every Gilbert or Albert turn. During a negotiation burst (dozens
# of threads talking at once) that churns sockets and file descriptors. One
# session per event loop is reused for every call and closed on shutdown.
#
# The sessions/semaphores are keyed by loop id instead of held in a module
# global so two ``asyncio.run`` cycles in one process (the checks do this) never
# share a loop-bound object.
_SESSIONS: dict[int, aiohttp.ClientSession] = {}
_SEMAPHORES: dict[int, asyncio.Semaphore] = {}

# Cap simultaneous provider calls. The provider rate-limits anyway; queueing
# here keeps a 20-thread burst from opening 20 sockets and getting 429s.
DEFAULT_MAX_CONCURRENCY = 8
MAX_MAX_CONCURRENCY = 64
# Statuses worth one more try. 429 is the rate limiter, 5xx is transient.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_BASE_SECONDS = 0.75
RETRY_MAX_SECONDS = 5.0

# How many backup keys may be configured: CHATBOT_FALLBACK_* plus
# CHATBOT_FALLBACK_2_* through CHATBOT_FALLBACK_9_*.
MAX_FALLBACK_TIERS = 9

# Statuses that mean "this key is not allowed to run this model", not "the key
# is dead": the next model on the same key is worth a try.
MODEL_REJECT_STATUSES = frozenset({400, 404, 422})

# The Cline gateway (https://api.cline.bot/api/v1) labels free models with a
# ``:free`` suffix. These six were exercised on the real Gilbert system prompt
# plus a private-mansion case (1/1 contracts used, 400k Union treasury against a
# 1M budget) and each returned the correct ruling; the comment carries the
# measured latency. The list is the last-resort model pool on that gateway, so a
# free key keeps the personas answering after a paid model or key fails.
FREE_CLINE_MODELS = (
    "dots-studio/dots-3-note-preview:free",      # 6.2 s
    "baidu/ernie-5.5-flash-preview:free",        # 8.5 s
    "nvidia/nemotron-3.5-super-vl:free",         # 10.8 s
    "nvidia/nemotron-3-ultra-550b-a55b:free",    # 20.3 s, best prose
    "recursive/flint-v1.0:free",                 # short but in character
    "openrouter/free",                           # gateway's own free router
)

# Why the last ``_complete_with`` returned ``None``. A context variable keeps
# the reason local to the calling task, so twenty simultaneous negotiations
# never read each other's failure. "model" (rejected) and "empty" (no text) are
# the reasons that justify another model on the same key; everything else moves
# to the next key.
_LAST_FAILURE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ai_last_failure", default=""
)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int((os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(value, high))


def _max_concurrency() -> int:
    return _env_int(
        "CHATBOT_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY, 1, MAX_MAX_CONCURRENCY
    )


def _max_attempts() -> int:
    # One first try plus ``CHATBOT_RETRIES`` retries.
    return 1 + _env_int("CHATBOT_RETRIES", 2, 0, 4)


def _session() -> aiohttp.ClientSession:
    """The process' shared client for the running loop (created on first use)."""
    key = id(asyncio.get_running_loop())
    session = _SESSIONS.get(key)
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=32, limit_per_host=16, ttl_dns_cache=300
            )
        )
        _SESSIONS[key] = session
    return session


def _semaphore() -> asyncio.Semaphore:
    key = id(asyncio.get_running_loop())
    sem = _SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_max_concurrency())
        _SEMAPHORES[key] = sem
    return sem


def shared_session() -> aiohttp.ClientSession:
    """Public accessor for the shared client (the Tavily lookup reuses it)."""
    return _session()


async def close_shared_sessions() -> None:
    """Close every shared client. Called from ``RoyalCourt.close``."""
    sessions = list(_SESSIONS.values())
    _SESSIONS.clear()
    _SEMAPHORES.clear()
    reset_breakers()
    for session in sessions:
        try:
            if not session.closed:
                await session.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass


def _retry_delay(response: aiohttp.ClientResponse | None, attempt: int) -> float:
    """Retry-After when the provider sent one, else exponential backoff.

    The exponential branch is jittered: twenty negotiations retried in lockstep
    keep colliding on the same attempt, which is exactly what the rate limiter
    punishes. Spreading them over ±25% turns a thundering herd into a trickle.
    """
    if response is not None:
        header = (response.headers.get("Retry-After") or "").strip()
        if header:
            try:
                return min(max(float(header), 0.0), RETRY_MAX_SECONDS)
            except ValueError:
                pass
    base = min(RETRY_BASE_SECONDS * (2 ** attempt), RETRY_MAX_SECONDS)
    return min(base * random.uniform(0.75, 1.25), RETRY_MAX_SECONDS)


# ---------------------------------------------------------------------------
# Failure breaker
# ---------------------------------------------------------------------------
# When the endpoint is down, misconfigured (401/402/404) or unreachable, every
# waiting negotiation used to run its own retry ladder: a twenty-thread evening
# turned one outage into sixty doomed requests and twenty slow replies. After
# ``BREAKER_FAILURES`` consecutive failures the endpoint is declared unhealthy
# for ``BREAKER_SECONDS``: calls return ``None`` at once, so the caller posts
# its fallback instantly and the provider is left alone to recover.
BREAKER_FAILURES = 3
BREAKER_SECONDS = 20.0
_FAILURES: dict[tuple[int, str], int] = {}
_OPEN_UNTIL: dict[tuple[int, str], float] = {}


def _breaker_seconds() -> float:
    try:
        value = float((os.getenv("CHATBOT_BREAKER_SECONDS") or "").strip() or BREAKER_SECONDS)
    except ValueError:
        value = BREAKER_SECONDS
    return max(0.0, min(value, 600.0))


def _breaker_key(endpoint: str, model: str) -> tuple[int, str]:
    # Keyed by loop as well: two ``asyncio.run`` cycles (the checks) must not
    # inherit each other's breaker.
    return (id(asyncio.get_running_loop()), f"{endpoint}|{model}")


def breaker_open(key: tuple[int, str]) -> bool:
    until = _OPEN_UNTIL.get(key)
    if until is None:
        return False
    if time.monotonic() < until:
        return True
    _OPEN_UNTIL.pop(key, None)
    _FAILURES.pop(key, None)
    return False


def breaker_note_success(key: tuple[int, str]) -> None:
    _FAILURES.pop(key, None)
    _OPEN_UNTIL.pop(key, None)


def breaker_note_failure(key: tuple[int, str]) -> None:
    failures = _FAILURES.get(key, 0) + 1
    _FAILURES[key] = failures
    seconds = _breaker_seconds()
    if seconds and failures >= BREAKER_FAILURES:
        _OPEN_UNTIL[key] = time.monotonic() + seconds
        logger.warning(
            "AI endpoint paused for %.0fs after %d consecutive failures (%s)",
            seconds, failures, key[1],
        )


def reset_breakers() -> None:
    """Forget every breaker (shutdown and tests)."""
    _FAILURES.clear()
    _OPEN_UNTIL.clear()


def _log_usage(payload) -> None:
    """One debug line per reply: prompt/completion tokens, when reported.

    Cheap observability for prompt bloat: a long negotiation whose prompt
    tokens creep up every turn is the signal that the memory window needs
    tuning, and this is the only place that number is visible.
    """
    if not logger.isEnabledFor(logging.DEBUG) or not isinstance(payload, dict):
        return
    node = payload
    for _ in range(3):
        usage = node.get("usage") if isinstance(node, dict) else None
        if isinstance(usage, dict):
            logger.debug(
                "AI usage: prompt=%s completion=%s total=%s",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
            )
            return
        node = node.get("data") if isinstance(node, dict) else None


_DEFAULT_MODEL = "deepseek-chat"
# Only used when neither ``CHATBOT_API_URL`` nor ``AI_API_URL`` is set. The live
# deployment always sets its provider explicitly.
DEFAULT_BASE_URL = "https://api.deepseek.com"
# Legacy deployments used "deepseek-flash". The current endpoint accepts it, but
# deepseek-chat is the portable default for DeepSeek-compatible services.
_DEEPSEEK_MODEL_ALIASES = {"deepseek-flash": _DEFAULT_MODEL, "deepseek": _DEFAULT_MODEL}

# Fields that moved from AI_* to CHATBOT_*. Documented here so the migration is
# visible; the fallback itself lives in ``_cfg``.
_RENAMED = {
    "AI_API_URL": "CHATBOT_API_URL",
    "AI_API_KEY": "CHATBOT_API_KEY",
    "AI_MODEL": "CHATBOT_MODEL",
    "AI_TIMEOUT": "CHATBOT_TIMEOUT",
    "AI_TEMPERATURE": "CHATBOT_TEMPERATURE",
    "AI_MAX_TOKENS": "CHATBOT_MAX_TOKENS",
}
_LEGACY_WARNED: set[str] = set()


def _cfg(name: str, default: str = "") -> str:
    """Read ``CHATBOT_<name>``, falling back to the legacy ``AI_<name>``."""
    new = os.getenv(f"CHATBOT_{name}")
    if new is not None and new.strip():
        return new.strip()
    old = os.getenv(f"AI_{name}")
    if old is not None and old.strip():
        if name not in _LEGACY_WARNED:
            _LEGACY_WARNED.add(name)
            logger.warning(
                "AI_%s is deprecated; use CHATBOT_%s (chat-bot config).", name, name
            )
        return old.strip()
    return default


class _Provider(NamedTuple):
    """One OpenAI-compatible endpoint: where to POST, with which key/models.

    ``models`` is the ordered candidate list for this key; ``model`` is the
    first entry and is kept as its own field because callers and the docs talk
    about "the model" on a tier. An empty ``models`` tuple means ``model`` only.
    """

    base_url: str
    api_key: str
    model: str
    models: tuple[str, ...] = ()

    @property
    def candidates(self) -> tuple[str, ...]:
        return self.models or ((self.model,) if self.model else ())


def _split_models(value: str) -> tuple[str, ...]:
    """A comma-separated model list from the env, empty entries dropped."""
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _candidate_models(*groups) -> tuple[str, ...]:
    """Merge model groups into one order-preserving, duplicate-free tuple.

    Each group may be a string or an iterable of strings. The first name is the
    tier's primary model; the rest are tried when the gateway rejects that model
    (``model not supported``) without blaming the key.
    """
    ordered: list[str] = []
    for group in groups:
        if isinstance(group, str):
            group = (group,)
        for name in group or ():
            name = (name or "").strip()
            if name and name not in ordered:
                ordered.append(name)
    return tuple(ordered)


def free_models_for(base_url: str) -> tuple[str, ...]:
    """The free ``:free`` model pool for this endpoint, or ``()``.

    Only the Cline gateway publishes that suffix; sending such a name to
    DeepSeek or Google would just be a 404, so the list is host-scoped.
    """
    if "api.cline.bot" in (base_url or "").lower():
        return FREE_CLINE_MODELS
    return ()


def _default_model_for(base_url: str) -> str:
    """``deepseek-chat`` everywhere except the Cline gateway (a free model)."""
    if "api.cline.bot" in (base_url or "").lower():
        return FREE_CLINE_MODELS[0]
    return _DEFAULT_MODEL


def _free_fallbacks_enabled(name: str = "CHATBOT_FREE_FALLBACKS") -> bool:
    """Whether the ``:free`` pool is appended (default on)."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return True
    return value not in {"0", "false", "no", "off"}


def _legacy_fallback_enabled() -> bool:
    """Whether a populated legacy ``AI_*`` key joins the chain (default on)."""
    value = (os.getenv("CHATBOT_LEGACY_FALLBACK") or "").strip().lower()
    if not value:
        return True
    return value not in {"0", "false", "no", "off"}


def _legacy_provider(
    primary_url: str, primary_key: str, *, free_fallbacks: bool
) -> _Provider | None:
    """The old ``AI_*`` endpoint as a last-resort tier, or ``None``.

    Before the ``CHATBOT_*`` rename the personas ran on ``AI_API_URL`` /
    ``AI_API_KEY`` / ``AI_MODEL``. When the new primary is configured those old
    values are otherwise unreachable, so switching providers would silently
    throw away a working key. They are appended after every explicit
    ``CHATBOT_FALLBACK_*`` tier instead - "keep the other keys as backups".
    Same endpoint and key as the primary is not duplicated.
    """
    if not _legacy_fallback_enabled():
        return None
    if not (os.getenv("CHATBOT_API_URL") or os.getenv("CHATBOT_API_KEY")):
        return None
    url = _normalise_url(os.getenv("AI_API_URL") or DEFAULT_BASE_URL)
    key = (os.getenv("AI_API_KEY") or "").strip()
    if not key or (url == primary_url and key == primary_key):
        return None
    model = (os.getenv("AI_MODEL") or "").strip() or _default_model_for(url)
    models = _candidate_models(
        _apply_deepseek_alias(url, model),
        free_models_for(url) if free_fallbacks else (),
    )
    return _Provider(url, key, models[0], models)


def _normalise_url(url: str) -> str:
    """``https://host/v1/`` and ``.../chat/completions`` both become ``.../v1``."""
    base = (url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    return base


def _apply_deepseek_alias(base_url: str, model: str) -> str:
    """Map the retired ``deepseek-flash`` name, but only on the DeepSeek host.

    Other gateways may legitimately serve their own ``deepseek-flash``; only
    ``api.deepseek.com`` renamed it to ``deepseek-chat``.
    """
    if "api.deepseek.com" in base_url:
        alias = _DEEPSEEK_MODEL_ALIASES.get(model.lower())
        if alias and alias != model:
            logger.warning(
                "AI_MODEL=%s is a legacy model name; using %s instead.", model, alias
            )
            return alias
    return model


def _fallback_providers(
    primary_model: str,
    *,
    primary_url: str = "",
    fallback_model: str = "",
    fallback_models: tuple[str, ...] | list[str] | None = None,
    free_fallbacks: bool = True,
) -> tuple[_Provider, ...]:
    """The ordered backup tiers from ``CHATBOT_FALLBACK_*``.

    Tier 1 keeps the original names; tiers 2-9 insert the tier number before
    the field name (``CHATBOT_FALLBACK_2_API_URL``). Both a URL and a key are
    required for a tier to exist. An empty model falls back to the primary
    model, which is only useful when the two tiers share a provider.

    ``fallback_model`` overrides tier 1's model for a single caller (the daily
    guide), and ``free_fallbacks`` appends the gateway's ``:free`` pool to every
    Cline tier.
    """
    providers: list[_Provider] = []
    extra = _candidate_models(fallback_models or ())
    for tier in range(1, MAX_FALLBACK_TIERS + 1):
        prefix = "FALLBACK_" if tier == 1 else f"FALLBACK_{tier}_"
        base_url = _normalise_url(_cfg(prefix + "API_URL", ""))
        api_key = _cfg(prefix + "API_KEY", "")
        if not base_url or not api_key:
            continue
        configured = (_cfg(prefix + "MODEL", "") or "").strip()
        if not configured:
            # Sharing a provider (and therefore its model names) is common; a
            # different provider needs its own default, not a Cline model name.
            configured = (
                primary_model
                if not primary_url or base_url == _normalise_url(primary_url)
                else _default_model_for(base_url)
            )
        models = _candidate_models(
            (fallback_model,) if tier == 1 and fallback_model else (),
            configured,
            _split_models(_cfg(prefix + "MODELS", "")),
            extra if tier == 1 else (),
            free_models_for(base_url) if free_fallbacks else (),
        )
        models = tuple(_apply_deepseek_alias(base_url, name) for name in models)
        providers.append(
            _Provider(base_url, api_key, models[0] if models else configured, models)
        )
    return tuple(providers)


def _fallback_provider(primary_model: str) -> _Provider | None:
    """The first backup tier, kept for callers that still expect one."""
    providers = _fallback_providers(primary_model)
    return providers[0] if providers else None


def provider_hint(status: int, body: str) -> str:
    """A one-line diagnosis for the common provider failures.

    Returns ``""`` when the response needs no explanation. The empty-content case
    matters most: a reasoning model that spends its whole ``max_tokens`` budget
    thinking returns ``200``/``500`` with no text at all, which looks like an
    outage but is really "give it more room to answer".
    """
    text = (body or "").lower()
    if status in (401, 403):
        return "check CHATBOT_API_KEY (or CHATBOT_FALLBACK_API_KEY)"
    if status == 402 or "insufficient_credits" in text:
        return "the provider account is out of credit"
    if status in MODEL_REJECT_STATUSES or "model not found" in text:
        if status == 404 or any(
            marker in text
            for marker in ("model not found", "model not supported", "invalid model")
        ):
            return "check CHATBOT_MODEL against the provider's model list"
    if status == 429 or "rate limit" in text:
        return "rate limited; retry later or slow the caller down"
    if "empty response content" in text:
        return (
            "the model returned no text - reasoning models spend max_tokens "
            "before they answer, so raise CHATBOT_MAX_TOKENS/DAILY_GUIDE_MAX_TOKENS"
        )
    return ""


def _failure_reason(status: int, body: str) -> str:
    """Classify a failed provider response for the failover chain."""
    text = (body or "").lower()
    model_markers = (
        "model not found",
        "model not supported",
        "invalid model",
        "model not exist",
        "does not exist",
        "unknown model",
    )
    if status in MODEL_REJECT_STATUSES and (
        status == 404 or not text or any(marker in text for marker in model_markers)
    ):
        return "model"
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "credit"
    if status == 429:
        return "rate"
    if status >= 500:
        return "server"
    return "http"


def reply_text(payload) -> str | None:
    """Pull the assistant text out of an OpenAI-compatible response.

    Most providers answer ``{"choices": [{"message": {"content": ...}}]}``, but
    the Cline gateway wraps that whole object one level deeper as
    ``{"data": {"choices": [...]}}`` and some return ``content`` as a list of
    parts. Both shapes are accepted here, so swapping the endpoint cannot
    silently produce an empty reply.
    """
    node = payload
    for _ in range(3):
        if isinstance(node, dict) and "choices" in node:
            break
        node = node.get("data") if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return None

    choices = node.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if content is None:
        # Older/plain-completion providers put the text on the choice itself.
        content = first.get("text")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str):
        return None
    return content.strip() or None


# A reply that opens with the speaker's own name reads like a script excerpt:
# "Gilbert: Right then, ..." posted under Gilbert's own message. Models slip into
# this when the history shows "user: ..." lines, so the label is removed. Only
# the personas' own names are touched — "Note:" or "Case UN-X-001:" are content.
_ECHOED_LABEL = re.compile(
    r"^\s*(?:gilbert|albert|the court|the union|assistant|ai)\s*[:\-–—]\s*",
    re.IGNORECASE,
)


# A persona must never reveal the machinery behind it. The hard prompt rule
# makes this rare, but a model can still answer a direct meta-question out of
# role (the live 2026-11-11 turn: "I am Gemini (specifically Gemini 3.6 Flash),
# built by Google!"). These patterns catch the explicit breaks - naming itself
# an AI/language model, or naming a provider, model or version - so the caller
# can retry once and, if it happens again, post an in-character deflection
# instead of leaking it into the negotiation.
_AI_IDENTITY = re.compile(
    r"(?:\bI\s*(?:'m|’m|am)\s+(?!(?:not|no|never)\b)(?:an?\s+)?"
    r"(?:[A-Za-z-]+\s+){0,2}"
    r"(?:AI|A\.I\.|artificial intelligence|bot|program|machine|language model|"
    r"LLM|chatbot|Gemini|DeepSeek|OpenAI|ChatGPT|GPT(?:-?[0-9][0-9.]*)?|"
    r"Claude|Anthropic|LLaMA|Mistral)\b|"
    r"\bas\s+(?:an?\s+)?(?:[A-Za-z-]+\s+){0,2}"
    r"(?:AI|artificial intelligence|language model|LLM|chatbot)\b|"
    r"\b(?:Administrative|Secretariat'?s?)\s+AI\b|"
    r"\b(?:powered|running|built|made|created|trained|developed)\s+(?:by|on)\s+"
    r"(?:Google|OpenAI|Anthropic|DeepSeek|Meta|Microsoft|Mistral|Gemini|"
    r"GPT(?:-?[0-9][0-9.]*)?)\b)",
    re.IGNORECASE,
)
_AI_BUILT_BY = re.compile(
    r"\b(?:built|made|created|trained|developed)\s+by\s+"
    r"(?:Google|OpenAI|Anthropic|DeepSeek|Meta|Microsoft|Mistral)\b",
    re.IGNORECASE,
)


def looks_out_of_character(text: str | None) -> bool:
    """True when a persona reply names the AI/provider instead of staying in role.

    Only a *self-identification* counts: "I am Gemini", "as an AI language
    model", "the Administrative AI", "powered by Google". An in-role denial that
    merely repeats the player's words - "AI? No, just Gilbert", "there is no
    DeepSeek or Gemini here" - is allowed through, so a good answer is not
    replaced by the canned deflection.
    """
    if not text:
        return False
    return bool(_AI_IDENTITY.search(text) or _AI_BUILT_BY.search(text))


def clean_reply(text: str | None, speaker: str = "") -> str | None:
    """Tidy a model reply before it is posted as the persona.

    Removes an echoed ``Gilbert:``/``Albert:`` label, squeezes runs of blank
    lines, and returns ``None`` for an empty result so the caller posts its own
    fallback line instead of an empty message.
    """
    if not text:
        return None
    body = str(text).strip()
    if speaker:
        name = re.escape(speaker.strip())
        body = re.sub(rf"^\s*{name}\s*[:\-–—]\s*", "", body, flags=re.IGNORECASE)
    body = _ECHOED_LABEL.sub("", body, count=1)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body or None


class GilbertAI:
    def __init__(
        self,
        *,
        model: str | None = None,
        models: tuple[str, ...] | list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        fallback_model: str | None = None,
        fallback_models: tuple[str, ...] | list[str] | None = None,
        use_free_fallbacks: bool | None = None,
    ) -> None:
        """One chat-completions client.

        The keyword overrides let a caller (the daily guide) run on a different
        model, temperature and token budget than the Albert/Gilbert personas
        while sharing every other setting. ``None`` means "read the env".

        ``api_url`` / ``api_key`` let that caller also use its own provider (the
        guide runs on a DeepSeek key while the personas run on Google AI Studio),
        and ``fallback_model`` overrides ``CHATBOT_FALLBACK_MODEL`` for that
        caller alone - the guide falls back to the big pro tier, the personas to
        the fast one.

        ``models`` / ``fallback_models`` are ordered candidate lists, so a key
        can offer more than one model before the client moves to another key.
        ``use_free_fallbacks`` controls whether the gateway's ``:free`` pool is
        appended; ``None`` reads ``CHATBOT_FREE_FALLBACKS`` (default on).
        """
        self.base_url = _normalise_url(api_url or _cfg("API_URL", DEFAULT_BASE_URL))
        self.api_key = (api_key if api_key is not None else _cfg("API_KEY", "")).strip()

        supplied = _candidate_models((model,) if model else (), models or ())
        if supplied:
            ordered = supplied
        else:
            ordered = _candidate_models(
                (model,) if model else (),
                _cfg("MODEL", "") or _default_model_for(self.base_url),
                _split_models(_cfg("MODELS", "")),
            )
        if not ordered:
            ordered = (_default_model_for(self.base_url),)
        free = (
            _free_fallbacks_enabled()
            if use_free_fallbacks is None
            else bool(use_free_fallbacks)
        )
        if free:
            ordered = _candidate_models(ordered, free_models_for(self.base_url))
        self.models = tuple(_apply_deepseek_alias(self.base_url, name) for name in ordered)
        self.model = self.models[0]

        try:
            self.timeout = float(timeout) if timeout is not None else float(_cfg("TIMEOUT", "60"))
        except ValueError:
            self.timeout = 60.0
        try:
            self.temperature = (
                float(temperature) if temperature is not None else float(_cfg("TEMPERATURE", "0.6"))
            )
        except ValueError:
            self.temperature = 0.6
        try:
            self.max_tokens = (
                int(max_tokens) if max_tokens is not None else int(_cfg("MAX_TOKENS", "900"))
            )
        except ValueError:
            self.max_tokens = 900

        providers = _fallback_providers(
            self.model,
            primary_url=self.base_url,
            fallback_model=(fallback_model or "").strip(),
            fallback_models=fallback_models,
            free_fallbacks=free,
        )
        # A tier that repeats the primary endpoint, key and model buys nothing.
        primary = (self.base_url, self.api_key, self.model)
        fallbacks = [
            tier for tier in providers if (tier.base_url, tier.api_key, tier.model) != primary
        ]
        # The old AI_* key is still a working provider; keep it as the final
        # backup rather than making the operator duplicate it by hand.
        legacy = _legacy_provider(self.base_url, self.api_key, free_fallbacks=free)
        if legacy is not None and all(
            (tier.base_url, tier.api_key) != (legacy.base_url, legacy.api_key)
            for tier in fallbacks
        ):
            fallbacks.append(legacy)
        self.fallbacks = tuple(fallbacks)
        self.fallback = self.fallbacks[0] if self.fallbacks else None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or bool(self.fallbacks)

    @property
    def chain(self) -> tuple[_Provider, ...]:
        """Primary first, then every configured backup key, in order."""
        primary: tuple[_Provider, ...] = ()
        if self.api_key:
            primary = (_Provider(self.base_url, self.api_key, self.model, self.models),)
        return primary + self.fallbacks

    async def complete(self, messages: list[dict]) -> str | None:
        """Return the reply, or ``None`` when every key and model failed.

        Keys are tried in order (primary, then ``CHATBOT_FALLBACK_*`` tiers),
        and each key's ``*_MODELS`` list is tried in order too. A dead key, an
        empty balance, a rate limit, an outage or an open breaker moves to the
        next key; only a rejected model or a model that returned no text moves
        to the next model on the same key. Posting the canned persona line while
        a working key sits unused is the bug this covers.
        """
        chain = self.chain
        if not chain:
            logger.warning("AI_API_KEY is not set; Gilbert cannot generate a reply.")
            return None
        if not self.api_key:
            logger.warning(
                "CHATBOT_API_KEY is not set; using the fallback endpoint %s (%s)",
                chain[0].base_url,
                chain[0].model,
            )
        for index, provider in enumerate(chain):
            for position, candidate in enumerate(provider.candidates):
                reply = await self._complete_with(
                    provider.base_url, provider.api_key, candidate, messages
                )
                if reply is not None:
                    return reply
                reason = _LAST_FAILURE.get()
                if reason in {"model", "empty"} and position + 1 < len(provider.candidates):
                    logger.warning(
                        "Model %s gave no usable reply on %s (%s); trying %s on the same key",
                        candidate,
                        provider.base_url,
                        reason,
                        provider.candidates[position + 1],
                    )
                    continue
                break
            if index + 1 < len(chain):
                logger.warning(
                    "AI endpoint %s (%s) gave no reply; retrying on %s (%s)",
                    provider.base_url,
                    provider.model,
                    chain[index + 1].base_url,
                    chain[index + 1].model,
                )
        return None

    async def _complete_with(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
    ) -> str | None:
        """One provider's retry ladder. ``None`` means "this provider is out".

        Also records *why* in ``_LAST_FAILURE`` so ``complete()`` can tell a
        rejected model (try the next model on this key) from a rejected key
        (move to the next configured key).
        """
        _LAST_FAILURE.set("")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = f"{base_url}/chat/completions"
        attempts = _max_attempts()
        # One overall budget: a retry only happens when the first try failed
        # quickly (429/5xx). A request that burns the whole timeout is not
        # repeated, which keeps the daily guide from doubling a long call.
        deadline = time.monotonic() + self.timeout
        breaker = _breaker_key(base_url, model)
        if breaker_open(breaker):
            logger.warning(
                "AI endpoint %s is paused after repeated failures; skipping this call",
                base_url,
            )
            _LAST_FAILURE.set("breaker")
            return None

        # Queue behind the shared cap: the provider is the bottleneck, and
        # firing 20 requests at once only converts them into 429s.
        async with _semaphore():
            for attempt in range(attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout = aiohttp.ClientTimeout(total=min(self.timeout, remaining))
                try:
                    async with _session().post(
                        url, json=payload, headers=headers, timeout=timeout
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            content = reply_text(data)
                            if content is None:
                                logger.error(
                                    "Gilbert AI returned no message text (model=%s, keys=%s)",
                                    model,
                                    sorted(data)[:6]
                                    if isinstance(data, dict)
                                    else type(data).__name__,
                                )
                                breaker_note_failure(breaker)
                                _LAST_FAILURE.set("empty")
                            else:
                                breaker_note_success(breaker)
                                _log_usage(data)
                            return content
                        body = (await resp.text())[:300]
                        hint = provider_hint(resp.status, body)
                        logger.error(
                            "Gilbert AI returned HTTP %s (model=%s, url=%s): %s%s",
                            resp.status,
                            model,
                            url,
                            body,
                            f" [hint: {hint}]" if hint else "",
                        )
                        reason = _failure_reason(resp.status, body)
                        _LAST_FAILURE.set(reason)
                        if reason == "model":
                            # The key works; this model does not. Do not open
                            # the breaker for a configuration mistake.
                            return None
                        if resp.status not in RETRY_STATUSES or attempt + 1 >= attempts:
                            # 429 means the provider is healthy but busy: that is
                            # not an outage, so it never opens the breaker.
                            if resp.status != 429:
                                breaker_note_failure(breaker)
                            return None
                        delay = _retry_delay(resp, attempt)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    _LAST_FAILURE.set("timeout")
                    logger.warning(
                        "Gilbert AI request failed (attempt %s/%s): %s",
                        attempt + 1,
                        attempts,
                        exc,
                    )
                    if attempt + 1 >= attempts:
                        logger.exception("Gilbert AI request failed: %s", exc)
                        breaker_note_failure(breaker)
                        return None
                    delay = _retry_delay(None, attempt)
                except Exception as exc:  # pragma: no cover - unexpected shape
                    _LAST_FAILURE.set("error")
                    logger.exception("Gilbert AI request failed: %s", exc)
                    breaker_note_failure(breaker)
                    return None
                # Back off, but never past the overall budget.
                await asyncio.sleep(max(0.0, min(delay, deadline - time.monotonic())))
            if not _LAST_FAILURE.get():
                _LAST_FAILURE.set("timeout")
            return None

