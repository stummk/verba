"""Frontend consistency: the service worker shell covers every frontend file,
every view stops once its navigation was overtaken, every i18n key used in JS
exists in all three catalogs, and styling never uses fixed px units
(rem/em/relative only)."""

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


def test_views_stop_when_their_navigation_was_overtaken():
    """Every view renders asynchronously and writes into the one #view element
    itself, so the router cannot keep an overtaken render from painting over
    the view that replaced it — the view has to stop by itself: take
    viewGuard() (frontend/js/navigation.js) before the first await and ask it
    afterwards."""
    for file in sorted((FRONTEND / "js" / "views").glob("*.js")):
        source = file.read_text(encoding="utf-8")
        if "export async function render" not in source:
            continue  # a view without awaits cannot be overtaken
        match = re.search(r"const (\w+) = viewGuard\(\);", source)
        assert match, f"{file.name}: render() nimmt kein viewGuard()"
        assert f"!{match.group(1)}()" in source, (
            f"{file.name}: viewGuard() genommen, aber nie geprüft"
        )


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
    # the guide is a document, not a "?" — that glyph now opens an explanation
    assert source.count('iconSvg("article")') == 2
    assert 'iconSvg("help")' not in source
    assert 'class="icon-btn" id="apikey-create"' in source
    assert 'iconSvg("add")' in source
    # an icon-only button still names its action for tooltip and screen reader
    for button in ('href="#/docs"', 'id="apikey-create"'):
        block = source.split(button, 1)[1][:400]
        assert "title=" in block and "aria-label=" in block

    icons = (FRONTEND / "js" / "icons.js").read_text(encoding="utf-8")
    for name in ("article", "add", "help"):
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
    assert 'checkLine("update-auto"' in source
    assert 'updates: { check_enabled: el("update-auto").checked }' in source


def test_the_type_form_offers_the_page_break_per_section():
    """A layout choice of the type, next to the structure it belongs to."""
    source = (FRONTEND / "js" / "views" / "types.js").read_text(encoding="utf-8")
    assert 'checkLine("type-keep-sections"' in source
    assert 't("types.keepSections")' in source and 't("types.keepSectionsHint")' in source
    # and it is part of what a type saves
    assert "keep_sections: draft.keep_sections" in source


def test_the_type_form_offers_the_verbatim_switch():
    """Whether the export may restructure the text is a choice of the type."""
    source = (FRONTEND / "js" / "views" / "types.js").read_text(encoding="utf-8")
    assert 'checkLine("type-verbatim"' in source
    assert 't("types.verbatim")' in source and 't("types.verbatimHint")' in source
    assert "verbatim: draft.verbatim" in source
    # a verbatim type never reaches the output prompt — the hint has to say so
    assert 't("types.promptOutputUnused")' in source


def test_the_type_form_offers_the_cleanup_mode_of_its_own():
    """What the aufbereitung does is a second choice, next to the export one:
    a type may have its recording rewritten and still be exported verbatim."""
    source = (FRONTEND / "js" / "views" / "types.js").read_text(encoding="utf-8")
    assert 'checkLine("type-condense"' in source
    assert 't("types.condense")' in source and 't("types.condenseHint")' in source
    assert "condense: draft.condense" in source
    # and the cleanup prompt says which of the two roles it currently has
    assert 't("types.promptHintDocument")' in source
    assert 't("types.promptHintChunks")' in source


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
    # dist-upgrade and autoremove may remove packages: a switch, and off
    assert 'checkLine("os-full"' in source
    assert 'api.startOsUpdate(el("os-full").checked)' in source
    assert 'checked ? t("osUpdate.runFull") : t("osUpdate.run")' in source


def test_a_new_release_is_announced_where_the_user_looks():
    """A toast while the app is open, a reminder on the start page afterwards."""
    app = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert 'ws.on("update.available"' in app
    assert "app.updateAvailable" in app

    dashboard = (FRONTEND / "js" / "views" / "dashboard.js").read_text(encoding="utf-8")
    assert "systemStatus?.update_available" in dashboard
    # installing belongs to an administrator, so only they are reminded
    assert 'access.user?.role === "admin"' in dashboard


def test_the_file_list_is_a_list_of_cards():
    """No table any more: a card per file, and the card is the link into it."""
    source = (FRONTEND / "js" / "views" / "project.js").read_text(encoding="utf-8")
    assert "<table" not in source and "filetable" not in source
    assert 'class="file-cards"' in source
    assert 'card.className = "card file-card"' in source
    # the link is a layer over the card, never a wrapper around its controls:
    # cancelling a click inside an <a> would also cancel the checkbox's tick
    assert 'overlay.className = "file-card-overlay"' in source
    assert "overlay.href = `#/editor/${fileRow.id}`" in source
    # the multi-selection stays, in the corner of every card
    assert 'select.className = "row-select file-card-select"' in source
    assert 'id="file-select-all"' in source


def test_every_file_card_shows_all_its_steps_as_badges():
    """Always all of them, so a card says what is missing, not only what is done."""
    steps = (FRONTEND / "js" / "file-steps.js").read_text(encoding="utf-8")
    for key, icon in (
        ("transcribe", "audioToText"),
        ("cleanup", "spellcheck"),
        ("translation", "translate"),
        ("index", "search"),
    ):
        assert f'{{ key: "{key}", icon: "{icon}" }}' in steps
    # the search index leaves no mark on the file status — the chunk count is
    # the only thing that says whether this file can be found
    assert "fileRow.index_chunks" in steps

    # the state is the whole message: grey / blue ring / green / red
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    for state in ("idle", "queued", "running", "done", "failed"):
        assert f".step-{state}" in css, f"the badge state '{state}' has no styling"
    assert "conic-gradient(" in css, "the progress of a step is the ring itself"

    # and the text of a badge lives in its tooltip, in all three catalogs
    keys = {
        f"fileStep.{name}"
        for name in (
            "transcribe",
            "cleanup",
            "translation",
            "index",
            "idle",
            "queued",
            "queuedAt",
            "running",
            "runningPlain",
            "done",
            "failed",
        )
    }
    for lang in ("de", "en", "ru"):
        catalog = json.loads((FRONTEND / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert not keys - set(catalog), f"{lang}.json fehlt: {sorted(keys - set(catalog))}"


def test_the_actions_of_a_file_live_in_an_overflow_menu():
    """Icon plus label per entry — an icon-only row of six said nothing."""
    menu = (FRONTEND / "js" / "menu.js").read_text(encoding="utf-8")
    assert 'iconSvg("moreVert")' in menu
    assert "item.innerHTML = iconSvg(icon)" in menu and "textContent: itemLabel" in menu
    # Escape, a click elsewhere and scrolling all close it again
    assert 'event.key === "Escape"' in menu
    assert 'window.addEventListener("scroll", closeOpenMenu' in menu

    source = (FRONTEND / "js" / "views" / "project.js").read_text(encoding="utf-8")
    assert "overflowMenu({" in source
    for label in (
        't("project.transcribe")',
        't("ai.title")',
        't("project.openEditor")',
        't("export.file")',
        't("common.delete")',
    ):
        assert label in source
    # stopping a running step stays on the card: it is the urgent one
    assert 'stop.classList.add("file-card-stop")' in source
    assert "stop.hidden = !isBusy(fileRow)" in source


def test_the_step_badges_keep_their_place_while_the_stop_button_comes_and_goes():
    """The badges sit one row lower, at the right edge of the card.

    They used to share the corner with the stop button, which appears with a
    running step and disappears with it — and shifted all three badges every
    time. Two rules keep them still: the card is a grid whose lower rows span
    past the actions column to its own edge, and that column holds the stop
    button's place open while it is hidden.
    """
    source = (FRONTEND / "js" / "views" / "project.js").read_text(encoding="utf-8")
    # the badges belong to the meta line, next to the language and the duration
    assert "meta.append(stepBadges(fileRow, badgeContext(fileRow)));" in source
    assert "card.append(select, title, actions, meta);" in source

    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    card = css.split(".file-card {", 1)[1].split("}", 1)[0]
    assert "display: grid" in card
    assert ".file-card-meta { grid-column: 2 / -1;" in css
    assert ".file-card-error { grid-column: 2 / -1;" in css
    # the last block is the one that lays the column out (the first only places it)
    actions = css.rsplit(".file-card-actions {", 1)[1].split("}", 1)[0]
    assert "min-width:" in actions, "the stop button's slot stays reserved"


def test_descriptions_hang_on_a_question_mark_instead_of_filling_the_form():
    """An explanation belongs to its label, not between the fields.

    The texts are unchanged — they moved into the bubble that `js/help.js`
    opens on hover, focus or a tap, so a settings card shows fields again
    instead of three screens of prose.
    """
    help_js = (FRONTEND / "js" / "help.js").read_text(encoding="utf-8")
    # the "?" carries its text and names itself for tooltip and screen reader
    assert 'class="help-btn"' in help_js and "data-help=" in help_js
    assert 't("common.help")' in help_js
    assert 'iconSvg("help")' in help_js
    # one bubble for the whole app, on the body: no card can clip it
    assert 'className = "help-bubble"' in help_js
    assert 'setAttribute("role", "tooltip")' in help_js
    assert "document.body.appendChild(bubble)" in help_js
    # a finger has no hover, so the tap has to open it as well
    assert '"pointerover"' in help_js and '"click"' in help_js and '"focusin"' in help_js

    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".help-btn" in styles and ".help-bubble" in styles and ".label-row" in styles

    # the descriptions the screens complained about are gone from the markup
    for view, keys in {
        "settings.js": (
            "settings.audioBackupHint",
            "settings.embeddingModelHint",
            "settings.embeddingsDirHint",
            "settings.dataDirHint",
        ),
        "types.js": (
            "types.structureHint",
            "types.verbatimHint",
            "types.condenseHint",
            "types.keepSectionsHint",
        ),
    }.items():
        source = (FRONTEND / "js" / "views" / view).read_text(encoding="utf-8")
        for key in keys:
            assert f'<p class="hint">${{t("{key}")}}</p>' not in source, key
            assert f't("{key}")' in source, key
