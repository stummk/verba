# PyInstaller spec for the Verba desktop builds (Windows + Linux).
#
#   pyinstaller packaging/verba.spec --noconfirm
#
# One-dir build: dist/verba/ contains the executable plus _internal/ with the
# bundled Python, core dependencies, frontend and user docs. Heavy feature
# groups (Whisper, search) are NOT bundled — the in-app first-run setup pip-
# installs them into the per-user data directory (see setup_check.py), which
# is why pip itself must be part of the bundle.

import sys
from pathlib import Path

import pip
from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — SPECPATH is a PyInstaller global

datas = [
    (str(PROJECT_ROOT / "frontend"), "frontend"),
    (str(PROJECT_ROOT / "docs" / "user"), "docs/user"),
    # pip is shipped as a plain file tree, NOT as frozen modules: its vendored
    # distlib only understands standard path-based imports, so the runtime
    # feature-group installation adds <bundle>/pip-lib to sys.path and imports
    # pip from there (see setup_check._pip_install_frozen).
    (str(Path(pip.__file__).parent), "pip-lib/pip"),
]

# Runtime-installed feature groups may import any part of the standard
# library (fpdf2 → timeit + unittest.mock, torch → multiprocessing, ...) —
# bundle it whole, including package submodules, instead of chasing
# individual names. Missing platform-specific names only warn.
STDLIB_SKIP = {"tkinter", "test", "idlelib", "turtledemo", "antigravity", "this"}

stdlib_hidden = []
for module_name in sys.stdlib_module_names:
    if module_name in STDLIB_SKIP or module_name.startswith("_"):
        continue
    stdlib_hidden.append(module_name)
    try:
        stdlib_hidden.extend(collect_submodules(module_name))
    except Exception:
        pass  # not importable on this platform

hiddenimports = [
    *collect_submodules("verba"),
    *stdlib_hidden,
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

binaries = []
if sys.platform == "win32":
    # Stable-ABI wheels (e.g. PyAV, installed at runtime) link against
    # python3.dll — PyInstaller only ships python3XY.dll by itself.
    python3_dll = Path(sys.base_prefix) / "python3.dll"
    if python3_dll.exists():
        binaries.append((str(python3_dll), "."))

# Heavy feature groups are installed at runtime into <data>/site-packages —
# never bundle them, even when the build environment happens to have them
# (PyInstaller follows function-level imports too).
excludes = [
    "tkinter",
    "test",
    "faster_whisper",
    "ctranslate2",
    "torch",
    "torchaudio",
    "sentence_transformers",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "sqlite_vec",
    "fpdf",
    "av",
    "onnxruntime",
    "numpy",
    "scipy",
    "sklearn",
    "PIL",
    "pandas",
]

a = Analysis(
    [str(PROJECT_ROOT / "run.py")],
    pathex=[str(PROJECT_ROOT / "backend")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Verba",
    icon=str(PROJECT_ROOT / "packaging" / "verba.ico") if sys.platform == "win32" else None,
    console=False,  # logs go to <data>/logs; run.py guards the missing streams
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="verba",
    upx=False,
)
