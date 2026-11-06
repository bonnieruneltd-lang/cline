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
"""
from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger("royalcourt.gilbert")

_DEFAULT_MODEL = "deepseek-chat"
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


class GilbertAI:
    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """One chat-completions client.

        The keyword overrides let a caller (the daily guide) run on a different
        model, temperature and token budget than the Albert/Gilbert personas
        while sharing every other setting. ``None`` means "read the env".
        """
        self.base_url = _cfg("API_URL", "https://api.deepseek.com").rstrip("/")
        if self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url[: -len("/chat/completions")]
        self.api_key = _cfg("API_KEY", "")
        self.model = (model or _cfg("MODEL", _DEFAULT_MODEL) or _DEFAULT_MODEL).strip()
        if "api.deepseek.com" in self.base_url:
            alias = _DEEPSEEK_MODEL_ALIASES.get(self.model.lower())
            if alias and alias != self.model:
                logger.warning(
                    "AI_MODEL=%s is a legacy model name; using %s instead.",
                    self.model,
                    alias,
                )
                self.model = alias
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

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def complete(self, messages: list[dict]) -> str | None:
        """Return Gilbert's reply, or ``None`` if the model is unavailable."""
        if not self.enabled:
            logger.warning("AI_API_KEY is not set; Gilbert cannot generate a reply.")
            return None

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        logger.error(
                            "Gilbert AI returned HTTP %s (model=%s, url=%s/chat/completions): %s",
                            resp.status,
                            self.model,
                            self.base_url,
                            body,
                        )
                        return None
                    data = await resp.json()
            content = reply_text(data)
            if content is None:
                logger.error(
                    "Gilbert AI returned no message text (model=%s, keys=%s)",
                    self.model,
                    sorted(data)[:6] if isinstance(data, dict) else type(data).__name__,
                )
            return content
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("Gilbert AI request failed: %s", exc)
            return None
