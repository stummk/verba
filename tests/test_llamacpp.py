from __future__ import annotations

import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from verba import config
from verba.services import llamacpp


def machine(ram_mb: int, vram_mb: int = 0, gpu: str = "") -> dict:
    """A probe result as services/hardware.py reports it (see probe())."""
    return {
        "ram_total_mb": ram_mb,
        "ram_available_mb": ram_mb,
        "gpu_name": gpu,
        "vram_total_mb": vram_mb,
        "vram_free_mb": vram_mb,
    }


def test_recommendation_scales_with_hardware():
    tiny = llamacpp.recommend_model(machine(4000))
    assert tiny["name"] == "Qwen3-1.7B-Q8_0"

    mid = llamacpp.recommend_model(machine(16000))
    assert mid["name"] == "Qwen3-4B-Q4_K_M"

    gpu = llamacpp.recommend_model(machine(16000, vram_mb=12000, gpu="RTX"))
    assert gpu["name"] == "Qwen3-8B-Q4_K_M"


def test_the_recommendation_ignores_unflagged_alternatives():
    """An alternative of the same size must not change what new users get."""
    flagged = [m for m in llamacpp.MODEL_CATALOG if m.get("recommended")]
    alternatives = [m for m in llamacpp.MODEL_CATALOG if not m.get("recommended")]
    assert flagged and alternatives, "the catalog offers alternatives besides the recommended line"
    # a machine that fits everything still gets a recommended line, not the
    # largest alternative that happens to sit last in the list
    everything = llamacpp.recommend_model(machine(64000, vram_mb=48000, gpu="A6000"))
    assert everything.get("recommended") is True
    assert everything["name"] == flagged[-1]["name"]


def test_the_catalog_is_ordered_and_reachable():
    """Ordered by hardware need, and no duplicate names or files."""
    needs = [entry["min_free_mb"] for entry in llamacpp.MODEL_CATALOG]
    assert needs == sorted(needs)
    names = [entry["name"] for entry in llamacpp.MODEL_CATALOG]
    files = [entry["file"] for entry in llamacpp.MODEL_CATALOG]
    assert len(set(names)) == len(names)
    assert len(set(files)) == len(files)
    for entry in llamacpp.MODEL_CATALOG:
        assert entry["url"].startswith("https://huggingface.co/")
        assert entry["url"].endswith(".gguf")
        # gated repositories answer 401 without a token — those cannot be used
        assert "/google/gemma" not in entry["url"]


def test_alternatives_from_another_family_are_offered():
    families = {entry["name"].split("-")[0].lower() for entry in llamacpp.MODEL_CATALOG}
    assert {"qwen3", "gemma"} <= families


def test_probe_hardware_returns_numbers():
    hw = llamacpp.probe_hardware()
    assert hw["ram_total_mb"] > 0  # every dev/CI machine has RAM
    assert hw["ram_total_mb"] >= hw["ram_available_mb"] >= 0
    assert hw["vram_total_mb"] >= 0


def test_delete_model_rejects_traversal():
    with pytest.raises(ValueError):
        llamacpp.delete_model("../app.db")
    with pytest.raises(ValueError):
        llamacpp.delete_model("model.bin")  # only .gguf is allowed


def test_llm_status_endpoint(client):
    data = client.get("/api/models/llm").json()
    assert data["binary_installed"] is False
    assert data["server_running"] is False
    assert len(data["catalog"]) == len(llamacpp.MODEL_CATALOG)
    assert data["recommended"]["name"] in {m["name"] for m in data["catalog"]}
    assert data["installed"] == []
    assert data["models_dir"].endswith("llm")


def test_llm_download_unknown_model_rejected(client):
    response = client.post("/api/models/llm/download", json={"name": "evil/../model"})
    assert response.status_code == 422


def test_process_requires_configured_llm(client, tmp_path):
    project = client.post("/api/projects", json={"name": "P"}).json()
    response = client.post(f"/api/projects/{project['id']}/process", json={"steps": ["cleanup"]})
    assert response.status_code == 409  # llm mode is "none" by default


def test_queue_endpoint(client):
    data = client.get("/api/jobs/queue").json()
    assert data["llm_location"] == "none"
    assert data["lanes"] == {"main": [], "llm": []}


# ── the models directory ──────────────────────────────────────────────


def test_a_configured_directory_is_used_as_is(tmp_path):
    """An existing GGUF collection is used from where it is — no copying."""
    collection = tmp_path / "F" / "Models" / "llm"
    collection.mkdir(parents=True)
    (collection / "mein-modell-Q4_K_M.gguf").write_bytes(b"gguf")

    settings = config.get_settings()
    settings.llm.models_dir = str(collection)
    config.save_settings(settings)

    assert llamacpp.llm_models_dir() == collection
    assert [m["file"] for m in llamacpp.list_installed_models()] == ["mein-modell-Q4_K_M.gguf"]
    assert llamacpp._pick_model_file() == collection / "mein-modell-Q4_K_M.gguf"
    # nothing was copied into the default directory
    assert not list(config.default_llm_models_dir().glob("*.gguf"))


def test_the_configured_model_is_taken_from_that_directory(tmp_path):
    collection = tmp_path / "gguf"
    collection.mkdir()
    for name in ("a-Q4.gguf", "b-Q4.gguf"):
        (collection / name).write_bytes(b"gguf")

    settings = config.get_settings()
    settings.llm.models_dir = str(collection)
    settings.llm.model = "b-Q4.gguf"
    config.save_settings(settings)

    assert llamacpp._pick_model_file() == collection / "b-Q4.gguf"


def test_a_relative_directory_is_stored_absolute():
    settings = config.Settings.model_validate({"llm": {"models_dir": "modelle"}})
    assert Path(settings.llm.models_dir).is_absolute()


def test_delete_stays_inside_the_configured_directory(tmp_path):
    """A traversal attempt is stripped to a plain name, never followed."""
    collection = tmp_path / "gguf"
    collection.mkdir()
    outsider = tmp_path / "elsewhere.gguf"
    outsider.write_bytes(b"gguf")
    settings = config.get_settings()
    settings.llm.models_dir = str(collection)
    config.save_settings(settings)

    llamacpp.delete_model("../elsewhere.gguf")

    assert outsider.is_file(), "the file outside the models directory must survive"
    with pytest.raises(ValueError):
        llamacpp.delete_model("../app.db")  # only .gguf at all


# ── the release binary ────────────────────────────────────────────────

#: Asset names of a real llama.cpp nightly build (b10621) — the picker has to
#: find its platform in this list, which mixes CPU, CUDA, ROCm, SYCL, Vulkan
#: and OpenVINO builds for two architectures.
RELEASE_ASSETS = [
    "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "cudart-llama-bin-win-cuda-13.3-x64.zip",
    "cudart-llama-bin-win-cuda-13.4-arm64.zip",
    "llama-b10621-bin-android-arm64.tar.gz",
    "llama-b10621-bin-macos-arm64.tar.gz",
    "llama-b10621-bin-macos-x64.tar.gz",
    "llama-b10621-bin-ubuntu-arm64.tar.gz",
    "llama-b10621-bin-ubuntu-rocm-7.14-x64.tar.gz",
    "llama-b10621-bin-ubuntu-s390x.tar.gz",
    "llama-b10621-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b10621-bin-ubuntu-x64.tar.gz",
    "llama-b10621-bin-win-cpu-arm64.zip",
    "llama-b10621-bin-win-cpu-x64.zip",
    "llama-b10621-bin-win-cuda-12.4-x64.zip",
    "llama-b10621-bin-win-cuda-13.3-x64.zip",
    "llama-b10621-bin-win-cuda-13.4-arm64.zip",
    "llama-b10621-bin-win-vulkan-x64.zip",
    "llama-b10621-ui.tar.gz",
    "llama-b10621-xcframework.zip",
]


def assets(names: list[str]) -> list[dict]:
    return [
        {"name": name, "size": 1024, "browser_download_url": f"https://example/{name}"}
        for name in names
    ]


def on_platform(monkeypatch, system: str, machine: str = "x86_64", gpu: bool = False) -> None:
    monkeypatch.setattr(llamacpp.platform, "system", lambda: system)
    monkeypatch.setattr(llamacpp.platform, "machine", lambda: machine)
    monkeypatch.setattr(llamacpp.hardware, "has_gpu", lambda *a, **k: gpu)


@pytest.mark.parametrize(
    ("system", "machine", "gpu", "expected"),
    [
        # the Linux server case: the plain CPU build, not s390x, not Vulkan
        ("Linux", "x86_64", False, "llama-b10621-bin-ubuntu-x64.tar.gz"),
        ("Linux", "x86_64", True, "llama-b10621-bin-ubuntu-x64.tar.gz"),
        ("Linux", "aarch64", False, "llama-b10621-bin-ubuntu-arm64.tar.gz"),
        ("Windows", "AMD64", False, "llama-b10621-bin-win-cpu-x64.zip"),
        ("Windows", "AMD64", True, "llama-b10621-bin-win-cuda-12.4-x64.zip"),
        ("Windows", "ARM64", True, "llama-b10621-bin-win-cpu-arm64.zip"),
        ("Darwin", "arm64", False, "llama-b10621-bin-macos-arm64.tar.gz"),
        ("Darwin", "x86_64", False, "llama-b10621-bin-macos-x64.tar.gz"),
    ],
)
def test_the_release_asset_matches_the_platform(monkeypatch, system, machine, gpu, expected):
    on_platform(monkeypatch, system, machine, gpu)
    picked = llamacpp._pick_release_asset(assets(RELEASE_ASSETS))
    assert picked is not None and picked["name"] == expected


def test_a_cuda_build_of_another_architecture_is_not_taken(monkeypatch):
    """With the x64 CUDA builds gone, the arm64 one must not be installed."""
    on_platform(monkeypatch, "Windows", "AMD64", gpu=True)
    remaining = [name for name in RELEASE_ASSETS if "cuda-12" not in name]
    remaining = [name for name in remaining if "cuda-13.3" not in name]
    picked = llamacpp._pick_release_asset(assets(remaining))
    assert picked is not None and picked["name"] == "llama-b10621-bin-win-cpu-x64.zip"


def test_the_cuda_runtime_belongs_to_the_cuda_build():
    binary = "llama-b10621-bin-win-cuda-12.4-x64.zip"
    cudart = llamacpp._pick_cudart_asset(assets(RELEASE_ASSETS), binary)
    assert cudart is not None and cudart["name"] == "cudart-llama-bin-win-cuda-12.4-x64.zip"
    # a CPU build needs no runtime archive
    assert llamacpp._pick_cudart_asset(assets(RELEASE_ASSETS), "llama-bin-win-cpu-x64.zip") is None


def test_a_platform_without_a_build_is_refused(monkeypatch):
    on_platform(monkeypatch, "Linux", "s390x")
    assert llamacpp._pick_release_asset(assets(RELEASE_ASSETS)) is None


def _release(tag: str, names: list[str]) -> dict:
    return {"tag_name": tag, "assets": assets(names)}


def test_the_nightly_pointer_is_followed(monkeypatch):
    """`releases/latest` only names the nightly build that carries the binaries."""
    on_platform(monkeypatch, "Linux", "x86_64")
    pages = {
        llamacpp.RELEASE_API: _release("v0.3.0", [llamacpp.NIGHTLY_POINTER]),
        llamacpp.RELEASE_TAG_API.format(tag="b10621"): _release("b10621", RELEASE_ASSETS),
    }
    monkeypatch.setattr(llamacpp, "_get_json", lambda url: pages[url])
    monkeypatch.setattr(llamacpp, "_fetch_text", lambda url: "b10621\n")

    release, asset = llamacpp.resolve_release()

    assert release["tag_name"] == "b10621"
    assert asset["name"] == "llama-b10621-bin-ubuntu-x64.tar.gz"


def test_the_release_list_is_the_last_resort(monkeypatch):
    """Neither the semver release nor the nightly it names has an asset."""
    on_platform(monkeypatch, "Linux", "x86_64")
    pages = {
        llamacpp.RELEASE_API: _release("v0.3.0", [llamacpp.NIGHTLY_POINTER]),
        llamacpp.RELEASE_TAG_API.format(tag="b99999"): _release("b99999", ["llama-ui.tar.gz"]),
        llamacpp.RELEASE_LIST_API: [
            _release("b99998", ["llama-ui.tar.gz"]),
            _release("b10621", RELEASE_ASSETS),
        ],
    }
    monkeypatch.setattr(llamacpp, "_get_json", lambda url: pages[url])
    monkeypatch.setattr(llamacpp, "_fetch_text", lambda url: "b99999")

    release, asset = llamacpp.resolve_release()

    assert release["tag_name"] == "b10621"
    assert asset["name"] == "llama-b10621-bin-ubuntu-x64.tar.gz"


def test_a_release_without_any_build_for_this_system_raises(monkeypatch):
    on_platform(monkeypatch, "Linux", "s390x")
    pages = {
        llamacpp.RELEASE_API: _release("b1", RELEASE_ASSETS),
        llamacpp.RELEASE_LIST_API: [_release("b1", RELEASE_ASSETS)],
    }
    monkeypatch.setattr(llamacpp, "_get_json", lambda url: pages[url])
    monkeypatch.setattr(llamacpp, "_fetch_text", lambda url: "")
    with pytest.raises(RuntimeError, match="kein llama.cpp-Release"):
        llamacpp.resolve_release()


def test_the_newest_installed_binary_is_used(monkeypatch, tmp_path):
    """After an update two build directories lie side by side."""
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llamacpp, "binary_dir", lambda: tmp_path)
    assert llamacpp.server_binary() is None

    old = tmp_path / "llama-b10000" / "llama-server"
    new = tmp_path / "llama-b10621" / "llama-server"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x7fELF")
    import os

    os.utime(old, (1, 1))

    assert llamacpp.server_binary() == new


def test_a_loader_failure_is_explained_in_german(monkeypatch):
    """A distribution too old for the official build says so, in the UI language."""
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Linux")
    too_old = llamacpp._loader_failure(
        1, "llama-server: /lib/libm.so.6: version `GLIBC_2.38' not found"
    )
    assert "glibc 2.34" in too_old and "Debian 12" in too_old

    outdated_libstdcxx = llamacpp._loader_failure(
        1, "/lib/x86_64-linux-gnu/libstdc++.so.6: version `GLIBCXX_3.4.29' not found"
    )
    assert "Debian 12" in outdated_libstdcxx  # too old, not missing

    missing = llamacpp._loader_failure(
        127, "llama-server: error while loading shared libraries: libstdc++.so.6"
    )
    assert "libstdc++6" in missing

    assert llamacpp._loader_failure(0, "version: 6789 (abc123)") == ""


def test_a_missing_dll_is_named(monkeypatch):
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Windows")
    assert "DLL" in llamacpp._loader_failure(0xC0000135, "")


def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _single_entry_catalog(monkeypatch, url: str) -> dict:
    entry = {
        "name": "Test-Model-Q4",
        "file": "test-model-Q4.gguf",
        "url": url,
        "size_mb": 1,
        "min_free_mb": 100,
        "label": "Testmodell",
    }
    monkeypatch.setattr(llamacpp, "MODEL_CATALOG", [entry])
    return entry


def test_a_model_download_lands_in_the_models_directory(http_source, monkeypatch, tmp_path):
    http_source.handler.payload = b"GGUF" + b"\x00" * 2048
    http_source.handler.drop_first = True  # and survives a dropped connection
    entry = _single_entry_catalog(monkeypatch, http_source.url)
    monkeypatch.setattr(llamacpp, "llm_models_dir", lambda: tmp_path)

    assert llamacpp.start_model_download(entry["name"]) is True
    target = tmp_path / entry["file"]

    assert _wait_for(target.exists), "the download did not finish"
    assert target.read_bytes() == http_source.handler.payload
    assert not (tmp_path / "test-model-Q4.part").exists()
    assert [m["file"] for m in llamacpp.list_installed_models()] == [entry["file"]]


def test_an_error_page_is_not_kept_as_a_model(http_source, monkeypatch, tmp_path):
    http_source.handler.payload = b"<html>404</html>"
    entry = _single_entry_catalog(monkeypatch, http_source.url)
    monkeypatch.setattr(llamacpp, "llm_models_dir", lambda: tmp_path)

    llamacpp.start_model_download(entry["name"])

    assert _wait_for(lambda: entry["name"] not in llamacpp._downloads_running)
    assert not (tmp_path / entry["file"]).exists()
    assert not (tmp_path / "test-model-Q4.part").exists()
    assert llamacpp.list_installed_models() == []


def test_every_catalog_model_stays_under_the_download_limit():
    for entry in llamacpp.MODEL_CATALOG:
        assert entry["size_mb"] * 1024 * 1024 < llamacpp.MAX_MODEL_BYTES


# ── unpacking the release archive ─────────────────────────────────────


def _tar_release(path: Path) -> None:
    """A Linux release archive: llama-server plus the libraries next to it."""
    build = path.parent / "build" / "llama-b1"
    build.mkdir(parents=True, exist_ok=True)
    (build / "llama-server").write_bytes(b"\x7fELF fake")
    (build / "libggml.so").write_bytes(b"\x7fELF lib")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(build, arcname="llama-b1")


def _zip_release(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("llama-server.exe", "MZ fake")
        zf.writestr("ggml.dll", "MZ lib")


@pytest.mark.parametrize(
    ("system", "make", "archive_name", "exe"),
    [
        ("Linux", _tar_release, "llama-b1-bin-ubuntu-x64.tar.gz", "llama-server"),
        ("Windows", _zip_release, "llama-b1-bin-win-cpu-x64.zip", "llama-server.exe"),
    ],
)
def test_the_release_archive_is_unpacked_and_the_server_found(
    http_source, monkeypatch, tmp_path, system, make, archive_name, exe
):
    archive = tmp_path / archive_name
    make(archive)
    http_source.handler.payload = archive.read_bytes()
    dest = tmp_path / "tools" / "llama"
    monkeypatch.setattr(llamacpp.platform, "system", lambda: system)
    monkeypatch.setattr(llamacpp, "binary_dir", lambda: dest)
    monkeypatch.setattr(llamacpp, "_verify_binary", lambda binary: None)
    monkeypatch.setattr(
        llamacpp,
        "resolve_release",
        lambda: (
            {"tag_name": "b1", "assets": []},
            {
                "name": archive_name,
                "size": len(http_source.handler.payload),
                "browser_download_url": http_source.url,
            },
        ),
    )

    installed = Path(llamacpp.install_binary())

    assert installed.name == exe
    assert installed.is_file()
    # the archive itself is gone, the libraries the binary loads sit next to it
    assert not list(dest.rglob("*.tar.gz")) and not list(dest.rglob("*.zip"))
    assert list(installed.parent.glob("*ggml*"))
    # a second call reuses the installation instead of downloading again
    assert Path(llamacpp.install_binary()) == installed


# ── missing system libraries are installed ────────────────────────────


@pytest.fixture
def package_manager(monkeypatch):
    """A Linux with apt-get whose commands are recorded instead of run."""
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        llamacpp,
        "_package_manager",
        lambda: llamacpp._Manager("apt", ["apt-get", "install", "-y"], ["apt-get", "update"]),
    )
    state = SimpleNamespace(commands=[], fails=set())

    def run(cmd, timeout=None):
        state.commands.append(cmd)
        return not any(part in state.fails for part in cmd)

    monkeypatch.setattr(llamacpp, "_run_privileged", run)
    return state


def test_the_package_carrying_a_missing_library_is_installed(package_manager):
    installed = llamacpp._install_system_libraries(["libgomp.so.1"], lambda *a: None)

    assert installed == ["libgomp1"]
    assert package_manager.commands == [["apt-get", "install", "-y", "libgomp1"]]


def test_a_renamed_package_is_tried_after_a_refresh(package_manager):
    """Debian 13 and Ubuntu 24.04 call OpenSSL 3 libssl3t64, older ones libssl3."""
    package_manager.fails = {"libssl3"}

    installed = llamacpp._install_system_libraries(["libssl.so.3"], lambda *a: None)

    assert installed == ["libssl3t64"]
    assert ["apt-get", "update"] in package_manager.commands


def test_only_known_libraries_reach_the_package_manager(package_manager):
    """The library name comes from parsed output — the package name never does."""
    assert llamacpp._install_system_libraries(["libevil.so.1"], lambda *a: None) == []
    assert package_manager.commands == []


def test_nothing_is_installed_without_root(monkeypatch):
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llamacpp, "_root_prefix", lambda: None)
    monkeypatch.setattr(llamacpp, "_run_privileged", lambda *a, **k: pytest.fail("ran as user"))

    assert llamacpp._install_system_libraries(["libgomp.so.1"], lambda *a: None) == []


def test_root_needs_no_sudo(monkeypatch):
    monkeypatch.setattr(llamacpp.os, "geteuid", lambda: 0, raising=False)
    assert llamacpp._root_prefix() == []


def test_a_windows_install_never_touches_a_package_manager(monkeypatch):
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llamacpp, "_package_manager", lambda: pytest.fail("looked for apt"))

    assert llamacpp._install_system_libraries(["libgomp.so.1"], lambda *a: None) == []


def _failing_verify(*failures: list[str]):
    """A _verify_binary that reports the given loader failures, then succeeds."""
    pending = list(failures)

    def verify(binary):
        if pending:
            missing = pending.pop(0)
            raise llamacpp._LoaderFailure("llama-server startet nicht", missing)

    return verify


def test_the_loader_names_one_library_per_attempt(monkeypatch, package_manager, tmp_path):
    """Install, run again, install what the next attempt reports."""
    monkeypatch.setattr(
        llamacpp, "_verify_binary", _failing_verify(["libgomp.so.1"], ["libssl.so.3"])
    )

    llamacpp._ensure_loadable(tmp_path / "llama-server", lambda *a: None)

    assert [cmd[-1] for cmd in package_manager.commands] == ["libgomp1", "libssl3"]


def test_a_library_that_is_only_too_old_is_not_installed(monkeypatch, package_manager, tmp_path):
    """No package fixes a libstdc++ that is present but older than the build."""
    monkeypatch.setattr(llamacpp, "_verify_binary", _failing_verify([]))

    with pytest.raises(llamacpp._LoaderFailure):
        llamacpp._ensure_loadable(tmp_path / "llama-server", lambda *a: None)

    assert package_manager.commands == []


def test_an_installation_that_stays_broken_is_reported(monkeypatch, package_manager, tmp_path):
    """The package installs, the binary still does not run — that is a failure."""
    always = ["libgomp.so.1"]
    monkeypatch.setattr(llamacpp, "_verify_binary", _failing_verify(*[always] * 12))

    with pytest.raises(llamacpp._LoaderFailure):
        llamacpp._ensure_loadable(tmp_path / "llama-server", lambda *a: None)


def test_the_hint_names_the_packages_an_admin_has_to_install(monkeypatch):
    monkeypatch.setattr(llamacpp.platform, "system", lambda: "Linux")
    message = llamacpp._loader_failure(
        127, "llama-server: error while loading shared libraries: libgomp.so.1: cannot open"
    )
    assert "apt install libgomp1" in message


def test_the_installation_survives_a_repaired_dependency(
    http_source, monkeypatch, package_manager, tmp_path
):
    """End to end: the archive stays installed once the library is there."""
    archive = tmp_path / "llama-b1-bin-ubuntu-x64.tar.gz"
    _tar_release(archive)
    http_source.handler.payload = archive.read_bytes()
    dest = tmp_path / "tools" / "llama"
    monkeypatch.setattr(llamacpp, "binary_dir", lambda: dest)
    monkeypatch.setattr(llamacpp, "_verify_binary", _failing_verify(["libgomp.so.1"]))
    monkeypatch.setattr(
        llamacpp,
        "resolve_release",
        lambda: (
            {"tag_name": "b1", "assets": []},
            {
                "name": archive.name,
                "size": len(http_source.handler.payload),
                "browser_download_url": http_source.url,
            },
        ),
    )

    installed = Path(llamacpp.install_binary())

    assert installed.is_file()
    assert [cmd[-1] for cmd in package_manager.commands] == ["libgomp1"]


def test_an_unrepairable_installation_is_removed(
    http_source, monkeypatch, package_manager, tmp_path
):
    """Nothing may stay behind that looks like a working llama.cpp."""
    archive = tmp_path / "llama-b1-bin-ubuntu-x64.tar.gz"
    _tar_release(archive)
    http_source.handler.payload = archive.read_bytes()
    dest = tmp_path / "tools" / "llama"
    monkeypatch.setattr(llamacpp, "binary_dir", lambda: dest)
    monkeypatch.setattr(llamacpp, "_verify_binary", _failing_verify([]))
    monkeypatch.setattr(
        llamacpp,
        "resolve_release",
        lambda: (
            {"tag_name": "b1", "assets": []},
            {
                "name": archive.name,
                "size": len(http_source.handler.payload),
                "browser_download_url": http_source.url,
            },
        ),
    )

    with pytest.raises(RuntimeError):
        llamacpp.install_binary()

    assert llamacpp.server_binary() is None
    assert not dest.exists()


# ── the installation log the UI shows ─────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_install_state():
    """Every test starts with an empty installation log."""
    llamacpp._install_state.update(running=False, percent=0, detail="", error="", log=[])
    yield


def test_the_status_carries_an_idle_installation(client):
    install = client.get("/api/models/llm").json()["install"]
    assert install == {"running": False, "percent": 0, "detail": "", "error": "", "log": []}


def test_every_step_becomes_a_log_line():
    llamacpp._install_event(0, "Suche aktuelles llama.cpp-Release ...")
    llamacpp._install_event(10, "Lade llama.cpp ...")
    llamacpp._install_event(20, "Lade llama.cpp ...")  # the percent ticks repeat
    llamacpp._install_event(100, "llama.cpp installiert: /opt/llama", state="done")

    state = llamacpp.install_state()
    assert state["log"] == [
        "Suche aktuelles llama.cpp-Release ...",
        "Lade llama.cpp ...",
        "llama.cpp installiert: /opt/llama",
    ]
    assert state["running"] is False
    assert state["percent"] == 100


def test_the_log_reaches_the_ui_with_every_event(monkeypatch):
    events = []
    monkeypatch.setattr(llamacpp.hub, "publish", lambda name, data: events.append((name, data)))

    llamacpp._install_event(0, "Entpacke llama.cpp ...")
    llamacpp._install_event(0, "Installiere fehlendes Systempaket libgomp1 (apt) ...")

    assert [name for name, _ in events] == ["model.download", "model.download"]
    assert events[-1][1]["scope"] == "llm-binary"
    assert events[-1][1]["log"] == [
        "Entpacke llama.cpp ...",
        "Installiere fehlendes Systempaket libgomp1 (apt) ...",
    ]


def test_the_log_does_not_grow_without_bound():
    for number in range(llamacpp._INSTALL_LOG_LINES + 50):
        llamacpp._install_event(0, f"Schritt {number}")
    log = llamacpp.install_state()["log"]
    assert len(log) == llamacpp._INSTALL_LOG_LINES
    assert log[-1] == f"Schritt {llamacpp._INSTALL_LOG_LINES + 49}"


def test_a_failed_installation_ends_in_the_log(monkeypatch):
    """The wizard shows why it did not work, in German and without a reload."""
    monkeypatch.setattr(
        llamacpp,
        "resolve_release",
        lambda: (_ for _ in ()).throw(RuntimeError("Für dieses System gibt es kein Release")),
    )

    assert llamacpp.start_binary_install() is True
    assert _wait_for(lambda: not llamacpp.install_state()["running"])

    state = llamacpp.install_state()
    assert "kein Release" in state["error"]
    assert state["log"][-1] == state["error"]


def test_a_second_install_starts_with_an_empty_log(monkeypatch):
    llamacpp._install_event(0, "alter Lauf")
    monkeypatch.setattr(llamacpp, "install_binary", lambda: "/opt/llama-server")

    assert llamacpp.start_binary_install() is True
    assert _wait_for(lambda: not llamacpp.install_state()["running"])

    assert "alter Lauf" not in llamacpp.install_state()["log"]


def test_the_log_tells_the_whole_story(http_source, monkeypatch, package_manager, tmp_path):
    """Release, package, unpacking, the repaired library and the version."""
    archive = tmp_path / "llama-b1-bin-ubuntu-x64.tar.gz"
    _tar_release(archive)
    http_source.handler.payload = archive.read_bytes()
    monkeypatch.setattr(llamacpp, "binary_dir", lambda: tmp_path / "tools" / "llama")
    verify = _failing_verify(["libgomp.so.1"])
    monkeypatch.setattr(
        llamacpp,
        "_verify_binary",
        lambda binary: verify(binary) or "version: 0.3.0-dev (build 10621)",
    )
    monkeypatch.setattr(
        llamacpp,
        "resolve_release",
        lambda: (
            {"tag_name": "b1", "assets": []},
            {
                "name": archive.name,
                "size": len(http_source.handler.payload),
                "browser_download_url": http_source.url,
            },
        ),
    )

    llamacpp.install_binary()

    log = "\n".join(llamacpp.install_state()["log"])
    assert "Release b1, Paket llama-b1-bin-ubuntu-x64.tar.gz" in log
    assert "Entpacke" in log
    assert "libgomp.so.1" in log and "libgomp1" in log
    assert "version: 0.3.0-dev (build 10621)" in log
    assert "llama.cpp installiert:" in log
    assert llamacpp.install_state()["percent"] == 100
