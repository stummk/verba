"""The installation ladder: llama.cpp is installed so that it uses the GPU.

Nothing here downloads, unpacks or compiles anything. What is tested is the
reasoning: which builds are tried and in which order, that a build which does
not find the card is thrown away again instead of quietly computing on the
processor, that the CUDA build from source runs where no package reaches the
GPU — and that llama.cpp stays installed either way, because a slow local LLM
still beats none.
"""

from __future__ import annotations

import pytest

from verba.services import llamabuild, llamacpp

#: conftest patches `possible` away for every test so no run ever compiles —
#: captured before that happens, for the tests about the predicate itself.
REAL_POSSIBLE = llamabuild.possible


@pytest.fixture()
def real_possible(monkeypatch):
    monkeypatch.setattr(llamabuild, "possible", REAL_POSSIBLE)


# ── asking a build what it sees ───────────────────────────────────────


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def answering(monkeypatch, **kwargs) -> None:
    monkeypatch.setattr(llamacpp.procutil, "run", lambda *a, **k: _Result(**kwargs))


def test_a_listed_gpu_is_reported(monkeypatch, tmp_path):
    answering(
        monkeypatch,
        stdout=(
            "Available devices:\n  CUDA0: NVIDIA RTX A500 Laptop GPU (4096 MiB, 3900 MiB free)\n"
        ),
    )
    assert llamacpp._probe_devices(tmp_path / "llama-server") == [
        "CUDA0: NVIDIA RTX A500 Laptop GPU (4096 MiB, 3900 MiB free)"
    ]


def test_a_vulkan_device_counts_just_as_much(monkeypatch, tmp_path):
    answering(monkeypatch, stdout="Available devices:\n  Vulkan0: NVIDIA RTX A500 (4096 MiB)\n")
    devices = llamacpp._probe_devices(tmp_path / "llama-server")
    assert devices and devices[0].startswith("Vulkan0")


def test_a_build_without_a_backend_reports_nothing(monkeypatch, tmp_path):
    """The CPU build lists the CPU, which is not a graphics card."""
    answering(monkeypatch, stdout="Available devices:\n  CPU: 13th Gen Intel Core i7\n")
    assert llamacpp._probe_devices(tmp_path / "llama-server") == []


def test_the_backend_announcement_is_a_second_witness(monkeypatch, tmp_path):
    """Some builds print what they found without listing it as a device."""
    answering(monkeypatch, stderr="ggml_cuda_init: found 1 CUDA devices:\n")
    assert llamacpp._probe_devices(tmp_path / "llama-server")


def test_a_build_that_does_not_know_the_question_answers_none(monkeypatch, tmp_path):
    """`None` is not `[]`: unknown must not read as "no GPU"."""
    answering(monkeypatch, stderr="error: invalid argument: --list-devices", returncode=1)
    assert llamacpp._probe_devices(tmp_path / "llama-server") is None


def test_a_binary_that_cannot_be_run_answers_none(monkeypatch, tmp_path):
    def explode(*args, **kwargs):
        raise OSError("Exec format error")

    monkeypatch.setattr(llamacpp.procutil, "run", explode)
    assert llamacpp._probe_devices(tmp_path / "llama-server") is None


# ── which builds are tried ────────────────────────────────────────────


def on_platform(monkeypatch, system: str, machine: str = "x86_64", gpu: bool = False) -> None:
    monkeypatch.setattr(llamacpp.platform, "system", lambda: system)
    monkeypatch.setattr(llamacpp.platform, "machine", lambda: machine)
    monkeypatch.setattr(llamacpp.hardware, "has_gpu", lambda *a, **k: gpu)


def test_linux_with_a_card_tries_vulkan_before_the_cpu(monkeypatch):
    on_platform(monkeypatch, "Linux", gpu=True)
    assert [c.key for c in llamacpp._candidates()] == ["vulkan", "cpu"]


def test_linux_without_a_card_has_nothing_to_try(monkeypatch):
    on_platform(monkeypatch, "Linux", gpu=False)
    assert [c.key for c in llamacpp._candidates()] == ["cpu"]


def test_windows_prefers_cuda_12_over_the_newer_one(monkeypatch):
    on_platform(monkeypatch, "Windows", "AMD64", gpu=True)
    patterns = [c.pattern for c in llamacpp._candidates()]
    assert patterns == ["bin-win-cuda-12", "bin-win-cuda", "bin-win-cpu-x64"]


def test_windows_on_arm_may_also_use_cuda(monkeypatch):
    on_platform(monkeypatch, "Windows", "ARM64", gpu=True)
    assert [c.key for c in llamacpp._candidates()] == ["cuda", "cpu"]


def test_an_unsupported_architecture_gets_no_ladder(monkeypatch):
    on_platform(monkeypatch, "Linux", "s390x", gpu=True)
    assert llamacpp._candidates() == []


# ── walking the ladder ────────────────────────────────────────────────


@pytest.fixture()
def ladder(monkeypatch, tmp_path):
    """A Linux with a card, a release with both builds, and nothing real."""
    on_platform(monkeypatch, "Linux", gpu=True)
    monkeypatch.setattr(llamacpp, "binary_dir", lambda: tmp_path / "llama")
    (tmp_path / "llama").mkdir(parents=True, exist_ok=True)
    rungs = [
        (
            llamacpp._Candidate("vulkan", "bin-ubuntu-vulkan-x64", True, "Vulkan-Build"),
            {"name": "llama-b1-bin-ubuntu-vulkan-x64.tar.gz", "size": 1024},
        ),
        (
            llamacpp._Candidate("cpu", "bin-ubuntu-x64", False, "CPU-Build"),
            {"name": "llama-b1-bin-ubuntu-x64.tar.gz", "size": 1024},
        ),
    ]
    monkeypatch.setattr(
        llamacpp, "resolve_release", lambda: ({"tag_name": "b1", "assets": []}, rungs)
    )
    monkeypatch.setattr(llamacpp.hub, "publish", lambda *a, **k: None)
    return rungs


def install_result(monkeypatch, outcomes: dict[str, list[str] | None]) -> list[str]:
    """Run the ladder with a canned answer per candidate; returns the log."""
    llamacpp._install_state.update(running=False, percent=0, detail="", error="", log=[])

    real_try = llamacpp._try_candidate

    def fake_candidate(candidate, asset, release, emit, base, span):
        # the compiled rung carries no asset and goes through the real
        # dispatch, so the tests about it exercise that path too
        if asset is None:
            return real_try(candidate, asset, release, emit, base, span)
        if candidate.key not in outcomes:
            return llamacpp._Attempt(None, problem="läuft auf diesem System nicht")
        directory = llamacpp.binary_dir() / candidate.key
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "llama-server"
        binary.write_bytes(b"\x7fELF")
        return llamacpp._Attempt(binary, outcomes[candidate.key])

    monkeypatch.setattr(llamacpp, "_try_candidate", fake_candidate)
    monkeypatch.setattr(llamacpp, "server_binary", lambda: None)
    llamacpp.install_binary()
    return llamacpp.install_state()["log"]


def test_a_vulkan_build_that_sees_the_card_ends_the_ladder(ladder, monkeypatch, tmp_path):
    install_result(monkeypatch, {"vulkan": ["Vulkan0: NVIDIA RTX A500"], "cpu": []})

    backend = llamacpp.installed_backend()
    assert backend["backend"] == "vulkan"
    assert backend["gpu"] is True
    assert backend["devices"] == ["Vulkan0: NVIDIA RTX A500"]
    # the CPU build was never fetched
    assert not (llamacpp.binary_dir() / "cpu").exists()


def test_a_gpu_build_without_a_card_is_thrown_away(ladder, monkeypatch, tmp_path):
    """The silence this whole ladder exists to break."""
    log = install_result(monkeypatch, {"vulkan": [], "cpu": []})

    assert llamacpp.installed_backend()["backend"] == "cpu"
    assert not (llamacpp.binary_dir() / "vulkan").exists()
    assert any("findet keine Grafikkarte" in line for line in log)
    assert any("Es bleibt beim CPU-Build" in line for line in log)


def test_an_unverifiable_build_is_kept_on_trust(ladder, monkeypatch, tmp_path):
    """A build that cannot be asked is not a build that has no GPU."""
    install_result(monkeypatch, {"vulkan": None, "cpu": []})

    backend = llamacpp.installed_backend()
    assert backend["backend"] == "vulkan"
    assert backend["verified"] is False
    assert backend["gpu"] is True


def test_the_cpu_build_still_lands_when_the_gpu_build_does_not_run(ladder, monkeypatch, tmp_path):
    log = install_result(monkeypatch, {"cpu": []})

    assert llamacpp.installed_backend()["backend"] == "cpu"
    assert any("Vulkan-Build: läuft auf diesem System nicht" in line for line in log)


def test_nothing_installable_is_an_error_that_names_the_attempts(ladder, monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="Vulkan-Build"):
        install_result(monkeypatch, {})


def test_the_accepted_build_is_the_only_one_left(ladder, monkeypatch, tmp_path):
    """Two installations side by side would make `server_binary()` a coin toss."""
    stale = llamacpp.binary_dir() / "cuda-source"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "llama-server").write_bytes(b"\x7fELF old")

    install_result(monkeypatch, {"vulkan": ["Vulkan0: NVIDIA"], "cpu": []})

    assert not stale.exists()
    assert [entry.name for entry in llamacpp.binary_dir().iterdir() if entry.is_dir()] == ["vulkan"]


# ── the CUDA build from source ────────────────────────────────────────


def test_the_source_build_runs_when_no_package_reaches_the_card(ladder, monkeypatch, tmp_path):
    built = tmp_path / "llama" / "cuda-source" / "llama-server"
    calls: list[str] = []

    def fake_build(tag, emit, *, base=0, span=100):
        calls.append(tag)
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_bytes(b"\x7fELF built")
        return built

    monkeypatch.setattr(llamabuild, "possible", lambda: (True, ""))
    monkeypatch.setattr(llamabuild, "build", fake_build)
    monkeypatch.setattr(llamacpp, "_probe_devices", lambda binary: ["CUDA0: NVIDIA RTX A500"])

    install_result(monkeypatch, {"vulkan": [], "cpu": []})

    assert calls == ["b1"], "the build uses the release the packages come from"
    backend = llamacpp.installed_backend()
    assert backend["backend"] == "cuda-source"
    assert backend["gpu"] is True


def test_a_failed_build_still_leaves_llama_cpp_installed(ladder, monkeypatch, tmp_path):
    def explode(tag, emit, *, base=0, span=100):
        raise RuntimeError("nvcc ließ sich nicht installieren")

    monkeypatch.setattr(llamabuild, "possible", lambda: (True, ""))
    monkeypatch.setattr(llamabuild, "build", explode)

    log = install_result(monkeypatch, {"vulkan": [], "cpu": []})

    assert llamacpp.installed_backend()["backend"] == "cpu"
    assert any("nvcc ließ sich nicht installieren" in line for line in log)


def test_an_impossible_build_says_why_in_the_log(ladder, monkeypatch, tmp_path):
    monkeypatch.setattr(
        llamabuild, "possible", lambda: (False, "Der CUDA-Treiber antwortet nicht (cuInit → 999)")
    )
    monkeypatch.setattr(llamabuild, "build", lambda *a, **k: pytest.fail("built anyway"))

    log = install_result(monkeypatch, {"vulkan": [], "cpu": []})

    assert any("cuInit" in line for line in log)


# ── whether a build is possible at all ────────────────────────────────


def test_no_build_without_a_card(real_possible, monkeypatch):
    monkeypatch.setattr(llamabuild.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llamabuild.hardware, "has_gpu", lambda *a, **k: False)
    possible, reason = llamabuild.possible()
    assert possible is False
    assert reason == "", "nothing is missing on a machine without a GPU"


def test_no_build_where_the_driver_is_silent(real_possible, monkeypatch):
    monkeypatch.setattr(llamabuild.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llamabuild.hardware, "has_gpu", lambda *a, **k: True)
    monkeypatch.setattr(llamabuild.cudalibs, "driver_state", lambda: (False, "cuInit → 999"))

    possible, reason = llamabuild.possible()

    assert possible is False
    assert "/dev/nvidia-uvm" in reason


def test_no_build_without_a_way_to_install_the_tools(real_possible, monkeypatch):
    monkeypatch.setattr(llamabuild.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llamabuild.hardware, "has_gpu", lambda *a, **k: True)
    monkeypatch.setattr(llamabuild.cudalibs, "driver_state", lambda: (True, "libcuda.so.1"))
    monkeypatch.setattr(llamabuild, "missing_tools", lambda: ["nvcc"])
    monkeypatch.setattr(llamacpp, "can_install_packages", lambda: False)

    possible, reason = llamabuild.possible()

    assert possible is False
    assert "nvcc" in reason and "sudo" in reason


def test_a_build_is_possible_with_the_tools_in_place(real_possible, monkeypatch):
    monkeypatch.setattr(llamabuild.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llamabuild.hardware, "has_gpu", lambda *a, **k: True)
    monkeypatch.setattr(llamabuild.cudalibs, "driver_state", lambda: (True, "libcuda.so.1"))
    monkeypatch.setattr(llamabuild, "missing_tools", lambda: [])

    assert llamabuild.possible() == (True, "")


def test_windows_never_compiles(real_possible, monkeypatch):
    """There is an official CUDA package; compiling would be absurd."""
    monkeypatch.setattr(llamabuild.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llamabuild.hardware, "has_gpu", lambda *a, **k: True)
    assert llamabuild.possible() == (False, "")


# ── how hard the machine is allowed to work ───────────────────────────


@pytest.mark.parametrize(
    ("cores", "ram_mb", "expected"),
    [
        (16, 8 * 1024, 4),  # the container that would OOM under -j16
        (4, 64 * 1024, 4),  # plenty of memory, four cores is the limit
        (16, 64 * 1024, 16),
        (2, 0, 2),  # no readable memory: stay careful
    ],
)
def test_the_compiler_count_is_bounded_by_memory(monkeypatch, cores, ram_mb, expected):
    monkeypatch.setattr(llamabuild.os, "cpu_count", lambda: cores)
    monkeypatch.setattr(llamabuild.hardware, "ram_mb", lambda: (ram_mb, ram_mb))
    assert llamabuild._parallel_jobs() == expected


# ── the tools it looks for ────────────────────────────────────────────


def test_nvcc_is_found_where_a_toolkit_puts_it(monkeypatch, tmp_path):
    nvcc = tmp_path / "nvcc"
    nvcc.write_bytes(b"")
    monkeypatch.setattr(llamabuild.shutil, "which", lambda tool: None)
    monkeypatch.setattr(llamabuild, "_NVCC_PATHS", (str(nvcc),))
    assert llamabuild._find("nvcc") == str(nvcc)
    assert llamabuild._find("cmake") is None


def test_only_known_tools_can_be_installed():
    """The package names are a closed table, never anything parsed."""
    assert set(llamabuild.TOOLS) == {"cmake", "make", "g++", "nvcc"}
    for entry in llamabuild.TOOLS.values():
        assert "apt" in entry


def test_the_cmake_call_builds_for_this_card_only(tmp_path):
    arguments = llamabuild._cmake_arguments(tmp_path, tmp_path / "build", native=True)
    assert "-DGGML_CUDA=ON" in arguments
    assert "-DCMAKE_CUDA_ARCHITECTURES=native" in arguments
    assert "-DLLAMA_BUILD_SERVER=ON" in arguments
    # one binary to copy, nothing to find next to it at run time
    assert "-DBUILD_SHARED_LIBS=OFF" in arguments


def test_an_old_cmake_gets_the_call_without_native(tmp_path):
    arguments = llamabuild._cmake_arguments(tmp_path, tmp_path / "build", native=False)
    assert not any("CUDA_ARCHITECTURES" in argument for argument in arguments)
    assert "-DGGML_CUDA=ON" in arguments


# ── what the review found ─────────────────────────────────────────────


def test_a_rung_that_raises_does_not_cost_the_cpu_build(ladder, monkeypatch, tmp_path):
    """Too little disk for a CUDA runtime must not skip the 17 MB build."""
    llamacpp._install_state.update(running=False, percent=0, detail="", error="", log=[])

    def fake_candidate(candidate, asset, release, emit, base, span):
        if candidate.gpu:
            raise OSError("Zu wenig freier Speicherplatz")
        directory = llamacpp.binary_dir() / candidate.key
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "llama-server"
        binary.write_bytes(b"ELF")
        return llamacpp._Attempt(binary, [])

    monkeypatch.setattr(llamacpp, "_try_candidate", fake_candidate)
    monkeypatch.setattr(llamacpp, "server_binary", lambda: None)

    llamacpp.install_binary()

    assert llamacpp.installed_backend()["backend"] == "cpu"
    log = llamacpp.install_state()["log"]
    assert any("Speicherplatz" in line for line in log), log


def test_an_announcement_across_two_lines_is_unknown_not_absent(monkeypatch, tmp_path):
    """The whole output matched, no single line did — that is not "no card"."""
    answering(monkeypatch, stderr="ggml_vulkan: found 1\nVulkan devices available\n")
    assert llamacpp._probe_devices(tmp_path / "llama-server") is None
