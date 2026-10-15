"""OpenAI-compatible chat client — one codepath for remote APIs and local servers.

Works against any endpoint that speaks the OpenAI chat-completions protocol
(OpenAI, llama.cpp `llama-server`, Ollama, LM Studio, vLLM, ...). In "local"
mode the managed llama.cpp server is started on demand and used as endpoint.

Status changes are broadcast as "engine.status" events (engine "llm") so
the AI status monitor in the UI stays live.

Reasoning models are turned down (or off) from `settings.llm.reasoning`,
because nothing Verba asks an LLM for benefits from deliberation: cleaning up,
translating and structuring are transformations with an explicit prompt. What
thinking does cost is the token budget the answer itself needs — which is
exactly where EmptyAnswer and TruncatedAnswer come from.

Switching it off, though, is a request: a model free to ignore it is also free
to hand the transcript back unchanged. `settings.llm.reasoning_budget` is the
enforced half — the server closes the thought once the budget is spent — and
it is what stands between a long recording and a model that never arrives.
"""

from __future__ import annotations

import json
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

# Several levers, because no single one reaches every server:
# - `reasoning_effort` is the OpenAI parameter; llama.cpp reads "none" from it
#   as "do not think at all".
# - `chat_template_kwargs.enable_thinking` is what the Qwen3-style templates
#   look at, used by llama.cpp, vLLM and LM Studio. It has to be a real JSON
#   boolean — a string is rejected.
# - the budget fields are the only ones a server *enforces*: switching thinking
#   off is a request the model may ignore (and a model that then returns the
#   transcript unchanged is exactly what it looks like), while a budget forces
#   the closing tag once it is spent. llama.cpp reads either name.
# What an endpoint does not know it answers with a 400 that names the field,
# and `_reasoning_unsupported` remembers *that field* for that endpoint — the
# detour is paid once, and one unknown field does not cost the others.
BUDGET_FIELDS = ("reasoning_budget_tokens", "thinking_budget_tokens")

# Put in front of the forced closing tag when the budget is spent. Without it
# the thought simply stops, and what follows reads like the model finishing a
# sentence instead of starting its answer.
BUDGET_MESSAGE = "\n\nDas Denk-Budget ist aufgebraucht — ich antworte jetzt."

_reasoning_unsupported: dict[str, set[str]] = {}


def reasoning_fields(settings: config.Settings, mode: str = "") -> dict[str, Any]:
    """The request fields that turn this endpoint's thinking down.

    `mode` overrides the configured one for a single call; the budget is the
    setting's either way, because it answers a different question — not
    whether the model may think, but when it has to stop.
    """
    mode = mode or settings.llm.reasoning
    if mode == "off":
        # nothing left to cap, and every field sent is one more to be refused
        return {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    fields: dict[str, Any] = {"reasoning_effort": "low"} if mode == "low" else {}
    if budget := settings.llm.reasoning_budget:
        fields.update(dict.fromkeys(BUDGET_FIELDS, budget))
        fields["reasoning_budget_message"] = BUDGET_MESSAGE
    return fields


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


def configured_model(settings: config.Settings | None = None) -> str:
    """The model a call would run on right now, without starting anything.

    Which model produced a text belongs to that text (`derived_texts.model`),
    and the pipeline rarely passes one: a project only overrides the model, it
    does not normally name it. Asked here, because this is where the answer
    lives — including the local server, whose model is a file on disk the
    settings may not name at all.
    """
    settings = settings or config.get_settings()
    if settings.llm.mode == "none":
        return ""
    if settings.llm.model:
        return settings.llm.model
    if settings.llm.mode == "local":
        from . import llamacpp

        return llamacpp.active_model_name()
    return ""


def strip_reasoning(text: str) -> str:
    """Drop a reasoning model's thinking and keep the answer."""
    text = _THINK_BLOCK_RE.sub("", text)
    if _THINK_HEAD_RE.search(text):  # closing tag without an opener
        text = _THINK_HEAD_RE.sub("", text)
    text = _THINK_TAIL_RE.sub("", text)  # opener without a closer: nothing usable follows
    return text.strip()


# ── reading and judging what came back ────────────────────────────────
#
# Every caller that asks for structured output or has to check an answer
# against its material faces the same two problems, so they are answered
# once here instead of per caller.


def _json_values(raw: str, opener: str, kind: type) -> list[Any]:
    """Every JSON value of `kind` in an answer, in order.

    A local model rarely answers with exactly one value: it wraps it in prose
    or a Markdown fence, or emits one per paragraph. Decoding from each
    opening bracket and skipping what does not parse takes all of those,
    where taking the span from the first bracket to the last took none.
    """
    decoder = json.JSONDecoder()
    found: list[Any] = []
    index = 0
    while (start := raw.find(opener, index)) >= 0:
        try:
            data, end = decoder.raw_decode(raw, start)
        except ValueError:
            index = start + 1
            continue
        index = end
        if isinstance(data, kind):
            found.append(data)
    return found


def json_arrays(raw: str) -> list[list[Any]]:
    """Every JSON array an answer carries, in order."""
    return _json_values(raw, "[", list)


def json_object(raw: str) -> dict[str, Any] | None:
    """The first JSON object an answer carries, or None."""
    objects = _json_values(raw, "{", dict)
    return objects[0] if objects else None


# What counts as a word when an answer is weighed against its material, and
# how much material it takes to judge at all. One decision, so that the two
# checks built on it (`pdf._keeps_the_material`, `pipeline._keeps_the_text`)
# cannot drift apart: short filler words are exactly what an answer may drop.
_WORD_RE = re.compile(r"\w{4,}")
MIN_WORDS_TO_JUDGE = 20


def word_set(text: str) -> set[str]:
    """The distinct words of a text, for comparing an answer to its material."""
    return set(_WORD_RE.findall(text.lower()))


def _message_content(message: dict[str, Any]) -> str:
    """The message text, also for servers that answer in content parts."""
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def _thinking_advice() -> str:
    """What to do about a model that thinks instead of answering.

    Only worth saying while the switch is not already off — otherwise the
    server ignored it and the model itself is the problem.
    """
    if config.get_settings().llm.reasoning == "off":
        return (
            " Der Denkmodus ist bereits abgeschaltet — dieses Modell oder dieser Endpunkt "
            "ignoriert die Einstellung. Bitte ein Modell ohne Reasoning verwenden."
        )
    return (
        " Bitte unter Einstellungen → KI-Aufbereitung „Reasoning“ auf „Aus“ stellen "
        "oder ein Modell ohne Reasoning verwenden."
    )


def _empty_answer_reason(message: dict[str, Any], raw: str, finish_reason: str) -> str:
    """Why an answer carries no text — shown verbatim in the web UI (German)."""
    thinking = bool(raw.strip()) or bool(message.get("reasoning_content"))
    if finish_reason == "length":
        return (
            "Das Modell hat sein Token-Budget aufgebraucht, bevor eine Antwort begann"
            + (" — es hat nur nachgedacht." if thinking else ".")
            + _thinking_advice()
        )
    if thinking:
        return (
            "Das Modell hat nur interne Überlegungen geliefert, keinen Antworttext."
            + _thinking_advice()
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
        return settings.llm.base_url.rstrip("/"), settings.llm.api_key, configured_model(settings)
    if settings.llm.mode == "local":
        from . import hardware, llamacpp

        try:
            base_url = llamacpp.ensure_running(settings)
        except hardware.InsufficientMemory as exc:
            # not a bug but a machine limit: the caller shows this verbatim
            raise LLMError(str(exc)) from exc
        # asked after the server is up, which is when it knows its own model
        return base_url.rstrip("/"), "", configured_model(settings)
    raise LLMNotConfigured("No LLM configured (mode: none)")


def chat(
    messages: list[dict[str, str]],
    model_override: str = "",
    temperature: float = 0.2,
    max_tokens: int | None = None,
    reasoning: str = "",
) -> str:
    """One chat completion with retries; returns the assistant message text.

    `max_tokens` is left out of the request by default, which is what every
    OpenAI-compatible server reads as "answer until you are done, the context
    is the only limit". A cleanup or a translation has to return the whole
    piece it was given, so a cap here would silently shorten the transcript —
    only callers that genuinely want a short answer (a question about the
    guide) pass a number.

    `reasoning` overrides the configured setting for this one call; empty
    means "whatever the settings say".
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
    extras = reasoning_fields(settings, reasoning)

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # re-evaluated per attempt: a refusal in the previous one may just
        # have taught us which of the controls this endpoint does not take
        refused = _reasoning_unsupported.get(base_url, frozenset())
        sent_extras = {k: v for k, v in extras.items() if k not in refused}
        try:
            _publish_status("busy", f"{model} (attempt {attempt}/{MAX_ATTEMPTS})")
            response = httpx.post(
                f"{base_url}/chat/completions",
                json={**body, **sent_extras},
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
            if rejected := _rejected_reasoning_fields(exc, sent_extras):
                # not a failure of the request, only of one of its fields:
                # retry immediately without it instead of burning a backoff
                _reasoning_unsupported.setdefault(base_url, set()).update(rejected)
                logger.info(
                    "%s does not accept %s — retrying without it",
                    base_url,
                    ", ".join(sorted(rejected)),
                )
                continue
            logger.warning(
                "LLM call failed (attempt %d/%d, model %s): %s",
                attempt,
                MAX_ATTEMPTS,
                model,
                _error_detail(exc),
            )
            if isinstance(exc, httpx.TimeoutException):
                # Repeating it would cost another five minutes for the same
                # answer. A model that never finishes is a budget question,
                # not a flaky connection — so say that instead of waiting.
                _publish_status("error", _error_detail(exc))
                raise LLMError(_timeout_message(model, settings)) from exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S * attempt)

    detail = _error_detail(last_error) if last_error else ""
    _publish_status("error", detail)
    raise LLMError(f"LLM-Aufruf fehlgeschlagen ({model}): {detail}") from last_error


def _timeout_message(model: str, settings: config.Settings) -> str:
    """Why a call was given up on — shown verbatim in the web UI (German)."""
    minutes = int(REQUEST_TIMEOUT // 60)
    advice = (
        " Ein kleineres Denk-Budget oder ein schnelleres Modell hilft hier."
        if settings.llm.reasoning_budget
        else " Unter Einstellungen → KI-Aufbereitung lässt sich das Denk-Budget begrenzen, "
        "damit das Modell zu einem Ergebnis kommt."
    )
    return f"Das Modell '{model}' hat nach {minutes} Minuten nicht geantwortet.{advice}"


def _rejected_reasoning_fields(exc: Exception, sent: dict[str, Any]) -> set[str]:
    """Which of the reasoning fields this 400 was about, if any.

    Deliberately narrow: a 400 for any other reason must stay a 400, or one
    bad request would silently switch the reasoning control off for good.
    Every server that rejects an unknown parameter names it in the message —
    and only that one is given up, so an endpoint that knows `reasoning_effort`
    but not the budget keeps the half it understands.
    """
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 400:
        return set()
    # searched, not lower-cased: some servers echo the whole request back in
    # the error body, and that is a transcript chunk, not a sentence
    body = exc.response.text
    return {name for name in sent if re.search(re.escape(name), body, re.IGNORECASE)}


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
