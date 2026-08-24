"""Public OpenAI-compatible transcription API — service layer.

Owns three things:
- API key management (api_keys table): only a SHA-256 hash is persisted,
  the plaintext key is returned exactly once at creation time.
- The synchronous job bridge: the HTTP request enqueues an "api_transcribe"
  job (main lane, fair per API key) and waits; the handler stores its result
  in a small in-memory map keyed by job id for the waiting request to pick up.
- Response formatting for the OpenAI wire formats (srt/vtt timestamps).

Endpoints live in api/openai_compat.py and api/apikeys.py.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from pathlib import Path
from typing import Any

from .. import config, db
from . import llm, pipeline, whisper

# ── API keys ──────────────────────────────────────────────────────────

KEY_COLUMNS = "id, name, prefix, created_at, last_used_at"


def _hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_key(name: str) -> dict[str, Any]:
    token = "vb-" + secrets.token_hex(24)
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO api_keys (name, prefix, key_hash) VALUES (?, ?, ?)",
            (name, token[:11], _hash_key(token)),
        )
        row = conn.execute(
            f"SELECT {KEY_COLUMNS} FROM api_keys WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    result = dict(row)
    result["key"] = token  # shown exactly once — never retrievable again
    return result


def list_keys() -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(f"SELECT {KEY_COLUMNS} FROM api_keys ORDER BY id").fetchall()
    return db.rows_to_dicts(rows)


def delete_key(key_id: int) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return cursor.rowcount > 0


def keys_configured() -> bool:
    with db.get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM api_keys").fetchone()
    return row["n"] > 0


def verify_key(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {KEY_COLUMNS} FROM api_keys WHERE key_hash = ?", (_hash_key(token),)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?", (row["id"],)
        )
    return dict(row)


# ── uploads ───────────────────────────────────────────────────────────


def uploads_dir() -> Path:
    path = config.data_dir() / "api_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── synchronous job bridge ────────────────────────────────────────────

_results: dict[int, dict[str, Any]] = {}
_results_lock = threading.Lock()
# Orphaned results (requeued after a restart: no client is waiting anymore)
# are dropped oldest-first so the map can never grow unbounded.
_MAX_KEPT_RESULTS = 20


def _store_result(job_id: int, result: dict[str, Any]) -> None:
    with _results_lock:
        _results[job_id] = result
        while len(_results) > _MAX_KEPT_RESULTS:
            _results.pop(next(iter(_results)))


def pop_result(job_id: int) -> dict[str, Any] | None:
    with _results_lock:
        return _results.pop(job_id, None)


def handle_api_transcribe_job(job: dict[str, Any], cancel, report) -> None:
    """Payload: {audio_path, language, model, cleanup, type_prompt}."""
    payload = job["payload"]
    audio_path = Path(payload["audio_path"])
    try:
        if not audio_path.exists():
            raise RuntimeError("Uploaded file is missing (server restart?) — please send it again")
        result = whisper.transcribe_path(
            audio_path,
            language=payload.get("language", ""),
            model_override=payload.get("model", ""),
            cancel=cancel,
            report=report,
        )
        text = "\n".join(s["text"] for s in result["segments"]).strip()
        if payload.get("cleanup"):
            if llm.llm_location() == "local":
                whisper.unload_model()  # both engines share the local hardware
            text = pipeline.cleanup_segments(
                result["segments"], payload.get("type_prompt", ""), "", cancel, report, (99, 99)
            )
        _store_result(job["id"], {**result, "text": text})
        report(100, "Transcription completed")
    finally:
        audio_path.unlink(missing_ok=True)


# ── response formatting ───────────────────────────────────────────────


def format_timestamp(seconds: float, *, comma: bool) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    separator = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


def to_srt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{i + 1}\n"
        f"{format_timestamp(s['start'], comma=True)} --> "
        f"{format_timestamp(s['end'], comma=True)}\n"
        f"{s['text']}"
        for i, s in enumerate(segments)
    ]
    return "\n\n".join(blocks) + "\n"


def to_vtt(segments: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines.append(
            f"{format_timestamp(segment['start'], comma=False)} --> "
            f"{format_timestamp(segment['end'], comma=False)}"
        )
        lines.append(segment["text"])
        lines.append("")
    return "\n".join(lines)


def to_verbose_json(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "transcribe",
        "language": result["language"],
        "duration": result["duration"],
        "text": result["text"],
        "segments": [
            {"id": i, "start": s["start"], "end": s["end"], "text": s["text"]}
            for i, s in enumerate(result["segments"])
        ],
    }
