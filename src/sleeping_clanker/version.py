"""(2026-10-26) The four-part version MAJOR.MINOR.PATCH.BUILD (owner task 38040b3c; CHANGELOG.md).

The one source is `__version__` in this package's __init__.py; every other place derives from it:
pyproject.toml reads it (setuptools dynamic attr), and vscode-extension/scripts/build.py writes
vscode_version() into package.json, names the VSIX with the full version and stamps build_info.json
into the VSIX copy of the core. vsce 3.2.1 refuses four parts ("Invalid extension version") and VS Code
compares extension versions with semver, so package.json carries MAJOR.MINOR.(PATCH*1000+BUILD):
every new build is a higher x.y.z while BUILD stays below BUILD_LIMIT.
Compare versions with parse() (integer tuples), never as text: as text, BUILD 10 sorts before BUILD 9.
Examples here are tuples: a dotted four-number literal has an IPv4 shape, which the network contract
test allows only on the __version__ line. Standard library only; build.py imports it too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PARTS = ("major", "minor", "patch", "build")
_FOUR = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
BUILD_LIMIT = 1000  # BUILD 1000 would collide with PATCH+1 in vscode_version(): bump PATCH first
BUILD_INFO = Path(__file__).with_name("build_info.json")  # written by build.py into the VSIX copy only


def parse(v: str) -> tuple[int, int, int, int]:
    """(MAJOR, MINOR, PATCH, BUILD) of `v`; ValueError for anything but four plain non-negative integers."""
    m = _FOUR.fullmatch(v) if isinstance(v, str) else None
    if not m:
        raise ValueError(f"not a four-part version MAJOR.MINOR.PATCH.BUILD: {v!r}")
    major, minor, patch, build = (int(x) for x in m.groups())
    return major, minor, patch, build


def bump(v: str, part: str) -> str:
    """`part` + 1 and every part after it reset to 0: (1, 4, 2, 42) + patch = (1, 4, 3, 0), + build =
    (1, 4, 2, 43)."""
    if part not in PARTS:
        raise ValueError(f"unknown version part {part!r}; one of {', '.join(PARTS)}")
    i = PARTS.index(part)
    nums = list(parse(v))
    nums[i] += 1
    nums[i + 1:] = [0] * (len(PARTS) - 1 - i)
    return ".".join(str(n) for n in nums)


def vscode_version(v: str) -> str:
    """The x.y.z that package.json carries: MAJOR.MINOR.(PATCH*1000+BUILD), so (1, 0, 0, 0) -> 1.0.0,
    (1, 0, 0, 7) -> 1.0.7, (1, 0, 2, 3) -> 1.0.2003. ValueError when BUILD would break the order."""
    major, minor, patch, build = parse(v)
    if build >= BUILD_LIMIT:
        raise ValueError(f"BUILD {build} of {v} must stay below {BUILD_LIMIT} for the VS Code version; bump PATCH")
    return f"{major}.{minor}.{patch * BUILD_LIMIT + build}"


def describe(version: str, info_path: Path = BUILD_INFO) -> str:
    """`--version`, the run log and report drafts: the full version plus the stamp build.py wrote
    (channel, commit, UTC build time); "(source)" when run from a checkout, which has no stamp."""
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"{version} (source)"
    except (OSError, ValueError):
        info = None
    if not isinstance(info, dict) or info.get("version") != version:
        return f"{version} (unknown build)"
    return f"{version} ({info.get('channel')} build {info.get('commit')}, {info.get('built_utc')})"
