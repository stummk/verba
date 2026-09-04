"""Write a release version into the backend package.

The tag of a GitHub release is the single source of truth for the version of a
build: the release pipeline names its artifacts after it, and this script puts
the very same number into ``backend/verba/__init__.py`` so the running app
reports it too (Settings -> System, ``/api/system/info``, the startup banner).
Without that step every installer would keep showing whatever version happened
to be committed last.

Usage:

    python packaging/stamp_version.py 0.1.1
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

INIT_FILE = Path(__file__).resolve().parents[1] / "backend" / "verba" / "__init__.py"

# a release tag may be written as "v1.2.3"; pre-release suffixes stay allowed
VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[.-]?[0-9A-Za-z.]+)?$")

_ASSIGNMENT = re.compile(r'^__version__ = ".*"$', re.MULTILINE)


def normalise(version: str) -> str:
    """Strip a leading ``v`` and validate the remaining version string."""
    cleaned = version.strip()
    if cleaned.startswith(("v", "V")):
        cleaned = cleaned[1:]
    if not VERSION_PATTERN.match(cleaned):
        raise ValueError(f"not a usable version: {version!r}")
    return cleaned


def stamp(version: str, path: Path = INIT_FILE) -> str:
    """Rewrite ``__version__`` in *path* and return the version written."""
    cleaned = normalise(version)
    source = path.read_text(encoding="utf-8")
    updated, count = _ASSIGNMENT.subn(f'__version__ = "{cleaned}"', source, count=1)
    if count != 1:
        raise ValueError(f"no __version__ assignment found in {path}")
    if updated != source:
        path.write_text(updated, encoding="utf-8", newline="\n")
    return cleaned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version, with or without a leading 'v'")
    args = parser.parse_args(argv)
    try:
        written = stamp(args.version)
    except ValueError as error:
        print(f"stamp_version: {error}", file=sys.stderr)
        return 2
    print(f"stamped version {written} into {INIT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
