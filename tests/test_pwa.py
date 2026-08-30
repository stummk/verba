"""Frontend consistency: the service worker shell covers every frontend file,
every i18n key used in JS exists in all three catalogs, and styling never
uses fixed px units (rem/em/relative only)."""

from __future__ import annotations

import json
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def shell_paths() -> set[str]:
    source = (FRONTEND / "sw.js").read_text(encoding="utf-8")
    match = re.search(r"const SHELL = \[(.*?)\];", source, re.DOTALL)
    assert match, "SHELL list not found in sw.js"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_service_worker_shell_covers_all_frontend_files():
    expected = {"/", "/styles.css", "/manifest.webmanifest"}
    for folder in ("js", "i18n", "vendor", "icons"):
        for file in (FRONTEND / folder).rglob("*"):
            if file.is_file():
                expected.add("/" + file.relative_to(FRONTEND).as_posix())

    shell = shell_paths()
    missing = expected - shell
    stale = shell - expected
    assert not missing, f"Fehlt in sw.js SHELL: {sorted(missing)}"
    assert not stale, f"In sw.js SHELL, aber nicht auf der Platte: {sorted(stale)}"


def test_service_worker_never_caches_api_paths():
    source = (FRONTEND / "sw.js").read_text(encoding="utf-8")
    assert '"/api"' in source and '"/v1"' in source and '"/ws"' in source


def test_no_px_units_in_frontend_styling():
    """Styling rule: no fixed px — rem/em/relative units only, so the UI
    scales with the user's font size and zoom. Vendor files are exempt
    (third-party code)."""
    px_pattern = re.compile(r"\d*\.?\d+px\b")
    files = [FRONTEND / "styles.css", FRONTEND / "index.html", *(FRONTEND / "js").rglob("*.js")]
    offenders = []
    for file in files:
        for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if px_pattern.search(line):
                offenders.append(f"{file.relative_to(FRONTEND)}:{number}: {line.strip()}")
    assert not offenders, (
        "px-Einheiten gefunden — nur rem/em/relative Werte verwenden:\n" + "\n".join(offenders)
    )


def used_i18n_keys() -> set[str]:
    keys: set[str] = set()
    for js_file in (FRONTEND / "js").rglob("*.js"):
        source = js_file.read_text(encoding="utf-8")
        keys.update(re.findall(r'\bt\(\s*"([a-zA-Z0-9_.]+)"', source))
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    keys.update(re.findall(r'data-i18n(?:-title)?="([a-zA-Z0-9_.]+)"', index))
    return keys


def test_every_used_i18n_key_exists_in_all_catalogs():
    # dynamic keys (built with template literals) are covered by their prefixes
    dynamic_prefixes = ("lang.",)
    used = {k for k in used_i18n_keys() if not k.startswith(dynamic_prefixes)}
    assert used, "no i18n usages found — regex broken?"
    for lang in ("de", "en", "ru"):
        catalog = json.loads((FRONTEND / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = used - set(catalog)
        assert not missing, f"{lang}.json fehlt: {sorted(missing)}"


def test_dashboard_dialogs_are_available_inside_project_card_actions():
    source = (FRONTEND / "js/views/dashboard.js").read_text(encoding="utf-8")
    assert "let renameDialog = null;" in source
    assert "let deleteDialog = null;" in source
    assert "renameDialog.showModal()" in source
    assert "deleteDialog.showModal()" in source
