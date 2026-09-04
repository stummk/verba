"""Resumable HTTP downloads with progress — one place for every large file.

A llama.cpp release archive, a GGUF model and Verba's own update artifact are
all hundreds of megabytes, and all three are fetched over whatever line the
machine happens to have. So they share the same rules: continue after a
dropped connection instead of starting over (HTTP Range), refuse before the
disk fills up rather than dying halfway, and report progress in percent so
the UI can show a bar.

`emit(percent, message)` is the caller's progress callback — the same shape
the services use for their own event streams.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: A multi-gigabyte download over a shaky line gets more than one chance.
ATTEMPTS = 4
RETRY_DELAY_S = 3
#: Read buffer; big enough that the progress callback is not called per packet.
CHUNK_BYTES = 1 << 18


class Interrupted(RuntimeError):
    """A download that stopped early — worth another attempt, unlike an HTTP error."""


def require_free_space(target: Path, needed_bytes: int) -> None:
    """Refuse up front instead of dying with a full disk mid-download."""
    if needed_bytes <= 0:
        return
    try:
        free = shutil.disk_usage(target).free
    except OSError:  # the directory does not exist yet, or is not a real mount
        return
    if free < needed_bytes:
        raise RuntimeError(
            f"Zu wenig Speicherplatz in {target}: "
            f"{needed_bytes // (1024 * 1024)} MB nötig, {free // (1024 * 1024)} MB frei"
        )


def fetch(url: str, target: Path, max_bytes: int, emit: Any) -> None:
    """Download `url` to `target`, continuing after a dropped connection.

    A model is several gigabytes, and a connection that drops at 90 % used to
    mean starting over. Every attempt after the first asks for the rest of the
    file (HTTP Range), so only the missing part is fetched again. A refusal
    from the server (404, 401 …) is final and not retried.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, ATTEMPTS + 1):
        have = target.stat().st_size if target.exists() else 0
        try:
            _stream(url, target, max_bytes, emit, have)
            return
        except (Interrupted, httpx.TransportError) as exc:
            if attempt == ATTEMPTS:
                raise
            logger.warning("download attempt %d failed (%s), continuing: %s", attempt, exc, url)
            emit(0, f"Verbindung abgebrochen — neuer Versuch {attempt + 1}/{ATTEMPTS} ...")
            time.sleep(RETRY_DELAY_S)


def _stream(url: str, target: Path, max_bytes: int, emit: Any, have: int) -> None:
    """One attempt: append to `target` from byte `have` on."""
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", url, follow_redirects=True, timeout=120, headers=headers) as response:
        if response.status_code == 416:
            # the partial file is at or past the end — it does not belong to this URL
            target.unlink(missing_ok=True)
            raise Interrupted(f"Teildatei passt nicht zum Download: {url}")
        if have and response.status_code == 200:
            have = 0  # the server ignored the range and sends the whole file
        response.raise_for_status()
        remaining = int(response.headers.get("content-length", 0))
        total = have + remaining
        if total > max_bytes:
            raise RuntimeError(f"Download zu groß ({total} Bytes): {url}")
        require_free_space(target.parent, remaining)
        received = have
        last_percent = -1
        with open(target, "ab" if have else "wb") as fh:
            for chunk in response.iter_bytes(chunk_size=CHUNK_BYTES):
                received += len(chunk)
                if received > max_bytes:
                    raise RuntimeError(f"Download überschreitet Größenlimit: {url}")
                fh.write(chunk)
                if total:
                    percent = int(received * 98 / total)
                    if percent != last_percent:
                        last_percent = percent
                        emit(percent, "")
    if total and received != total:
        # a connection dropping mid-stream must not leave a file that looks complete
        raise Interrupted(f"Download unvollständig ({received} von {total} Bytes): {url}")


def error_message(exc: Exception) -> str:
    """German wording for a failed download; httpx phrases its own in English."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (403, 429) and "api.github.com" in str(exc.request.url):
            return "GitHub blockt die Abfrage gerade (Ratenlimit) — bitte später erneut versuchen"
        if status in (401, 403):
            return f"Download nicht möglich — die Quelle verweigert den Zugriff (HTTP {status})"
        if status == 404:
            return "Download nicht möglich — die Quelle antwortet mit 404"
        return f"Download nicht möglich (HTTP {status})"
    if isinstance(exc, httpx.HTTPError):
        return f"Download fehlgeschlagen: Verbindungsfehler ({type(exc).__name__})"
    return str(exc)


def phase(emit: Any, base: int, span: int, label: str) -> Any:
    """Map one download's 0-98 progress into its slice of an overall bar."""

    def report(percent: int, message: str, state: str = "running") -> None:
        emit(base + percent * span // 100, message or label, state)

    return report
