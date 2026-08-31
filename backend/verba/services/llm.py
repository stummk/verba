"""OpenAI-compatible chat client — one codepath for remote APIs and local servers.

Works against any endpoint that speaks the OpenAI chat-completions protocol
(OpenAI, llama.cpp `llama-server`, Ollama, LM Studio, vLLM, ...). In "local"
mode the managed llama.cpp server is started on demand and used as endpoint.

Status changes are broadcast as "engine.status" events (engine "llm") so
the AI status monitor in the UI stays live.
"""

from __future__ import annotations

import logging
import re
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
ERROR_BODY_CHARS = 400  # LM Studio & co. explain a refusal in the response body

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}

# A reasoning model puts its chain of thought in front of the answer. Some
# servers hand it over unchanged, others strip only the opening tag — and an
# unterminated block means the answer was cut off mid-thought.
_THINK_TAG = r"think|thinking|reason|reasoning"
_THINK_BLOCK_RE = re.compile(rf"<({_THINK_TAG})\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_THINK_TAIL_RE = re.compile(rf"<({_THINK_TAG})\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
_THINK_HEAD_RE = re.compile(rf"\A.*</({_THINK_TAG})\s*>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class EmptyAnswer(LLMError):
    """The endpoint answered, but without usable text.

    Either the model produced reasoning only, or the token budget ran out
    before the answer began. Callers must never store this as a result — an
    empty cleanup text silently empties every PDF built from it afterwards.
    """


class TruncatedAnswer(LLMError):
    """The answer stopped at the token limit instead of at its end.

    For a cleanup or a translation that means a piece of the transcript is
    missing, which must never end up stored as the result. Not retried in
    `chat()` — the same request would be cut off again; the caller splits its
    input instead (`pipeline.chat_pieces`).
    """

    def __init__(self, text: str) -> None:
        super().__init__(
            "Die Antwort des Modells brach ab, bevor der Abschnitt vollständig war — "
            "selbst ein kurzer Abschnitt passt nicht in sein Token-Budget. Bitte ein "
            "Modell mit größerem Kontextfenster verwenden."
        )
        self.text = text  # the partial answer, for logging and diagnosis


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


def strip_reasoning(text: str) -> str:
    """Drop a reasoning model's thinking and keep the answer."""
    text = _THINK_BLOCK_RE.sub("", text)
    if _THINK_HEAD_RE.search(text):  # closing tag without an opener
        text = _THINK_HEAD_RE.sub("", text)
    text = _THINK_TAIL_RE.sub("", text)  # opener without a closer: nothing usable follows
    return text.strip()


def _message_content(message: dict[str, Any]) -> str:
    """The message text, also for servers that answer in content parts."""
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def _empty_answer_reason(message: dict[str, Any], raw: str, finish_reason: str) -> str:
    """Why an answer carries no text — shown verbatim in the web UI (German)."""
    thinking = bool(raw.strip()) or bool(message.get("reasoning_content"))
    if finish_reason == "length":
        return (
            "Das Modell hat sein Token-Budget aufgebraucht, bevor eine Antwort begann"
            + (" — es hat nur nachgedacht." if thinking else ".")
            + " Bitte ein Modell ohne Reasoning verwenden oder dessen Denkmodus abschalten."
        )
    if thinking:
        return (
            "Das Modell hat nur interne Überlegungen geliefert, keinen Antworttext. "
            "Bitte ein Modell ohne Reasoning verwenden oder dessen Denkmodus abschalten."
        )
    return "Das Modell hat eine leere Antwort geliefert."


def _error_detail(exc: Exception) -> str:
    """Error text for the UI — with the server's own explanation if it sent one."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = " ".join(exc.response.text.split())
        status = exc.response.status_code
        return f"HTTP {status}: {body[:ERROR_BODY_CHARS]}" if body else f"HTTP {status}"
    if isinstance(exc, EmptyAnswer):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _publish_status(state: str, detail: str = "") -> None:
    hub.publish("engine.status", {"engine": "llm", "state": state, "detail": detail})


def _resolve_endpoint(settings: config.Settings) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for the configured mode."""
    if settings.llm.mode == "openai":
        if not settings.llm.base_url:
            raise LLMNotConfigured("LLM endpoint (base URL) is not configured")
        return settings.llm.base_url.rstrip("/"), settings.llm.api_key, settings.llm.model
    if settings.llm.mode == "local":
        from . import hardware, llamacpp

        try:
            base_url = llamacpp.ensure_running()
        except hardware.InsufficientMemory as exc:
            # not a bug but a machine limit: the caller shows this verbatim
            raise LLMError(str(exc)) from exc
        return base_url.rstrip("/"), "", settings.llm.model or llamacpp.active_model_name()
    raise LLMNotConfigured("No LLM configured (mode: none)")


def chat(
    messages: list[dict[str, str]],
    model_override: str = "",
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """One chat completion with retries; returns the assistant message text.

    `max_tokens` is left out of the request by default, which is what every
    OpenAI-compatible server reads as "answer until you are done, the context
    is the only limit". A cleanup or a translation has to return the whole
    piece it was given, so a cap here would silently shorten the transcript —
    only callers that genuinely want a short answer (a question about the
    guide) pass a number.
    """
    settings = config.get_settings()
    base_url, api_key, model = _resolve_endpoint(settings)
    if model_override:
        model = model_override
    if not model:
        raise LLMNotConfigured("No LLM model configured")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

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
            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason") or ""
            raw = _message_content(message)
            text = strip_reasoning(raw)
            if not text:
                # worth a retry: how much a model thinks is not deterministic
                raise EmptyAnswer(_empty_answer_reason(message, raw, finish_reason))
            _publish_status("idle")
            if finish_reason == "length":
                logger.warning(
                    "answer cut off by the token limit after %d characters (model %s)",
                    len(text),
                    model,
                )
                raise TruncatedAnswer(text)
            return text
        except (httpx.HTTPError, EmptyAnswer, KeyError, IndexError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "LLM call failed (attempt %d/%d, model %s): %s",
                attempt,
                MAX_ATTEMPTS,
                model,
                _error_detail(exc),
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S * attempt)

    detail = _error_detail(last_error) if last_error else ""
    _publish_status("error", detail)
    raise LLMError(f"LLM-Aufruf fehlgeschlagen ({model}): {detail}") from last_error


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
