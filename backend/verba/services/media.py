"""Media helpers. `av` ships with faster-whisper, so import lazily —
the core app must work before the onboarding installed that group."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
    ".webm",
    ".mp4",
}


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def probe_duration(path: Path) -> float | None:
    """Duration in seconds, or None if it cannot be determined."""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as container:
            if container.duration is not None:
                return container.duration / 1_000_000  # AV_TIME_BASE
    except Exception as exc:
        logger.warning("could not determine duration of %s: %s", path, exc)
    return None
