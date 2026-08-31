"""Would this model actually run here? — the memory verdicts and the guards.

Two things are pinned down: the verdict a model gets on a given machine
(`ok` / `tight` / `no`, in German, only for engines that run locally), and the
behaviour when memory really does run out — a refusal or a CPU fallback with a
message, never a process that simply dies.

Every test states its machine explicitly; nothing here depends on the RAM or
the GPU of the box the suite runs on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import types

import pytest

from verba import config
from verba.services import hardware, llamacpp, whisper

NO_CANCEL = threading.Event()


def machine(
    *, ram: int, ram_free: int | None = None, vram: int = 0, vram_free: int | None = None
) -> dict:
    """A probe result in MB, as `hardware.probe()` reports it."""
    return {
        "ram_total_mb": ram,
        "ram_available_mb": ram if ram_free is None else ram_free,
        "gpu_name": "NVIDIA Test GPU" if vram else "",
        "vram_total_mb": vram,
        "vram_free_mb": vram if vram_free is None else vram_free,
    }


SMALL_BOX = machine(ram=4096)  # office laptop, no GPU
WORKSTATION = machine(ram=32768, vram=4096)  # much RAM, small GPU
SERVER = machine(ram=131072, vram=49140)  # everything fits


@pytest.fixture(autouse=True)
def fresh_probe_cache():
    """The probe is cached process-wide — no leaking between tests."""
    hardware.invalidate_probe()
    yield
    hardware.invalidate_probe()


# ── the probe ─────────────────────────────────────────────────────────


def test_the_probe_reports_plausible_numbers():
    hw = hardware.probe()
    assert hw["ram_total_mb"] >= hw["ram_available_mb"] >= 0
    assert hw["vram_total_mb"] >= 0
    assert set(hw) == {
        "ram_total_mb",
        "ram_available_mb",
        "gpu_name",
        "vram_total_mb",
        "vram_free_mb",
    }


def test_the_gpu_query_is_parsed(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "NVIDIA RTX A500 Laptop GPU, 4096, 3210\n"

    monkeypatch.setattr(hardware.subprocess, "run", lambda *a, **k: FakeResult())
    assert hardware.gpu_info() == {
        "name": "NVIDIA RTX A500 Laptop GPU",
        "vram_total_mb": 4096,
        "vram_free_mb": 3210,
    }


@pytest.mark.parametrize("failure", [OSError("not found"), subprocess.TimeoutExpired("x", 1)])
def test_a_machine_without_nvidia_smi_reports_no_gpu(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(hardware.subprocess, "run", fail)
    assert hardware.gpu_info() == {"name": "", "vram_total_mb": 0, "vram_free_mb": 0}
    assert hardware.has_gpu(hardware.probe(fresh=True)) is False


def test_the_posix_memory_probe_parses_proc_meminfo(monkeypatch):
    """The Linux branch, exercised on every platform.

    Each OS has its own probe and normally only its own is ever run, so the
    other one is only covered by the CI job for that OS. A probe that quietly
    returned zero would turn every verdict into "unknown", which is worth
    catching on both.
    """
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    class FakeProcFile:
        def __init__(self, path):
            assert str(path) == "/proc/meminfo"

        def read_text(self, encoding="utf-8"):
            return "MemTotal:       16384000 kB\nMemFree:  1000 kB\nMemAvailable:  8192000 kB\n"

    monkeypatch.setattr(hardware, "Path", FakeProcFile)
    assert hardware.ram_mb() == (16000, 8000)


def test_a_machine_without_proc_meminfo_reports_zero_instead_of_raising(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    class Missing:
        def __init__(self, path):
            pass

        def read_text(self, encoding="utf-8"):
            raise OSError("no /proc here")

    monkeypatch.setattr(hardware, "Path", Missing)
    assert hardware.ram_mb() == (0, 0)


@pytest.mark.skipif(os.name != "nt", reason="the Windows probe needs kernel32")
def test_the_windows_memory_probe_reports_numbers():
    total, available = hardware.ram_mb()
    assert total >= available > 0


def test_the_probe_is_cached_and_can_be_invalidated(monkeypatch):
    calls = []
    monkeypatch.setattr(hardware, "ram_mb", lambda: (calls.append(1), (8192, 4096))[1])
    monkeypatch.setattr(
        hardware, "gpu_info", lambda: {"name": "", "vram_total_mb": 0, "vram_free_mb": 0}
    )

    hardware.probe(fresh=True)
    hardware.probe()
    assert len(calls) == 1  # served from the cache

    hardware.invalidate_probe()
    hardware.probe()
    assert len(calls) == 2


def test_an_aged_probe_does_not_make_the_caller_wait(monkeypatch):
    """nvidia-smi costs half a second on some machines — a UI request that
    only wants the RAM/VRAM numbers must not sit through it. The aged value is
    handed out, the refresh happens behind it."""
    hardware.invalidate_probe()
    seen: list[str] = []
    refreshing = threading.Event()
    release = threading.Event()

    def gpu_info():
        first = not seen
        seen.append("call")
        if not first:  # the background refresh: hold it open on purpose
            refreshing.set()
            release.wait(5)
        return {"name": "old" if first else "new", "vram_total_mb": 8192, "vram_free_mb": 4096}

    monkeypatch.setattr(hardware, "ram_mb", lambda: (8192, 4096))
    monkeypatch.setattr(hardware, "gpu_info", gpu_info)

    assert hardware.probe()["gpu_name"] == "old"  # first call measures
    monkeypatch.setattr(hardware, "PROBE_TTL_S", 0)

    started = time.monotonic()
    assert hardware.probe()["gpu_name"] == "old"  # stale, but instant
    assert time.monotonic() - started < 1
    assert refreshing.wait(5)

    release.set()
    for _ in range(50):  # the refreshed value lands without anybody waiting
        if hardware.probe()["gpu_name"] == "new":
            break
        time.sleep(0.05)
    assert hardware.probe()["gpu_name"] == "new"


# ── the verdicts ──────────────────────────────────────────────────────


def test_room_to_spare_is_ok():
    verdict = hardware.rate(1000, total_mb=8000, free_mb=6000, device="cpu")
    assert verdict["level"] == hardware.OK
    assert "Geeignet" in verdict["message"]


def test_nearly_the_whole_memory_is_tight():
    verdict = hardware.rate(7000, total_mb=8000, free_mb=8000, device="cpu")
    assert verdict["level"] == hardware.TIGHT


def test_memory_that_is_currently_occupied_is_tight_not_no():
    """The model does fit — something else is holding the memory right now."""
    verdict = hardware.rate(4000, total_mb=8000, free_mb=2000, device="gpu")
    assert verdict["level"] == hardware.TIGHT
    assert "VRAM" in verdict["message"]
    assert "Andere Programme" in verdict["message"]


def test_more_than_the_machine_has_is_a_no():
    verdict = hardware.rate(9000, total_mb=8000, free_mb=8000, device="cpu")
    assert verdict["level"] == hardware.NO
    assert "Zu groß" in verdict["message"]


def test_an_unreadable_machine_says_unknown_instead_of_guessing():
    verdict = hardware.rate(1000, total_mb=0, free_mb=0, device="cpu")
    assert verdict["level"] == hardware.UNKNOWN


# ── Whisper models ────────────────────────────────────────────────────


def test_a_small_machine_is_told_which_models_it_can_run():
    assert hardware.check_whisper_model("small", hw=SMALL_BOX)["level"] == hardware.OK
    too_big = hardware.check_whisper_model("large-v3", hw=SMALL_BOX)
    assert too_big["level"] == hardware.NO
    assert "Empfehlung für dieses System" in too_big["message"]


def test_a_model_too_large_for_the_gpu_falls_back_instead_of_failing():
    """4 GB VRAM, 32 GB RAM: large-v3 does run — on the CPU, and slower."""
    verdict = hardware.check_whisper_model("large-v3", hw=WORKSTATION)
    assert verdict["level"] == hardware.TIGHT
    assert verdict["device"] == "cpu"
    assert "Grafikspeicher" in verdict["message"]
    assert "CPU" in verdict["message"]


def test_the_gpu_is_used_when_the_model_fits_into_it():
    verdict = hardware.check_whisper_model("small", hw=WORKSTATION)
    assert (verdict["level"], verdict["device"]) == (hardware.OK, "gpu")


def test_cpu_only_is_rated_on_the_ram_even_with_a_gpu_present():
    verdict = hardware.check_whisper_model("large-v3", device="cpu", hw=WORKSTATION)
    assert verdict["device"] == "cpu"
    assert verdict["level"] == hardware.OK


def test_the_recommendation_grows_with_the_machine():
    assert hardware.suggest_whisper_model(SMALL_BOX) == "large-v3-turbo"
    assert hardware.suggest_whisper_model(SERVER) == "large-v3"
    nothing_fits = machine(ram=1024)
    assert hardware.suggest_whisper_model(nothing_fits) in ("", "tiny")


def test_an_unknown_model_is_rated_by_its_files_on_disk(tmp_path):
    folder = tmp_path / "mein-modell"
    folder.mkdir()
    (folder / "model.bin").write_bytes(b"x" * 3 * 1024 * 1024)

    need = hardware.whisper_requirement("mein-modell", tmp_path)

    assert need["cpu_mb"] < hardware.WHISPER_FALLBACK["cpu_mb"]  # not the pessimistic default
    assert need["cpu_mb"] >= 300


def test_an_unknown_model_without_files_assumes_the_worst(tmp_path):
    assert hardware.whisper_requirement("noch-nicht-da", tmp_path) == hardware.WHISPER_FALLBACK


# ── the API says it, and only for local engines ───────────────────────


def test_the_model_list_carries_the_verdict(client, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda **kw: SMALL_BOX)

    data = client.get("/api/models").json()

    assert data["hardware"]["ram_total_mb"] == 4096
    assert data["suggested"] == "large-v3-turbo"
    assert data["models"]["small"]["level"] == hardware.OK
    assert data["models"]["large-v3"]["level"] == hardware.NO
    # every offered name is rated, so no row is left without an answer
    assert set(data["models"]) >= set(data["builtin"])


def test_the_llm_catalog_carries_the_verdict(client, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda **kw: WORKSTATION)

    data = client.get("/api/models/llm").json()

    fits = {entry["name"]: entry["fit"] for entry in data["catalog"]}
    # 3.5 GB in a 4 GB GPU: runs there, and the wording says it is close
    assert fits["Qwen3-1.7B-Q8_0"]["level"] == hardware.TIGHT
    assert fits["Qwen3-1.7B-Q8_0"]["device"] == "gpu"
    # 12B does not fit the 4 GB GPU, but 32 GB of RAM carry it — slowly
    biggest = data["catalog"][-1]
    assert biggest["fit"]["level"] == hardware.TIGHT
    assert "Arbeitsspeicher" in biggest["fit"]["message"]
    assert all("message" in entry["fit"] for entry in data["catalog"])


def test_a_machine_too_small_for_any_local_llm_is_told_so(client, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda **kw: SMALL_BOX)

    data = client.get("/api/models/llm").json()

    assert {entry["fit"]["level"] for entry in data["catalog"]} == {hardware.NO}


def test_a_remote_endpoint_is_not_rated(client):
    """Nothing is said about hardware that is not ours: the settings for an
    OpenAI-compatible endpoint carry no verdict at all."""
    settings = config.get_settings()
    settings.llm.mode = "openai"
    settings.llm.base_url = "https://example.invalid/v1"
    config.save_settings(settings)

    body = client.get("/api/settings").json()

    assert "fit" not in body["llm"]
    assert "hardware" not in body["llm"]


# ── embedding models (the search index) ───────────────────────────────


def test_embedding_models_are_rated_on_the_ram():
    """They always encode on the CPU, so there is no GPU variant."""
    small = hardware.check_embedding_model(470, hw=SMALL_BOX)  # MiniLM
    assert (small["level"], small["device"]) == (hardware.OK, "cpu")

    big = hardware.check_embedding_model(2270, hw=SMALL_BOX)  # BGE-M3
    assert big["level"] == hardware.NO
    assert big["device"] == "cpu"

    # the same big model on a real machine is fine
    assert hardware.check_embedding_model(2270, hw=WORKSTATION)["level"] == hardware.OK


def test_the_embedding_recommendation_is_the_largest_that_fits():
    catalog = [("mini", 470), ("mpnet", 1030), ("bge", 2270)]
    assert hardware.suggest_embedding_model(catalog, SERVER) == "bge"
    assert hardware.suggest_embedding_model(catalog, SMALL_BOX) in ("mini", "mpnet")


def test_a_model_that_does_not_fit_names_one_that_does():
    catalog = [("mini", 470), ("mpnet", 1030), ("bge", 2270)]
    verdict = hardware.check_embedding_model(2270, hw=SMALL_BOX, alternatives=catalog)
    assert verdict["level"] == hardware.NO
    assert "Empfehlung für dieses System: mpnet" in verdict["message"]


def test_a_recommendation_does_not_follow_the_momentary_free_memory():
    """A busy machine must not recommend a smaller model than it can run —
    otherwise the advice changes every time a browser window opens."""
    busy = machine(ram=32768, ram_free=2048)
    idle = machine(ram=32768)
    catalog = [("mini", 470), ("mpnet", 1030), ("bge", 2270)]

    assert hardware.suggest_embedding_model(catalog, busy) == "bge"
    assert hardware.suggest_embedding_model(catalog, busy) == hardware.suggest_embedding_model(
        catalog, idle
    )
    assert hardware.suggest_whisper_model(busy) == hardware.suggest_whisper_model(idle)
    # the verdict, in contrast, does look at what is free right now
    assert hardware.check_embedding_model(2270, hw=busy)["level"] == hardware.TIGHT
    assert hardware.check_embedding_model(2270, hw=idle)["level"] == hardware.OK


def test_the_embedding_catalog_carries_the_verdict(client, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda **kw: SMALL_BOX)

    data = client.get("/api/search/models").json()

    levels = {entry["label"]: entry["fit"]["level"] for entry in data["models"]}
    assert set(levels.values()) <= {hardware.OK, hardware.TIGHT, hardware.NO}
    assert data["hardware"]["ram_total_mb"] == 4096
    # the largest one (BGE-M3) does not fit a 4 GB machine
    biggest = max(data["models"], key=lambda entry: entry["size_mb"])
    assert biggest["fit"]["level"] == hardware.NO
    assert "Empfehlung für dieses System" in biggest["fit"]["message"]


def test_an_embedding_model_that_cannot_fit_is_refused_before_torch_loads(monkeypatch):
    from verba.services import vectorstore

    settings = config.get_settings()
    settings.search.embedding_model = "BAAI/bge-m3"
    config.save_settings(settings)
    monkeypatch.setattr(hardware, "probe", lambda **kw: machine(ram=2048))
    vectorstore.unload_model()

    with pytest.raises(vectorstore.EmbeddingUnavailable, match="BGE-M3") as raised:
        vectorstore._load_model()
    assert "passt nicht in den Speicher" in str(raised.value)


# ── a local endpoint Verba does not manage ────────────────────────────


def test_the_machine_budget_is_reported_for_a_local_endpoint(client, monkeypatch):
    """An endpoint on 127.0.0.1 runs here, so the UI can estimate — the numbers
    come from the backend, the caveat wording from the UI catalog."""
    monkeypatch.setattr(hardware, "probe", lambda **kw: WORKSTATION)

    budget = client.get("/api/models/llm").json()["budget"]

    assert budget["gpu_mb"] == 4096 - hardware.VRAM_MARGIN_MB
    assert budget["cpu_mb"] == 32768 - hardware.CPU_RESERVE_MB
    assert budget["gpu_total_mb"] == 4096


def test_a_machine_without_a_gpu_reports_no_gpu_budget():
    budget = hardware.model_budget(SMALL_BOX)
    assert budget["gpu_mb"] == 0
    assert budget["cpu_mb"] == 4096 - hardware.CPU_RESERVE_MB


def test_the_verdicts_are_german():
    """These sentences are shown in the web UI (project rule)."""
    english = ("needs", "available", "too large", "fits", "memory", "recommend")
    for hw in (SMALL_BOX, WORKSTATION, SERVER):
        for name in hardware.WHISPER_REQUIREMENTS:
            message = hardware.check_whisper_model(name, hw=hw)["message"].lower()
            assert not any(word in message for word in english), message


# ── running out of memory ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        "CUDA failed with error out of memory",
        "cublas_status_alloc_failed",
        "std::bad_alloc",
        "ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer",
        "The paging file is too small for this operation to complete",
        MemoryError(),
    ],
)
def test_memory_failures_are_recognised(error):
    assert hardware.is_oom(error) is True


@pytest.mark.parametrize(
    "error", ["model.bin not found", "invalid audio format", RuntimeError("connection reset")]
)
def test_other_failures_are_not_mistaken_for_memory(error):
    assert hardware.is_oom(error) is False


def test_the_memory_message_names_the_numbers():
    message = hardware.oom_message("gpu", name="large-v3", hw=WORKSTATION)
    assert "large-v3" in message
    assert "VRAM" in message
    assert "4,0 GB" in message


# ── Whisper: the guard around the load ────────────────────────────────


def fake_whisper_module(record: list[str]):
    """A stand-in for faster-whisper (not installed in the test environment)."""
    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *args, device: str = "", **kwargs):
            record.append(device)

    module.WhisperModel = WhisperModel
    return module


def test_a_model_that_cannot_fit_is_refused_before_the_backend_loads_it(monkeypatch):
    record: list[str] = []
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_whisper_module(record))
    monkeypatch.setattr(hardware, "probe", lambda **kw: machine(ram=2048))
    settings = config.get_settings()
    settings.whisper.model = "large-v3"
    config.save_settings(settings)
    whisper.unload_model()

    with pytest.raises(hardware.InsufficientMemory, match="large-v3"):
        whisper.get_model()

    assert record == []  # nothing was allocated


def test_a_model_too_large_for_the_vram_skips_the_gpu_attempt(monkeypatch):
    record: list[str] = []
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_whisper_module(record))
    monkeypatch.setattr(hardware, "probe", lambda **kw: WORKSTATION)
    settings = config.get_settings()
    settings.whisper.model = "large-v3"
    settings.whisper.device = "auto"
    config.save_settings(settings)
    whisper.unload_model()

    whisper.get_model()

    assert record == ["cpu"]  # no doomed CUDA load in between


def test_a_fitting_model_still_goes_to_the_gpu(monkeypatch):
    record: list[str] = []
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_whisper_module(record))
    monkeypatch.setattr(hardware, "probe", lambda **kw: WORKSTATION)
    settings = config.get_settings()
    settings.whisper.model = "small"
    settings.whisper.device = "auto"
    config.save_settings(settings)
    whisper.unload_model()

    whisper.get_model()

    assert record == ["auto"]  # faster-whisper picks the GPU itself


def test_out_of_gpu_memory_continues_on_the_cpu(monkeypatch):
    """The transcription itself hits the wall — the job must not fail."""
    monkeypatch.setattr(whisper, "_active_device", "cuda")
    monkeypatch.setattr(whisper, "unload_model", lambda: None)
    messages: list[str] = []
    attempts = []

    def run():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("CUDA failed with error out of memory")
        return "transkribiert"

    result = whisper._with_cpu_fallback(run, lambda percent, message: messages.append(message))

    assert result == "transkribiert"
    assert len(attempts) == 2
    assert any("CPU" in message for message in messages)
    assert any("Grafikspeicher" in message for message in messages)


def test_out_of_ram_reports_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(whisper, "_active_device", "cpu")

    def run():
        raise MemoryError()

    with pytest.raises(hardware.InsufficientMemory, match="Arbeitsspeicher"):
        whisper._with_cpu_fallback(run, lambda percent, message: None)


# ── local LLM: the guard around the server ────────────────────────────


def test_the_local_server_is_not_started_for_a_model_that_cannot_fit(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    binary.write_bytes(b"")
    model = tmp_path / "gemma-3-12b-it-Q4_K_M.gguf"
    model.write_bytes(b"")
    monkeypatch.setattr(llamacpp, "server_binary", lambda: binary)
    monkeypatch.setattr(llamacpp, "_pick_model_file", lambda: model)
    monkeypatch.setattr(hardware, "probe", lambda **kw: SMALL_BOX)

    def refuse_to_start(*args, **kwargs):
        raise AssertionError("llama-server must not be started at all")

    monkeypatch.setattr(llamacpp.subprocess, "Popen", refuse_to_start)

    with pytest.raises(hardware.InsufficientMemory, match="gemma-3-12b"):
        llamacpp.ensure_running()


def test_a_gguf_larger_than_the_free_vram_stays_in_ram():
    needs = llamacpp.model_needs_mb(llamacpp.MODEL_CATALOG[-1])  # the 12B model
    assert hardware.offload_to_gpu(needs, WORKSTATION) is False
    assert hardware.offload_to_gpu(needs, SERVER) is True


def test_a_server_killed_by_the_memory_says_so(tmp_path):
    tail = [
        "load_tensors: loading model tensors",
        "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 6000 MiB: out of memory",
    ]
    message = llamacpp._startup_failure(tmp_path / "modell.gguf", tail, True)
    assert "modell.gguf" in message
    assert "Grafikspeicher" in message
    assert "kleineres Modell" in message


def test_any_other_crash_still_names_the_last_line(tmp_path):
    message = llamacpp._startup_failure(tmp_path / "modell.gguf", ["error: bad magic"], False)
    assert "bad magic" in message
    assert "Speicher" not in message


def test_an_unknown_gguf_is_rated_by_its_size(tmp_path):
    model = tmp_path / "fremd.gguf"
    model.write_bytes(b"x" * 5 * 1024 * 1024)
    assert llamacpp.file_needs_mb(model) == hardware.gguf_requirement(5)
    # a catalog model keeps the curated number
    entry = llamacpp.MODEL_CATALOG[0]
    assert llamacpp.file_needs_mb(tmp_path / entry["file"]) == entry["min_free_mb"]


def test_a_memory_refusal_reaches_the_llm_caller_as_an_llm_error(monkeypatch):
    """docs Q&A and the RAG answer turn LLMError into a 502 with the text —
    an unhandled RuntimeError would be a bare 500 instead."""
    from verba.services import llm

    settings = config.get_settings()
    settings.llm.mode = "local"
    config.save_settings(settings)
    monkeypatch.setattr(
        llamacpp,
        "ensure_running",
        lambda: (_ for _ in ()).throw(hardware.InsufficientMemory("Kein Speicher frei.")),
    )

    with pytest.raises(llm.LLMError, match="Kein Speicher frei"):
        llm._resolve_endpoint(config.get_settings())
