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

Backup provider
---------------
A second endpoint can be configured with ``CHATBOT_FALLBACK_API_URL``,
``CHATBOT_FALLBACK_API_KEY`` and ``CHATBOT_FALLBACK_MODEL``. Whenever the
primary endpoint produces no reply - a revoked key (401/403), an empty balance
(402), a rate limit (429), a 5xx, a timeout, or the failure breaker being open -
the same call is retried once against the fallback. That is what keeps the
personas answering when a free-tier key runs dry, instead of silently posting
the canned fallback line for the rest of the day.
"""
from __future__ import annotations

import asyncio
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
    """One OpenAI-compatible endpoint: where to POST, with which key/model."""

    base_url: str
    api_key: str
    model: str


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


def _fallback_provider(primary_model: str) -> _Provider | None:
    """The backup endpoint from ``CHATBOT_FALLBACK_*``, or ``None``.

    Both a URL and a key are required; an empty model means "same model as the
    primary", which is only useful when the two share a provider.
    """
    base_url = _normalise_url(_cfg("FALLBACK_API_URL", ""))
    api_key = _cfg("FALLBACK_API_KEY", "")
    if not base_url or not api_key:
        return None
    model = (_cfg("FALLBACK_MODEL", "") or primary_model).strip()
    return _Provider(base_url, api_key, _apply_deepseek_alias(base_url, model))


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
    if status == 404 or "model not found" in text:
        return "check CHATBOT_MODEL against the provider's model list"
    if status == 429 or "rate limit" in text:
        return "rate limited; retry later or slow the caller down"
    if "empty response content" in text:
        return (
            "the model returned no text - reasoning models spend max_tokens "
            "before they answer, so raise CHATBOT_MAX_TOKENS/DAILY_GUIDE_MAX_TOKENS"
        )
    return ""


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
    r"(?:\bI\s*(?:'m|’m|am)\s+(?:an?\s+)?(?:[A-Za-z-]+\s+){0,2}"
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
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        fallback_model: str | None = None,
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
        """
        self.base_url = _normalise_url(api_url or _cfg("API_URL", DEFAULT_BASE_URL))
        self.api_key = (api_key if api_key is not None else _cfg("API_KEY", "")).strip()
        self.model = (model or _cfg("MODEL", _DEFAULT_MODEL) or _DEFAULT_MODEL).strip()
        self.model = _apply_deepseek_alias(self.base_url, self.model)
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

        fallback = _fallback_provider(self.model)
        if fallback is not None and (fallback_model or "").strip():
            fallback = fallback._replace(model=(fallback_model or "").strip())
        if fallback is not None and fallback == _Provider(
            self.base_url, self.api_key, self.model
        ):
            # Same endpoint, key and model: retrying it once more buys nothing.
            fallback = None
        self.fallback = fallback

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self.fallback is not None

    async def complete(self, messages: list[dict]) -> str | None:
        """Return the reply, or ``None`` if neither provider could answer.

        The primary provider is tried first. If it yields nothing - a dead key,
        an empty balance, a rate limit, an outage or an open breaker - the
        configured fallback gets one attempt, because posting the canned
        persona line while a working key sits unused is the bug this covers.
        """
        if self.api_key:
            reply = await self._complete_with(
                self.base_url, self.api_key, self.model, messages
            )
            if reply is not None or self.fallback is None:
                return reply
            logger.warning(
                "Primary AI endpoint %s (%s) gave no reply; retrying on the "
                "fallback %s (%s)",
                self.base_url,
                self.model,
                self.fallback.base_url,
                self.fallback.model,
            )
        elif self.fallback is not None:
            logger.warning(
                "CHATBOT_API_KEY is not set; using the fallback endpoint %s (%s)",
                self.fallback.base_url,
                self.fallback.model,
            )
        else:
            logger.warning("AI_API_KEY is not set; Gilbert cannot generate a reply.")
            return None

        return await self._complete_with(
            self.fallback.base_url,
            self.fallback.api_key,
            self.fallback.model,
            messages,
        )

    async def _complete_with(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
    ) -> str | None:
        """One provider's retry ladder. ``None`` means "this provider is out"."""
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
                        if resp.status not in RETRY_STATUSES or attempt + 1 >= attempts:
                            # 429 means the provider is healthy but busy: that is
                            # not an outage, so it never opens the breaker.
                            if resp.status != 429:
                                breaker_note_failure(breaker)
                            return None
                        delay = _retry_delay(resp, attempt)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
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
                    logger.exception("Gilbert AI request failed: %s", exc)
                    breaker_note_failure(breaker)
                    return None
                # Back off, but never past the overall budget.
                await asyncio.sleep(max(0.0, min(delay, deadline - time.monotonic())))
            return None

