"""
gilbert/ai.py
=============
Async client for an OpenAI-compatible chat-completions endpoint.

Works with OpenAI, Azure OpenAI, OpenRouter, Ollama, LM Studio, vLLM and any
other service exposing ``POST {AI_API_URL}/chat/completions``.
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


class GilbertAI:
    def __init__(self) -> None:
        self.base_url = os.getenv("AI_API_URL", "https://api.deepseek.com").strip().rstrip("/")
        if self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url[: -len("/chat/completions")]
        self.api_key = os.getenv("AI_API_KEY", "").strip()
        self.model = os.getenv("AI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
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
            self.timeout = float(os.getenv("AI_TIMEOUT", "60"))
        except ValueError:
            self.timeout = 60.0
        try:
            self.temperature = float(os.getenv("AI_TEMPERATURE", "0.6"))
        except ValueError:
            self.temperature = 0.6
        try:
            self.max_tokens = int(os.getenv("AI_MAX_TOKENS", "900"))
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
            content = data["choices"][0]["message"]["content"]
            return content.strip() if content else None
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("Gilbert AI request failed: %s", exc)
            return None
