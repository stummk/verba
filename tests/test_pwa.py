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


def test_llama_cpp_can_be_installed_in_the_wizard_and_in_the_settings():
    """Both views mount the shared installer, so both show the same log."""
    component = (FRONTEND / "js" / "llamainstall.js").read_text(encoding="utf-8")
    assert "setup-log" in component, "the installer shows the backend's log"

    wizard = (FRONTEND / "js" / "views" / "setup.js").read_text(encoding="utf-8")
    assert "mountLlamaInstaller" in wizard and "wizard-llm-binary" in wizard
    # the wizard also offers the recommended model, the settings page has the catalog
    assert "withRecommendedModel: true" in wizard
    assert "llmModels.downloadRecommended" in component

    settings = (FRONTEND / "js" / "views" / "settings.js").read_text(encoding="utf-8")
    assert "mountLlamaInstaller" in settings


def test_api_key_label_is_required_and_gates_the_button():
    """A key is only recognisable by its label, so the form insists on one."""
    source = (FRONTEND / "js" / "views" / "settings.js").read_text(encoding="utf-8")
    form = source.split('id="apikey-name"', 1)[1][:400]
    assert "required" in form and 'aria-required="true"' in form
    assert 'class="required-mark"' in source, "the label carries a visible marker"
    # the button starts disabled and follows the field
    assert 'id="apikey-create" disabled' in source
    assert "apiKeyCreate.disabled = !apiKeyName.value.trim();" in source
    assert ".required-mark" in (FRONTEND / "styles.css").read_text(encoding="utf-8")


def test_settings_actions_are_icon_buttons():
    """Opening the documentation and creating a key are icon-only actions."""
    source = (FRONTEND / "js" / "views" / "settings.js").read_text(encoding="utf-8")
    # both views (administrator and personal settings) offer the documentation
    assert source.count('class="btn icon-btn" href="#/docs"') == 2
    assert source.count('iconSvg("help")') == 2
    assert 'class="icon-btn" id="apikey-create"' in source
    assert 'iconSvg("add")' in source
    # an icon-only button still names its action for tooltip and screen reader
    for button in ('href="#/docs"', 'id="apikey-create"'):
        block = source.split(button, 1)[1][:400]
        assert "title=" in block and "aria-label=" in block

    icons = (FRONTEND / "js" / "icons.js").read_text(encoding="utf-8")
    for name in ("help", "add"):
        assert f"  {name}:" in icons, f"icon '{name}' is missing"


def test_the_system_card_offers_the_update_next_to_the_version():
    """The version row carries the button — active only with a newer release."""
    source = (FRONTEND / "js" / "views" / "settings.js").read_text(encoding="utf-8")
    assert 'id="update-current"' in source and 'id="update-install"' in source
    # the button starts disabled and is only enabled by an installable release
    assert 'id="update-install" disabled' in source
    assert "button.disabled = !info.can_install" in source
    # both actions are icon-only, so both name themselves for tooltip and reader
    assert 'class="icon-btn" id="update-install"' in source
    assert 'class="icon-btn" id="update-check"' in source
    for button in ('id="update-install"', 'id="update-check"'):
        block = source.split(button, 1)[1][:300]
        assert "title=" in block and "aria-label=" in block
    assert 'iconSvg("download")' in source and 'iconSvg("refresh")' in source
    # what the installation does is shown while it happens
    assert 'id="update-log"' in source and 'id="update-bar"' in source
    assert 'on("update.progress"' in source
    # and the automatic check is a switch, not a fact of life
    assert 'id="update-auto"' in source
    assert 'updates: { check_enabled: el("update-auto").checked }' in source


def test_only_a_linux_server_sees_the_system_package_button():
    """The row lives in the card but stays hidden until the backend says so."""
    source = (FRONTEND / "js" / "views" / "settings.js").read_text(encoding="utf-8")
    assert 'id="os-section" hidden' in source
    assert "section.hidden = !info.supported" in source
    # one icon button, which names itself for tooltip and screen reader
    assert 'class="icon-btn" id="os-run"' in source
    block = source.split('id="os-run"', 1)[1][:300]
    assert "title=" in block and "aria-label=" in block
    assert 'iconSvg("upgrade")' in source
    # what apt does is shown while it happens
    assert 'id="os-log"' in source
    assert 'on("system.upgrade"' in source


def test_a_new_release_is_announced_where_the_user_looks():
    """A toast while the app is open, a reminder on the start page afterwards."""
    app = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert 'ws.on("update.available"' in app
    assert "app.updateAvailable" in app

    dashboard = (FRONTEND / "js" / "views" / "dashboard.js").read_text(encoding="utf-8")
    assert "systemStatus?.update_available" in dashboard
    # installing belongs to an administrator, so only they are reminded
    assert 'access.user?.role === "admin"' in dashboard
