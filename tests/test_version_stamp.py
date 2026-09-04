"""The release version reaches the running app.

A build takes its number from the release tag, and ``packaging/stamp_version``
writes exactly that number into the backend package. If it ever stopped
matching, every installer would report the version that happened to be
committed last instead of the one that was released.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "stamp_version", ROOT / "packaging" / "stamp_version.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stamp_version = _load_module()


def _init_copy(tmp_path: Path) -> Path:
    target = tmp_path / "__init__.py"
    target.write_bytes((ROOT / "backend" / "verba" / "__init__.py").read_bytes())
    return target


def test_stamp_rewrites_the_version(tmp_path):
    target = _init_copy(tmp_path)
    assert stamp_version.stamp("1.4.0", target) == "1.4.0"
    assert '__version__ = "1.4.0"' in target.read_text(encoding="utf-8")


def test_stamp_accepts_a_tag_with_a_v_prefix(tmp_path):
    target = _init_copy(tmp_path)
    assert stamp_version.stamp("v2.0.3", target) == "2.0.3"
    assert '__version__ = "2.0.3"' in target.read_text(encoding="utf-8")


def test_stamped_file_still_exposes_the_version(tmp_path):
    """The rewritten module remains importable and answers with the version."""
    target = _init_copy(tmp_path)
    stamp_version.stamp("9.9.9", target)
    namespace: dict[str, object] = {}
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), namespace)
    assert namespace["__version__"] == "9.9.9"


@pytest.mark.parametrize("bad", ["", "release", "1.2.3 ; rm -rf /", "v"])
def test_stamp_refuses_a_version_that_is_not_one(tmp_path, bad):
    target = _init_copy(tmp_path)
    before = target.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        stamp_version.stamp(bad, target)
    assert target.read_text(encoding="utf-8") == before


def test_stamp_reports_a_missing_assignment(tmp_path):
    target = tmp_path / "__init__.py"
    target.write_text('"""No version here."""\n', encoding="utf-8")
    with pytest.raises(ValueError):
        stamp_version.stamp("1.0.0", target)


def test_release_pipeline_stamps_every_build():
    """All three release artifacts carry the version, so all three jobs stamp."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    job_block = workflow.split("\njobs:\n", 1)[1]
    jobs = re.findall(r"^  ([a-z0-9-]+):$", job_block, re.MULTILINE)
    assert set(jobs) == {"windows-installer", "linux-appimage", "server-zip"}
    assert workflow.count("packaging/stamp_version.py") == len(jobs)
