"""faster-whisper integration: model discovery, lazy loading, transcription.

- Built-in model names are downloaded on first use into the configured models
  directory; local CTranslate2 model folders (containing model.bin) anywhere
  below that directory are discovered as well.
- The model is loaded lazily and reloaded when the relevant settings change.
- Before a load, services/hardware.py says whether this machine has the memory
  for that model: a model too large for the VRAM skips the GPU attempt, a model
  too large for everything is refused with a German message instead of letting
  the backend abort the process.
- If loading on CUDA fails (missing cuBLAS/cuDNN is common on Windows) or the
  GPU memory runs out, the service falls back to CPU/int8 and reports that via
  the status monitor.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import config, db
from ..core.jobs import JobCancelled
from ..events import hub
from . import hardware, transcripts, workspace
from .media import probe_duration

logger = logging.getLogger(__name__)

BUILTIN_MODELS = [
    "tiny",
    "base",
    "small",
    "medium",
    "large-v2",
    "large-v3",
    "large-v3-turbo",
    "distil-large-v3",
]

_model_lock = threading.Lock()
_model: Any = None
_model_key: tuple[str, str, str, str] | None = None  # name, device, compute, models dir
_active_device: str = ""
_cuda_broken = False  # set when CUDA libs or the VRAM turn out unusable at runtime


def _is_cuda_lib_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("cublas", "cudnn", "cuda"))


def list_local_models() -> list[str]:
    """CTranslate2 model folders (model.bin) anywhere below the models dir.
    HuggingFace cache folders (models--…) are internal storage and skipped."""
    root = config.models_dir(config.get_settings())
    found = []
    for model_bin in sorted(root.rglob("model.bin")):
        rel = model_bin.parent.relative_to(root)
        if any(part.startswith("models--") for part in rel.parts):
            continue
        found.append(str(rel))
    return found


def _hf_cache_dirs(name: str) -> list[str]:
    """HuggingFace cache folder names a built-in model may occupy. The
    name→repo mapping is owned by faster-whisper, but importing it pulls in
    the whole native stack — so the mapping is only consulted when the module
    is already loaded; static patterns cover every current builtin otherwise
    (a test guards the two against drift)."""
    repos: list[str] = []
    fw = sys.modules.get("faster_whisper")
    repo = getattr(getattr(fw, "utils", None), "_MODELS", {}).get(name) if fw else None
    if repo:
        repos.append(repo)
    if name.startswith("distil-"):
        fallback = f"Systran/faster-distil-whisper-{name.removeprefix('distil-')}"
    elif name == "large-v3-turbo":
        fallback = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    else:
        fallback = f"Systran/faster-whisper-{name}"
    if fallback not in repos:
        repos.append(fallback)
    return [f"models--{repo.replace('/', '--')}" for repo in repos]


def _hf_cache_complete(root: Path, name: str) -> bool:
    """True when a cache dir holds a finished snapshot (a crashed on-demand
    download leaves the models--… skeleton without a complete model.bin)."""
    return any(any((root / cache).glob("snapshots/*/model.bin")) for cache in _hf_cache_dirs(name))


def installed_builtin_models(local: list[str] | None = None) -> list[str]:
    """Built-in names that are already on disk (managed folder or HF cache)."""
    root = config.models_dir(config.get_settings())
    local_names = set(local if local is not None else list_local_models())
    installed = []
    for name in BUILTIN_MODELS:
        if name in local_names or _hf_cache_complete(root, name):
            installed.append(name)
    return installed


def list_models() -> dict[str, Any]:
    """The model list the settings page renders — with the fit for this machine.

    Every name is rated (see services/hardware.py), so the UI can say "runs
    here" / "tight" / "too large" before a download starts.
    """
    local = list_local_models()
    names = BUILTIN_MODELS + [name for name in local if name not in BUILTIN_MODELS]
    return {
        "builtin": BUILTIN_MODELS,
        "local": local,
        "installed": installed_builtin_models(local),
        "downloading": sorted(_active_downloads),
        **hardware.whisper_fit(names),
    }


# ── model management (settings page) ──────────────────────────────────

_downloads_lock = threading.Lock()
_active_downloads: set[str] = set()


def _download_target(name: str) -> Path:
    root = config.models_dir(config.get_settings())
    return root / name.replace("/", "_")


def start_model_download(name: str) -> bool:
    """Download a whisper model into the models dir (background thread).
    Returns False if this model is already being downloaded."""
    with _downloads_lock:
        if name in _active_downloads:
            return False
        _active_downloads.add(name)

    def run() -> None:
        hub.publish("model.download", {"name": name, "state": "running", "detail": ""})
        target = _download_target(name)
        try:
            from faster_whisper import download_model as fw_download

            target.mkdir(parents=True, exist_ok=True)
            fw_download(name, output_dir=str(target))
            hub.publish("model.download", {"name": name, "state": "done", "detail": str(target)})
            logger.info("Whisper model '%s' downloaded to %s", name, target)
        except Exception as exc:
            logger.exception("Model download failed: %s", name)
            if target.exists() and not (target / "model.bin").exists():
                shutil.rmtree(target, ignore_errors=True)  # remove partial download
            hub.publish("model.download", {"name": name, "state": "error", "detail": str(exc)})
        finally:
            with _downloads_lock:
                _active_downloads.discard(name)

    threading.Thread(target=run, daemon=True, name=f"model-download-{name}").start()
    return True


def delete_model(name: str) -> None:
    """Delete an installed model: a managed folder below the models dir and/or
    a built-in name whose files live in the HuggingFace cache (downloaded on
    demand by get_model). The traversal guard covers both locations. A folder
    without any model.bin (orphaned partial download) is removed too — but a
    folder that merely CONTAINS nested models is left alone."""
    root = config.models_dir(config.get_settings()).resolve()
    target = (root / name).resolve()
    if root not in target.parents:
        raise ValueError("Path is outside the model directory")
    deleted = False
    if target.is_dir() and ((target / "model.bin").exists() or not any(target.rglob("model.bin"))):
        shutil.rmtree(target)
        logger.info("Local model deleted: %s", target)
        deleted = True
    for cache in _hf_cache_dirs(name):
        cache_dir = root / cache
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
            logger.info("Whisper model deleted from HF cache: %s", cache_dir)
            deleted = True
    if not deleted:
        raise FileNotFoundError("No installed model with this name")
    if _model_key is not None and _model_key[0] == name:
        unload_model()  # never keep serving a model the user just deleted


def _resolve_model_ref(name: str) -> str:
    """A local folder (relative to models dir or absolute) wins over hub names."""
    root = config.models_dir(config.get_settings())
    candidate = Path(name) if Path(name).is_absolute() else root / name
    if (candidate / "model.bin").exists():
        return str(candidate)
    return name


def _publish_engine_status(state: str, detail: str = "") -> None:
    hub.publish("engine.status", {"engine": "whisper", "state": state, "detail": detail})


def get_model(model_override: str = "") -> Any:
    """Load (or reuse) the WhisperModel according to the current settings.

    model_override selects a different model for a single flow (advanced panel)
    without changing the persisted settings.
    """
    global _model, _model_key, _active_device
    settings = config.get_settings().whisper
    model_name = model_override or settings.model
    device = "cpu" if _cuda_broken and settings.device in ("auto", "cuda") else settings.device
    compute = settings.compute_type
    if device == "cpu" and compute == "auto":
        compute = "int8"
    # the directory is part of the identity: pointing the settings at another
    # collection has to reload, even when the model name stayed the same
    download_root = str(config.models_dir(config.get_settings()))
    key = (model_name, device, compute, download_root)

    with _model_lock:
        if _model is not None and _model_key == key:
            return _model

        from faster_whisper import WhisperModel

        verdict = _preflight(model_name, device, Path(download_root))
        model_ref = _resolve_model_ref(model_name)
        cpu_attempt = ("cpu", compute if compute not in ("auto", "float16") else "int8")
        if device in ("auto", "cuda") and verdict["device"] == "cpu":
            # the probe already knows the VRAM is too small — no point in
            # loading it there just to watch the allocation fail
            attempts: list[tuple[str, str]] = [cpu_attempt]
        else:
            attempts = [(device, compute)]
            if device in ("auto", "cuda"):
                attempts.append(cpu_attempt)  # CUDA libs missing, or VRAM full

        last_error: Exception | None = None
        for device, compute_type in attempts:
            try:
                _publish_engine_status("loading", f"Loading model '{model_name}' ({device}) ...")
                _model = WhisperModel(
                    model_ref,
                    device=device,
                    compute_type=compute_type,
                    download_root=download_root,
                )
                _model_key = key
                _active_device = device
                _publish_engine_status("ready", f"Model '{model_name}' on {device}")
                logger.info("Whisper model '%s' loaded (device=%s)", model_name, device)
                return _model
            except Exception as exc:  # noqa: BLE001 — any backend error triggers fallback
                logger.warning("loading the model on %s failed: %s", device, exc)
                last_error = exc

        if last_error is not None and hardware.is_oom(last_error):
            message = hardware.oom_message(attempts[-1][0], name=model_name)
            _publish_engine_status("error", message)
            raise hardware.InsufficientMemory(
                f"{message} Bitte ein kleineres Modell wählen oder Programme schließen."
            ) from last_error
        _publish_engine_status("error", f"Could not load model: {last_error}")
        raise RuntimeError(f"Could not load Whisper model: {last_error}")


def _preflight(model_name: str, device: str, models_dir: Path) -> dict[str, Any]:
    """Rate the model against this machine before the backend touches memory.

    A verdict of "no" is refused here: CTranslate2 answers an impossible
    allocation with an abort, which would take the whole process down instead
    of failing the one job.
    """
    verdict = hardware.check_whisper_model(
        model_name, device=device, hw=hardware.probe(fresh=True), models_dir=models_dir
    )
    if verdict["level"] == hardware.NO:
        message = f"Modell '{model_name}' passt nicht in den Speicher. {verdict['message']}"
        _publish_engine_status("error", message)
        raise hardware.InsufficientMemory(message)
    if verdict["level"] == hardware.TIGHT:
        logger.warning("memory is tight for '%s': %s", model_name, verdict["message"])
    return verdict


def unload_model() -> None:
    """Free VRAM/RAM (used later when an LLM shares the GPU)."""
    global _model, _model_key
    with _model_lock:
        _model = None
        _model_key = None
    hardware.invalidate_probe()  # the freed memory is available to the next check
    _publish_engine_status("idle")


# ── transcription job handler ─────────────────────────────────────────


def _store_segments(file_id: int, segments: list[dict[str, Any]]) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments WHERE file_id = ?", (file_id,))
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [(file_id, i, seg["start"], seg["end"], seg["text"]) for i, seg in enumerate(segments)],
        )


def _with_cpu_fallback(run: Callable[[], Any], report: Callable[[int, str], None]) -> Any:
    """Run a transcription callable and survive the two GPU surprises.

    CTranslate2 loads its CUDA libraries and allocates its workspace only at
    inference time, so both a missing cuDNN and a full VRAM show up here rather
    than at load time. Either way the session switches to the CPU and retries
    once — the user is told in the progress line, the job does not fail.

    Out of memory on the *CPU* has nowhere left to go: it becomes an
    `InsufficientMemory` with the numbers, so the job reports a reason instead
    of an allocation traceback.
    """
    global _cuda_broken
    try:
        return run()
    except JobCancelled:
        raise
    except Exception as exc:
        out_of_memory = hardware.is_oom(exc)
        if _active_device == "cpu" or not (out_of_memory or _is_cuda_lib_error(exc)):
            if out_of_memory:
                message = hardware.oom_message("cpu", name=config.get_settings().whisper.model)
                _publish_engine_status("error", message)
                raise hardware.InsufficientMemory(
                    f"{message} Bitte ein kleineres Modell wählen oder Programme schließen."
                ) from exc
            raise
        if out_of_memory:
            logger.warning("GPU memory exhausted (%s) — falling back to CPU", exc)
            detail = hardware.oom_message("gpu")
        else:
            logger.warning("CUDA not usable (%s) — falling back to CPU permanently", exc)
            detail = "GPU-Bibliotheken nicht verfügbar"
        _cuda_broken = True
        unload_model()
        report(0, f"{detail} — weiter auf der CPU ...")
        return run()


def handle_transcribe_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    file_id = job["file_id"]
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise RuntimeError(f"File {file_id} no longer exists")

    audio_path = workspace.file_path(file_row)
    if not audio_path.exists():
        workspace.set_file_status(
            file_id, "failed", error="Audio file is missing from the workspace"
        )
        raise RuntimeError(f"Audio file is missing: {audio_path}")

    workspace.set_file_status(file_id, "transcribing")
    report(0, f"Transkribiere {file_row['filename']} ...")

    overrides = job.get("payload") or {}
    try:
        segments, info, duration = _with_cpu_fallback(
            lambda: _run_transcription(audio_path, file_row, file_id, cancel, report, overrides),
            report,
        )
        _store_segments(file_id, segments)
        workspace.set_file_status(file_id, "done", duration=duration, language=info.language or "")
        transcripts.sync_after_change(file_id)
        _publish_engine_status("ready")
        report(100, f"{file_row['filename']}: fertig ({len(segments)} Segmente)")

        from .pipeline import maybe_enqueue_auto_process
        from .vectorstore import maybe_enqueue_index

        maybe_enqueue_index(file_id, session_id=job.get("session_id") or "")

        maybe_enqueue_auto_process(file_id, session_id=job.get("session_id") or "")
    except JobCancelled:
        raise
    except Exception as exc:
        workspace.set_file_status(file_id, "failed", error=str(exc))
        _publish_engine_status("error", str(exc))
        raise


def transcribe_path(
    audio_path: Path,
    *,
    language: str = "",
    model_override: str = "",
    cancel: threading.Event,
    report: Callable[[int, str], None],
) -> dict[str, Any]:
    """Transcribe a standalone file (public API) — no workspace or file row.

    Returns {"segments": [{start, end, text}], "language": str, "duration": float}.
    """

    def run() -> tuple[list[dict[str, Any]], str, float | None]:
        settings = config.get_settings().whisper
        model = get_model(model_override=model_override)
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language or settings.language or None,
            beam_size=5,
        )
        duration = info.duration or probe_duration(audio_path)
        _publish_engine_status_running(audio_path.name)
        collected: list[dict[str, Any]] = []
        for segment in segments_iter:
            if cancel.is_set():
                _publish_engine_status("ready")
                raise JobCancelled()
            collected.append(
                {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
            )
            if duration:
                percent = min(99, int(segment.end * 100 / duration))
                report(percent, f"{audio_path.name}: {_format_ts(segment.end)}")
        return collected, info.language or "", duration

    segments, detected_language, duration = _with_cpu_fallback(run, report)
    _publish_engine_status("ready")
    return {"segments": segments, "language": detected_language, "duration": duration or 0.0}


def handle_transcribe_range_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Transcribe only [start_s, end_s] of a file and merge the result back."""
    import tempfile

    from . import audio as audio_service

    payload = job.get("payload") or {}
    start_s, end_s = float(payload["start_s"]), float(payload["end_s"])
    file_id = job["file_id"]
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise RuntimeError(f"File {file_id} no longer exists")
    audio_path = workspace.file_path(file_row)
    if not audio_path.exists():
        raise RuntimeError(f"Audio file is missing: {audio_path}")

    report(0, f"{file_row['filename']}: extrahiere {_format_ts(start_s)}–{_format_ts(end_s)}")
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "range.wav"
        audio_service.extract_range(audio_path, start_s, end_s, clip)

        def run() -> list[dict[str, Any]]:
            settings = config.get_settings().whisper
            model = get_model(model_override=payload.get("model", ""))
            segments_iter, _info = model.transcribe(
                str(clip),
                language=payload.get("language") or settings.language or None,
                beam_size=5,
            )
            collected: list[dict[str, Any]] = []
            for segment in segments_iter:
                if cancel.is_set():
                    raise JobCancelled()
                collected.append(
                    {
                        "start": segment.start + start_s,
                        "end": min(segment.end + start_s, end_s),
                        "text": segment.text.strip(),
                    }
                )
                percent = min(99, int(segment.end * 100 / max(end_s - start_s, 0.01)))
                report(percent, f"{file_row['filename']}: {_format_ts(segment.end + start_s)}")
            return collected

        new_segments = _with_cpu_fallback(run, report)

    total = transcripts.replace_range(file_id, start_s, end_s, new_segments)
    workspace.emit_file_update(file_id)
    _publish_engine_status("ready")
    report(100, f"{file_row['filename']}: Abschnitt neu transkribiert ({total} Segmente gesamt)")


def _run_transcription(
    audio_path: Path,
    file_row: dict[str, Any],
    file_id: int,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    overrides: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], Any, float | None]:
    settings = config.get_settings().whisper
    overrides = overrides or {}
    model = get_model(model_override=overrides.get("model", ""))
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=overrides.get("language") or settings.language or None,
        beam_size=5,
    )
    duration = info.duration or probe_duration(audio_path)

    _publish_engine_status_running(file_row["filename"])
    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        if cancel.is_set():
            workspace.set_file_status(file_id, "pending")
            _publish_engine_status("ready")
            raise JobCancelled()
        segments.append({"start": segment.start, "end": segment.end, "text": segment.text.strip()})
        if duration:
            percent = min(99, int(segment.end * 100 / duration))
            report(percent, f"{file_row['filename']}: {_format_ts(segment.end)}")
    return segments, info, duration


def _publish_engine_status_running(filename: str) -> None:
    _publish_engine_status("running", f"Transkribiere {filename}")


def _format_ts(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
