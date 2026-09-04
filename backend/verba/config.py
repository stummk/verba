"""Application settings, persisted as JSON in the base directory.

The *base* directory is resolved in this order:
1. VERBA_DATA_DIR environment variable (also used by tests)
2. TRANSKRIPTOR_DATA_DIR (legacy fallback for pre-rename installations)
3. <project root>/data

It belongs to the installation and never moves: settings.json (the file that
says where everything else goes), `site-packages` (frozen builds put it on
sys.path at startup), the downloaded ffmpeg and the model directories — all
re-downloadable, none of it worth a backup.

The *data* directory holds what is: the database and the logs. It defaults to
the base directory and can be pointed at a backed-up drive
(`general.data_dir`). Because the database and the log files are open while
the app runs, a change only takes effect at the next start — `data_dir()`
therefore answers with `general.data_dir_active`, which is where the data
really is, and `datamove.py` reconciles the two at startup.

The workspaces, the whisper models, the embeddings and the GGUF files each
have their own setting on top of that.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

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


def runtime_site_packages() -> Path:
    # deliberately the *base* directory: this one is on sys.path while the
    # process runs, so it must not travel with a relocated data directory
    return base_data_dir() / "site-packages"


def site_packages_repair_marker() -> Path:
    return base_data_dir() / "site-packages-repair"


def mark_site_packages_repair(names: list[str]) -> None:
    """Record top-level packages a failed installation may have damaged.

    Windows keeps loaded .pyd files locked, so a pip run that replaces such a
    package can delete part of it and then fail. The named directories are
    removed at the next start (before anything imports them again) so the
    retry installs them cleanly — everything else stays in place, because
    wiping the whole directory would also throw away the groups that already
    work."""
    unique = sorted({name for name in names if name and "/" not in name and "\\" not in name})
    if not unique:
        return
    site_packages_repair_marker().write_text("\n".join(unique), encoding="utf-8")


def repair_site_packages() -> None:
    """Remove the packages a previous failed installation left damaged.

    Call once at startup, before the runtime site-packages is imported from.
    """
    if not FROZEN:
        return
    marker = site_packages_repair_marker()
    if not marker.exists():
        return
    import shutil

    target = runtime_site_packages()
    names = [line.strip() for line in marker.read_text(encoding="utf-8").splitlines()]
    failed = False
    for name in filter(None, names):
        # a distribution owns its package dir plus siblings like numpy.libs
        # and numpy-2.1.0.dist-info — glob covers all of them
        for path in target.glob(f"{name}*"):
            logger.warning("removing damaged package from site-packages: %s", path.name)
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError:
                logger.exception("could not remove %s; will retry on next start", path)
                failed = True
    if not failed:
        marker.unlink(missing_ok=True)


def ensure_runtime_site_packages() -> None:
    """Frozen builds pip-install feature groups into <data>/site-packages;
    make that directory importable (idempotent, no-op in source checkouts)."""
    if not FROZEN:
        return
    target = runtime_site_packages()
    target.mkdir(parents=True, exist_ok=True)
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


def normalize_dir(value: str) -> str:
    """Absolute, expanded form of a configured directory ("" keeps the default).

    Users type all of these: `M:\\Modelle\\whisper`, `~/verba`,
    `%USERPROFILE%\\Verba`, a path pasted with quotes by the Windows explorer,
    or a relative one — which would otherwise point at whatever the working
    directory happens to be when the app is started, and therefore somewhere
    else for the installed build than for a source checkout.
    """
    text = value.strip().strip('"')
    if not text:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(text))
    return os.path.abspath(expanded)


class ServerSettings(BaseModel):
    port: int = Field(default=8710, ge=1, le=65535)


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    retention_days: int = Field(default=14, ge=1, le=365)


class WhisperSettings(BaseModel):
    model: str = "small"
    models_dir: str = ""  # empty = <base>/models; subfolders are scanned for models
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"
    language: str = ""  # empty = automatic detection; otherwise ISO code like "de"

    @field_validator("models_dir")
    @classmethod
    def _absolute_models_dir(cls, value: str) -> str:
        return normalize_dir(value)


class LLMSettings(BaseModel):
    mode: Literal["none", "openai", "local"] = "none"
    base_url: str = ""  # OpenAI-compatible endpoint, e.g. http://localhost:8000/v1
    api_key: str = ""
    model: str = ""
    # How much a reasoning model may think before answering. Verba's work is
    # transformation with an explicit prompt — cleaning up, translating,
    # structuring — where thinking buys nothing and costs the token budget the
    # answer itself needs, which is where EmptyAnswer/TruncatedAnswer come
    # from. Hence "off" by default; "auto" leaves the model alone.
    reasoning: Literal["off", "low", "auto"] = "off"
    # empty = <base>/models/llm; GGUF files already lying in the configured
    # directory are used from there, never copied or downloaded again
    models_dir: str = ""

    @field_validator("models_dir")
    @classmethod
    def _absolute_models_dir(cls, value: str) -> str:
        return normalize_dir(value)


@dataclass(frozen=True)
class EmbeddingModel:
    """One selectable embedding model for the semantic search.

    `query_prefix`/`passage_prefix` belong to the model contract: the E5
    family is trained with them and loses a lot of quality without.
    """

    name: str
    label: str
    dim: int
    size_mb: int
    languages: int
    speed: Literal["fast", "balanced", "quality"]
    query_prefix: str = ""
    passage_prefix: str = ""


# A curated list instead of a free-text field: a mistyped model id would only
# fail deep inside the first index run, and a monolingual one would silently
# rank German queries against English text. All of them are multilingual and
# run on the CPU; the first is the default, the last trades size and speed for
# retrieval quality.
EMBEDDING_MODELS: tuple[EmbeddingModel, ...] = (
    EmbeddingModel(
        name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        label="MiniLM multilingual (Standard)",
        dim=384,
        size_mb=470,
        languages=50,
        speed="fast",
    ),
    EmbeddingModel(
        name="intfloat/multilingual-e5-small",
        label="Multilingual E5 small",
        dim=384,
        size_mb=470,
        languages=100,
        speed="balanced",
        query_prefix="query: ",
        passage_prefix="passage: ",
    ),
    EmbeddingModel(
        name="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        label="mpnet multilingual",
        dim=768,
        size_mb=1030,
        languages=50,
        speed="quality",
    ),
    # XLM-RoBERTa-large based: clearly the best multilingual retrieval of the
    # four, and the only one whose download and CPU time you notice. Needs no
    # prefixes (unlike the English BGE models, which expect an instruction),
    # and its 1024 dimensions make the vector table correspondingly larger.
    EmbeddingModel(
        name="BAAI/bge-m3",
        label="BGE-M3 (beste Qualität, groß)",
        dim=1024,
        size_mb=2270,
        languages=100,
        speed="quality",
    ),
)

DEFAULT_EMBEDDING_MODEL = EMBEDDING_MODELS[0].name


def embedding_model(name: str = "") -> EmbeddingModel:
    """Catalog entry for a model name; the default for anything unknown."""
    for entry in EMBEDDING_MODELS:
        if entry.name == name:
            return entry
    return EMBEDDING_MODELS[0]


class SearchSettings(BaseModel):
    # Multilingual, CPU-friendly default; German queries also match en/ru content.
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # empty = <data>/models/embeddings; a model already lying in the configured
    # directory is loaded from there instead of being downloaded again
    embeddings_dir: str = ""

    @field_validator("embeddings_dir")
    @classmethod
    def _absolute_embeddings_dir(cls, value: str) -> str:
        return normalize_dir(value)

    @field_validator("embedding_model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        """Fall back to the default instead of failing on an unknown id.

        Settings written by an older version (or by hand) may name a model
        that is not in the catalog — the search must still start.
        """
        if any(entry.name == value for entry in EMBEDDING_MODELS):
            return value
        if value:
            logger.warning("unknown embedding model %r, using the default", value)
        return DEFAULT_EMBEDDING_MODEL


class GeneralSettings(BaseModel):
    ui_language: str = "de"
    workspaces_dir: str = ""  # empty = <project root>/workspaces
    # Where the database and the logs live — the part worth putting on a
    # backed-up drive. Empty = the base directory. Changing it does not move
    # anything right away: `data_dir_active` says where the data is *now*, and
    # the next start reconciles the two (see datamove.py).
    data_dir: str = ""
    # Backend-owned, like auth.enabled: written only after a move has actually
    # happened, never by the settings form.
    data_dir_active: str = ""
    browse_roots: list[str] = Field(default_factory=list)  # empty = user home dir only

    @field_validator("workspaces_dir", "data_dir", "data_dir_active")
    @classmethod
    def _absolute_dir(cls, value: str) -> str:
        return normalize_dir(value)


class AuthSettings(BaseModel):
    """Optional user management.

    `enabled` is owned by the backend (services.auth), never by the settings
    form: a normal user who could write it would simply switch the whole
    protection off. Everything else here is an admin preference.
    """

    enabled: bool = False
    # What a newly created transcript gets when the owner does not choose.
    default_visibility: Literal["private", "shared", "public"] = "private"
    session_days: int = Field(default=30, ge=1, le=365)
    # Whether the session cookie is marked Secure (https only).
    # "auto" follows the request scheme, which behind a TLS-terminating proxy
    # is the scheme the *browser* used, taken from X-Forwarded-Proto — uvicorn
    # trusts that header from 127.0.0.1 by default (widen with the
    # FORWARDED_ALLOW_IPS environment variable for a proxy on another host).
    # "always" is for a setup where that header cannot be relied on; "never"
    # only for a deliberately plain-http installation.
    cookie_secure: Literal["auto", "always", "never"] = "auto"


class UpdateSettings(BaseModel):
    """Looking for a new release of Verba itself (services/updates.py).

    The only setting that reaches out to the internet on its own, so it can be
    switched off — an installation without internet access, or one that is
    updated by whoever administers the machine, then never asks GitHub.
    """

    check_enabled: bool = True


class SetupState(BaseModel):
    completed: bool = False
    ffmpeg_path: str = ""  # filled when ffmpeg was auto-installed to <base>/tools


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    updates: UpdateSettings = Field(default_factory=UpdateSettings)
    setup: SetupState = Field(default_factory=SetupState)


_lock = threading.Lock()
_cached: Settings | None = None
_cached_data_dir: Path | None = None


def base_data_dir() -> Path:
    """The installation's own directory: settings.json, site-packages, tools
    and the downloaded models.

    Not necessarily where the database and the logs are — see data_dir().
    """
    env = os.environ.get("VERBA_DATA_DIR") or os.environ.get("TRANSKRIPTOR_DATA_DIR")
    if env:
        path = Path(env)
    elif FROZEN:
        path = _frozen_data_dir()
    else:
        path = PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir(settings: Settings | None = None) -> Path:
    """Where the database and the logs are *right now*.

    This is `general.data_dir_active`, not `general.data_dir`: a directory
    chosen in the settings form only takes effect at the next start, because
    the database and the log files are open while the app runs. Until then
    everything keeps reading and writing where the data actually is.
    """
    settings = settings or get_settings()
    active = settings.general.data_dir_active
    path = Path(active) if active else base_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def configured_data_dir(settings: Settings | None = None) -> Path:
    """Where the data is meant to be — the next start moves it there."""
    settings = settings or get_settings()
    configured = settings.general.data_dir
    return Path(configured) if configured else base_data_dir()


def settings_path() -> Path:
    # deliberately the base directory: this file is what says where the data
    # directory is, so it cannot live inside it
    return base_data_dir() / "settings.json"


def models_dir(settings: Settings) -> Path:
    path = (
        Path(settings.whisper.models_dir)
        if settings.whisper.models_dir
        else base_data_dir() / "models"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_workspaces_dir() -> Path:
    """Where projects live when no directory is configured.

    Follows the data directory in an installed build — transcript folders are
    the bulk of what a backup is for, so they belong next to the database.
    """
    return data_dir() / "workspaces" if FROZEN else PROJECT_ROOT / "workspaces"


def workspaces_root(settings: Settings) -> Path:
    """The configured workspaces directory — without creating it."""
    if settings.general.workspaces_dir:
        return Path(settings.general.workspaces_dir)
    return default_workspaces_dir()


def workspaces_dir(settings: Settings) -> Path:
    path = workspaces_root(settings)
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_embeddings_dir() -> Path:
    return base_data_dir() / "models" / "embeddings"


def embeddings_dir(settings: Settings | None = None) -> Path:
    """Directory for the search embedding models.

    Configurable, so a model that already sits somewhere on disk (a moved
    HuggingFace cache, a folder copied from another machine) is reused
    instead of downloaded a second time.
    """
    settings = settings or get_settings()
    configured = settings.search.embeddings_dir
    path = Path(configured) if configured else default_embeddings_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_llm_models_dir() -> Path:
    return base_data_dir() / "models" / "llm"


def llm_models_dir(settings: Settings | None = None) -> Path:
    """Directory holding the GGUF files for the managed local LLM.

    Same idea as the embeddings directory: point it at an existing collection
    (`F:\\Models\\llm`) and those files are used from there — llama-server
    loads them in place, nothing is copied.
    """
    settings = settings or get_settings()
    configured = settings.llm.models_dir
    path = Path(configured) if configured else default_llm_models_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def tools_dir() -> Path:
    # ffmpeg is re-downloadable installation state, not data worth backing up
    path = base_data_dir() / "tools"
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
        # keyed on the base directory: that is what decides which
        # settings.json is read, and it only changes in tests
        current_dir = base_data_dir()
        if _cached is None or _cached_data_dir != current_dir:
            _cached = _load_from_disk()
            _cached_data_dir = current_dir
        return _cached


def save_settings(settings: Settings) -> None:
    global _cached, _cached_data_dir
    with _lock:
        settings_path().write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        _cached = settings
        _cached_data_dir = base_data_dir()


def reset_cache() -> None:
    """For tests: force a reload from disk on next access."""
    global _cached, _cached_data_dir
    with _lock:
        _cached = None
        _cached_data_dir = None
