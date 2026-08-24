"""Application settings, persisted as JSON in the data directory.

The data directory is resolved in this order:
1. VERBA_DATA_DIR environment variable (also used by tests)
2. TRANSKRIPTOR_DATA_DIR (legacy fallback for pre-rename installations)
3. <project root>/data

All other configurable paths (models, workspaces, tools) default to
subdirectories of the data directory but can be overridden in the settings.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

FROZEN = getattr(sys, "frozen", False)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    """Directory holding the read-only bundled resources (frontend/, docs/).

    In a PyInstaller build these are unpacked next to the binary; in a source
    checkout they simply live in the project root.
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return PROJECT_ROOT


def _frozen_data_dir() -> Path:
    """Per-user data directory for installed (PyInstaller) builds — the install
    location is read-only for normal users, so runtime data must live here."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Verba"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Verba"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "verba"


def site_packages_repair_marker() -> Path:
    return data_dir() / "site-packages-repair"


def ensure_runtime_site_packages() -> None:
    """Frozen builds pip-install feature groups into <data>/site-packages;
    make that directory importable (idempotent, no-op in source checkouts).

    A failed in-process installation can leave half-deleted packages behind
    (Windows locks loaded .pyd files). setup_check leaves a repair marker in
    that case; at the next start — before anything is imported and locked —
    the directory is wiped so the setup can reinstall cleanly."""
    if not FROZEN:
        return
    target = data_dir() / "site-packages"
    marker = site_packages_repair_marker()
    if marker.exists():
        import shutil

        logger.warning("repairing site-packages (a previous installation failed)")
        shutil.rmtree(target, ignore_errors=True)
        marker.unlink(missing_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


class ServerSettings(BaseModel):
    port: int = Field(default=8710, ge=1, le=65535)


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    retention_days: int = Field(default=14, ge=1, le=365)


class WhisperSettings(BaseModel):
    model: str = "small"
    models_dir: str = ""  # empty = <data>/models; subfolders are scanned for models
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"
    language: str = ""  # empty = automatic detection; otherwise ISO code like "de"


class LLMSettings(BaseModel):
    mode: Literal["none", "openai", "local"] = "none"
    base_url: str = ""  # OpenAI-compatible endpoint, e.g. http://localhost:8000/v1
    api_key: str = ""
    model: str = ""


class SearchSettings(BaseModel):
    # Multilingual, CPU-friendly default; German queries also match en/ru content.
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class GeneralSettings(BaseModel):
    ui_language: str = "de"
    workspaces_dir: str = ""  # empty = <project root>/workspaces
    browse_roots: list[str] = Field(default_factory=list)  # empty = user home dir only


class SetupState(BaseModel):
    completed: bool = False
    ffmpeg_path: str = ""  # filled when ffmpeg was auto-installed to <data>/tools


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    setup: SetupState = Field(default_factory=SetupState)


_lock = threading.Lock()
_cached: Settings | None = None
_cached_data_dir: Path | None = None


def data_dir() -> Path:
    env = os.environ.get("VERBA_DATA_DIR") or os.environ.get("TRANSKRIPTOR_DATA_DIR")
    if env:
        path = Path(env)
    elif FROZEN:
        path = _frozen_data_dir()
    else:
        path = PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return data_dir() / "settings.json"


def models_dir(settings: Settings) -> Path:
    path = (
        Path(settings.whisper.models_dir) if settings.whisper.models_dir else data_dir() / "models"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspaces_dir(settings: Settings) -> Path:
    if settings.general.workspaces_dir:
        path = Path(settings.general.workspaces_dir)
    elif FROZEN:
        path = data_dir() / "workspaces"
    else:
        path = PROJECT_ROOT / "workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def embeddings_dir() -> Path:
    """Cache directory for sentence-transformers downloads (kept under data/)."""
    path = data_dir() / "models" / "embeddings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tools_dir() -> Path:
    path = data_dir() / "tools"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_from_disk() -> Settings:
    path = settings_path()
    if path.exists():
        try:
            return Settings.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            logger.error("settings.json is invalid, using defaults: %s", exc)
    return Settings()


def get_settings() -> Settings:
    """Return the cached settings, reloading if the data dir changed (tests)."""
    global _cached, _cached_data_dir
    with _lock:
        current_dir = data_dir()
        if _cached is None or _cached_data_dir != current_dir:
            _cached = _load_from_disk()
            _cached_data_dir = current_dir
        return _cached


def save_settings(settings: Settings) -> None:
    global _cached, _cached_data_dir
    with _lock:
        settings_path().write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        _cached = settings
        _cached_data_dir = data_dir()


def reset_cache() -> None:
    """For tests: force a reload from disk on next access."""
    global _cached, _cached_data_dir
    with _lock:
        _cached = None
        _cached_data_dir = None
