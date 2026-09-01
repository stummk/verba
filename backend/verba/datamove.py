"""Moving the data directory.

`general.data_dir` is where the database and the logs are *meant* to be —
typically a drive that gets backed up. `general.data_dir_active` is where they
are. The two differ exactly between the moment the setting is saved and the
next start, which is when `apply_pending_move()` reconciles them.

The move waits for a restart because at runtime nothing here can be closed
safely: the database is read by request handlers and job threads, the log
files are open, and on Windows an open file cannot be renamed at all. At
startup — before the first connection, before logging, before any subprocess
exists — none of that is true, so the move is a plain rename.

What moves: `app.db` (with its WAL sidecars), `logs/`, and the workspaces when
they sit at their default location inside the data directory (their absolute
paths in `projects.workspace` are rewritten). What stays with the installation:
settings.json — it is the file that says where the data went —, site-packages,
the downloaded ffmpeg and the model directories. All of those are
re-downloadable and have no place in a backup.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

# The database plus the sidecars SQLite keeps next to it in WAL mode. They are
# moved together: a WAL left behind would take committed transactions with it.
DB_FILES = ("app.db", "app.db-wal", "app.db-shm")


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _inside(child: Path, parent: Path) -> bool:
    """Whether `child` is `parent` or lies below it (case-insensitive on Windows)."""
    normalised = os.path.normcase(str(child))
    root = os.path.normcase(str(parent))
    return normalised == root or normalised.startswith(root.rstrip("\\/") + os.sep)


def _moved_names(settings: config.Settings, source: Path) -> list[str]:
    """The entries of the data directory that travel with it."""
    names = [name for name in DB_FILES if (source / name).exists()]
    if (source / "logs").exists():
        names.append("logs")
    # only the default location: an explicitly configured workspaces directory
    # is the user's choice and has its own move (services.workspace)
    if not settings.general.workspaces_dir and _inside(config.default_workspaces_dir(), source):
        names.append("workspaces")
    return names


# ── planning (what the settings form validates before storing) ────────


def move_plan(target: Path, settings: config.Settings | None = None) -> dict[str, Any]:
    """What a move to `target` would do — without doing anything.

    Called before the setting is stored so an impossible target is refused
    with a reason instead of failing at the next start, where there is no UI
    left to say so.
    """
    settings = settings or config.get_settings()
    source = config.data_dir(settings)
    target = Path(config.normalize_dir(str(target)))
    problems: list[str] = []
    if same_path(target, source):
        return {"source": str(source), "target": str(target), "entries": [], "problems": []}
    if _inside(target, source):
        problems.append("Das Zielverzeichnis darf nicht im aktuellen Datenverzeichnis liegen.")
    if _inside(source, target):
        problems.append("Das Zielverzeichnis darf das aktuelle Datenverzeichnis nicht enthalten.")

    entries = _moved_names(settings, source)
    if target.is_file():
        problems.append("Das Ziel ist eine Datei, kein Verzeichnis.")
    elif target.is_dir():
        if (target / "app.db").exists():
            problems.append("Im Zielverzeichnis liegt bereits eine Verba-Datenbank.")
        taken = [name for name in entries if (target / name).exists()]
        if taken:
            problems.append(
                "Im Zielverzeichnis existieren bereits Einträge mit gleichem Namen: "
                + ", ".join(taken)
            )
    if not problems:
        problems.extend(_writability_problems(target))
    return {"source": str(source), "target": str(target), "entries": entries, "problems": problems}


def _writability_problems(target: Path) -> list[str]:
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".verba-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return [f"Das Zielverzeichnis ist nicht beschreibbar: {exc}"]
    return []


# ── the move itself (startup) ─────────────────────────────────────────


def apply_pending_move() -> dict[str, Any] | None:
    """Reconcile `data_dir` and `data_dir_active`. Call once at startup.

    Idempotent: with the two in agreement it does nothing, which is the normal
    case on every start.
    """
    settings = config.get_settings()
    source = config.data_dir(settings)
    target = config.configured_data_dir(settings)
    if same_path(source, target):
        return None

    plan = move_plan(target, settings)
    if plan["problems"]:
        # the target became unusable between saving the setting and this start
        # (drive not mounted, permissions changed) — keep running where the
        # data is rather than failing to start at all
        logger.error("data directory move to %s skipped: %s", target, " ".join(plan["problems"]))
        print(f"Verba: cannot move the data directory to {target} - {plan['problems'][0]}")
        return None

    print(f"Verba: moving the data directory to {target} ...", flush=True)
    target.mkdir(parents=True, exist_ok=True)
    for name in plan["entries"]:
        shutil.move(str(source / name), str(target / name))

    # only now is the new location the one in use; a crash before this leaves
    # the settings pointing at the old one, and the next start retries.
    # Back at the default location the field goes empty again, so a settings
    # file carries a path only while one is actually configured.
    settings.general.data_dir_active = (
        "" if same_path(target, config.base_data_dir()) else str(target)
    )
    config.save_settings(settings)
    projects = _repoint_projects(source, target)

    print(f"Verba: data directory is now {target}", flush=True)
    return {
        "source": str(source),
        "target": str(target),
        "entries": plan["entries"],
        "projects": projects,
    }


def _repoint_projects(source: Path, target: Path) -> int:
    """Projects store an absolute workspace path — follow the move."""
    from . import db

    if not Path(db.db_path()).exists():
        return 0
    updated = 0
    with db.get_conn() as conn:
        for row in conn.execute("SELECT id, workspace FROM projects").fetchall():
            workspace = Path(row["workspace"])
            if not _inside(workspace, source):
                continue
            rebased = str(target / workspace.relative_to(source))
            conn.execute("UPDATE projects SET workspace = ? WHERE id = ?", (rebased, row["id"]))
            updated += 1
    return updated
