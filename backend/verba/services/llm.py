"""OpenAI-compatible chat client — one codepath for remote APIs and local servers.

Works against any endpoint that speaks the OpenAI chat-completions protocol
(OpenAI, llama.cpp `llama-server`, Ollama, LM Studio, vLLM, ...). In "local"
mode the managed llama.cpp server is started on demand and used as endpoint.

Status changes are broadcast as "engine.status" events (engine "llm") so
the AI status monitor in the UI stays live.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import config
from ..events import hub

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 300.0  # local models on CPU can be slow
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


def llm_location(settings: config.Settings | None = None) -> str:
    """Where LLM work runs: "none", "remote" or "local".

    Local means the model shares this machine's resources with Whisper —
    the scheduler then batches phases instead of running lanes in parallel.
    """
    settings = settings or config.get_settings()
    mode = settings.llm.mode
    if mode == "none":
        return "none"
    if mode == "local":
        return "local"
    host = (urlparse(settings.llm.base_url).hostname or "").lower()
    return "local" if host in LOCAL_HOSTS else "remote"


def _publish_status(state: str, detail: str = "") -> None:
    hub.publish("engine.status", {"engine": "llm", "state": state, "detail": detail})


def _resolve_endpoint(settings: config.Settings) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for the configured mode."""
    if settings.llm.mode == "openai":
        if not settings.llm.base_url:
            raise LLMNotConfigured("LLM endpoint (base URL) is not configured")
        return settings.llm.base_url.rstrip("/"), settings.llm.api_key, settings.llm.model
    if settings.llm.mode == "local":
        from . import llamacpp

        base_url = llamacpp.ensure_running()
        return base_url.rstrip("/"), "", settings.llm.model or llamacpp.active_model_name()
    raise LLMNotConfigured("No LLM configured (mode: none)")


def chat(
    messages: list[dict[str, str]],
    model_override: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """One chat completion with retries; returns the assistant message text."""
    settings = config.get_settings()
    base_url, api_key, model = _resolve_endpoint(settings)
    if model_override:
        model = model_override
    if not model:
        raise LLMNotConfigured("No LLM model configured")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _publish_status("busy", f"{model} (attempt {attempt}/{MAX_ATTEMPTS})")
            response = httpx.post(
                f"{base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            _publish_status("idle")
            return content if isinstance(content, str) else str(content)
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            last_error = exc
            logger.warning("LLM call failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S * attempt)

    _publish_status("error", str(last_error))
    raise LLMError(f"LLM call failed permanently: {last_error}") from last_error


def probe(base_url: str, api_key: str = "") -> dict[str, Any]:
    """Check an OpenAI-compatible endpoint and list its models (settings UI)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models", headers=headers, timeout=10, follow_redirects=True
        )
        response.raise_for_status()
        data = response.json()
        models = [entry.get("id", "") for entry in data.get("data", [])]
        return {"ok": True, "models": [m for m in models if m]}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "models": []}
