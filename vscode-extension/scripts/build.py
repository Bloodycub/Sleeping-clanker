"""Build the Sleeping Clanker VSIX (2026-10-13, SPEC_VSCODE section 6). Standard library only.

1. Sync the Python core: src/sleeping_clanker -> vscode-extension/python/sleeping_clanker (never by hand).
   (2026-10-26, 0.4.13) A stale python/<OLD_CORE_NAMES> copy (the module name until 0.4.12) is removed
   first, and verify() refuses any VSIX entry with that name, so the old core can never ship again.
   (2026-10-29, Clanker rename) The same for sleeping_claud, the module name from 0.4.13 until the rename.
2. Generate icon.png (128x128) if it is missing.
3. Package with the stdlib zip builder, which writes a valid VSIX. (2026-10-27, security SEC-M10) It is
   the default now: `--vsce` opts in to `npx --yes @vscode/vsce@<VSCE_VERSION> package --no-dependencies`,
   whose ~200 unpinned sub-dependencies run install scripts as you; if vsce fails, the zip builder runs.
   A symbolic link or junction in the core or the extension folder refuses the build (SEC-M3).
4. Verify the VSIX: list every entry, refuse forbidden paths and key/credential/log file names.
   (2026-10-27, security SEC-M3) Every entry must also be a git-tracked file (vscode-extension/ without
   test/ and scripts/, or src/sleeping_clanker/; only the manifest, [Content_Types].xml and build_info.json
   are exempt), and the content of every entry must pass scripts/export_public.py's scrub (the same
   check as the public export). A checkout without git or without scripts/export_public.py (the public
   clone) skips what it cannot check with a warning; --release refuses instead (2026-10-29, lens A LOW-2:
   also when the owner's real name is unknown to the scrub, as export_public.py refuses). A refused VSIX is
   renamed *.REFUSED.vsix (SEC-L27). (2026-10-27) It is built and verified as *.UNVERIFIED.vsix and gets
   its real name only after verify() passed, so an interrupted build never leaves the real name.
(2026-10-26) Versions: the one source is __version__ in src/sleeping_clanker/__init__.py (four parts,
MAJOR.MINOR.PATCH.BUILD; src/sleeping_clanker/version.py). --bump PART raises it there first; package.json
always gets the derived VS Code form (vsce refuses four parts); python/sleeping_clanker/build_info.json
stamps the channel (development, testing, or production only with --release), commit and UTC time.

Usage: python vscode-extension/scripts/build.py [--bump build|patch|minor|major | --release]
       [--vsce | --fallback-only] [--list-only VSIX]
Output: dist/sleeping-clanker-<full version>.vsix (git-ignored; the extension id stays bloodycub.claud-autopilot).
(2026-10-27, security SEC-L27) Only a production (--release) build gets that name; a testing build is
dist/sleeping-clanker-<full version>-test.vsix and a development build ...-dev.vsix.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

EXT = Path(__file__).resolve().parents[1]
REPO = EXT.parent
CORE_SRC = REPO / "src" / "sleeping_clanker"
CORE_DST = EXT / "python" / "sleeping_clanker"
# (2026-10-26, 0.4.13) the module was renamed; its old synced copy must never be bundled again
# (2026-10-29, Clanker rename) renamed again (sleeping_claud -> sleeping_clanker): both old names
OLD_CORE_NAMES = ("sleeping_claud", "claud_autopilot")
STALE_CORE_DSTS = tuple(EXT / "python" / name for name in OLD_CORE_NAMES)
VSIX_STEM = "sleeping-clanker"  # the file name only: package.json "name" (the extension id) is unchanged
DIST = REPO / "dist"
# (2026-10-26) the version: its one source line, the derived package.json line, the release notes
PKG_JSON = EXT / "package.json"
CHANGELOG = REPO / "CHANGELOG.md"
VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"', re.M)
PKG_VERSION_LINE = re.compile(r'^  "version": "([^"]*)"', re.M)
VSCE_VERSION = "3.2.1"  # pinned (2026-10-13): released 2024-10, well past the 2-week age rule
# (2026-10-27, security SEC-M3) + .claude: an agent folder (settings.local.json) never ships
FORBIDDEN_PARTS = ("upstream", "tests", "test", ".venv", "__pycache__", "node_modules", ".git", ".claude")
# Same rules as .vscodeignore, for the fallback builder.
# (2026-10-21, 0.4.4 tester fixes MED-5) + key and secret files: the fallback builder had no .env rule
# (2026-10-27, security SEC-M3) + agent folders, logs and the credential names of SECRET_NAMES
IGNORE = ("test/**", "scripts/**", "node_modules/**", "**/__pycache__/**", "**/*.pyc", ".vscode/**",
          "*.vsix", "dist/**", ".vscodeignore",
          ".env", ".env.*", "**/.env", "**/.env.*", "**/*.env", "**/*.pem", "**/*.key",
          "**/providers.env", "**/*.secret",
          ".claude/**", "**/.claude/**", "logs/**", "**/logs/**", "**/*.log", "**/*.jsonl", "**/.credentials*",
          "**/credentials*", "**/id_rsa*", "**/id_dsa*", "**/id_ecdsa*", "**/id_ed25519*", "**/.npmrc",
          "**/.pypirc", "**/.git-credentials", "**/*.p12", "**/*.pfx", "**/*.zip")
# a file with one of these NAMES (any folder) never ships and makes verify() refuse the VSIX
# (2026-10-27, security SEC-M3) + the names scripts/export_public.py's NEVER refuses: credential and SSH
# key files, npm/PyPI/git credentials, certificates, transcripts, logs, archives and photos
SECRET_NAMES = (".env", ".env.*", "*.env", "*.pem", "*.key", "providers.env", "*.secret",
                ".credentials*", "credentials*", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc",
                ".pypirc", ".git-credentials", "*.p12", "*.pfx", "*.jsonl", "*.log", "*.zip", "*.vsix",
                "*.jpg", "*.jpeg", "*.gif", "*.ico")
# (2026-10-27, security SEC-M3) the VSIX entries that are not a copy of a git-tracked file
ALLOW_EXEMPT = ("extension.vsixmanifest", "[Content_Types].xml", "extension/python/sleeping_clanker/build_info.json")
SCRIPTS = REPO / "scripts"   # scripts/export_public.py: the scrub verify() runs over the VSIX content


def secret_name(rel: str) -> bool:
    """(2026-10-21, 0.4.4 tester fixes MED-5) True when the file NAME of `rel` is a key/secret file."""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_NAMES)


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# ---- 0. version (2026-10-26, owner task 38040b3c: MAJOR.MINOR.PATCH.BUILD) ------------------------
def _version_module():
    """src/sleeping_clanker/version.py of this checkout: its src/ goes first on sys.path, then a plain import
    (no importlib: the network contract forbids dynamic imports in shipped code)."""
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from sleeping_clanker import version
    return version


def set_version(bump: str | None = None) -> tuple[str, str]:
    """The full version from its one source (raised by `bump` first, if given) and its VS Code form,
    which is written into package.json whenever it differs. Bytes and line endings are kept."""
    v = _version_module()
    init = CORE_SRC / "__init__.py"
    text = init.read_bytes().decode("utf-8")
    m = VERSION_LINE.search(text)
    if not m:
        raise SystemExit(f"no __version__ line in {init}")
    try:
        full = v.bump(m.group(1), bump) if bump else m.group(1)
        vscode = v.vscode_version(full)
    except ValueError as exc:
        raise SystemExit(f"version: {exc}") from None
    if full != m.group(1):
        init.write_bytes((text[:m.start(1)] + full + text[m.end(1):]).encode("utf-8"))
        log(f"version {m.group(1)} -> {full} in {init}")
    raw = PKG_JSON.read_bytes().decode("utf-8")
    p = PKG_VERSION_LINE.search(raw)
    if not p:
        raise SystemExit(f'no top-level "version" line in {PKG_JSON}')
    if p.group(1) != vscode:
        PKG_JSON.write_bytes((raw[:p.start(1)] + vscode + raw[p.end(1):]).encode("utf-8"))
        log(f"package.json version {p.group(1)} -> {vscode} (the VS Code form of {full})")
    if json.loads(PKG_JSON.read_text(encoding="utf-8"))["version"] != vscode:
        raise SystemExit(f"package.json version is not {vscode}")
    return full, vscode


def _which(name: str) -> str | None:
    """(2026-10-27, security SEC-L8) A program from an ABSOLUTE PATH folder only: shutil.which and a bare
    "git" search the current folder first on Windows, so a planted git.exe or npx.cmd there would run.
    A copy of the core's claude_path.which_on_path (not imported: a venv may import another checkout's
    older core first): `.`, empty, relative and drive-relative entries and the cwd itself are skipped."""
    try:
        here = os.path.normcase(os.path.abspath(os.getcwd()))
    except OSError:
        here = None
    names = [name]
    if sys.platform == "win32":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
        if not any(name.lower().endswith(e.lower()) for e in exts):
            names = [name + e for e in exts]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        d = entry.strip().strip('"')
        if not d or not os.path.isabs(d) or (sys.platform == "win32" and not os.path.splitdrive(d)[0]):
            continue
        if here is not None and (os.path.normcase(os.path.abspath(d)) == here or _same_dir(d, here)):
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def _same_dir(a: str, b: str) -> bool:
    """(2026-10-27, S3S4 review 2) `a` is the folder `b` under another spelling (an 8.3 short name, a
    junction or a symbolic link): the string compare in _which missed the cwd's 8.3 name."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _git(*args: str) -> str | None:
    """(2026-10-27) Local, read-only git in this checkout (git from an absolute PATH folder): its stdout,
    or None without git, outside a checkout or on an error."""
    exe = _which("git")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-C", str(REPO), *args], shell=False, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def git_commit() -> str:
    """This checkout's 12-hex commit, + "-dirty" with uncommitted changes to tracked files; "" without
    git. Local and read-only (`git describe`; --exclude=* so a tag never replaces the hash)."""
    out = _git("describe", "--always", "--dirty", "--abbrev=12", "--exclude=*")
    return out.strip() if out else ""


def allow_list() -> set[str] | None:
    """(2026-10-27, security SEC-M3) The VSIX entry names (lower case) a git-tracked file may become:
    vscode-extension/<x> (not test/ or scripts/) -> extension/<x> (LICENSE -> LICENSE.txt) and
    src/sleeping_clanker/<x> -> extension/python/sleeping_clanker/<x>. None without git or a checkout.
    An untracked file never ships, even one .gitignore hides from git status (.claude/, logs/)."""
    out = _git("ls-files", "-z", "--", "vscode-extension", "src/sleeping_clanker")
    if out is None:
        return None
    allowed = {n.lower() for n in ALLOW_EXEMPT}
    for f in out.split("\0"):
        if f.startswith("src/sleeping_clanker/"):
            allowed.add(("extension/python/" + f[len("src/"):]).lower())
        elif f.startswith("vscode-extension/") and not f.startswith(("vscode-extension/test/", "vscode-extension/scripts/")):
            rel = f[len("vscode-extension/"):]
            allowed.add(("extension/" + ("LICENSE.txt" if rel == "LICENSE" else rel)).lower())
    return allowed


def changelog_entry(full: str) -> str:
    """The CHANGELOG.md section of `full`: its "## <full> - " heading up to the next "## "; "" if none."""
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(rf"^## {re.escape(full)} - .*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0) if m else ""


def build_channel(full: str, commit: str, release: bool) -> str:
    """development (uncommitted changes, or no git) or testing (a clean commit); production only with
    --release, which also needs a dated CHANGELOG.md entry with a Verification line, else SystemExit."""
    clean = bool(commit) and not commit.endswith("-dirty")
    if not release:
        return "testing" if clean else "development"
    entry = changelog_entry(full)
    if not clean:
        why = "uncommitted changes (or no git): a production build must match a commit"
    elif not re.match(rf"## {re.escape(full)} - \d{{4}}-\d{{2}}-\d{{2}}\b", entry):
        why = f"CHANGELOG.md has no dated entry '## {full} - YYYY-MM-DD'"
    elif "Verification:" not in entry:
        why = f"the CHANGELOG.md entry of {full} has no 'Verification:' line (gate counts, tester verdict)"
    else:
        return "production"
    raise SystemExit(f"--release refused: {why}")


def write_build_info(full: str, vscode: str, channel: str, commit: str) -> dict:
    """python/sleeping_clanker/build_info.json, shown by `autopilot --version` and the extension's log.
    Only the VSIX copy gets it (sync_core() makes a fresh copy first), never src/."""
    info = {"version": full, "vscode_version": vscode, "channel": channel, "commit": commit or "unknown",
            "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (CORE_DST / "build_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    log(f"build info: {info}")
    return info


# ---- 1. core sync ------------------------------------------------------------------------------
def sync_core() -> int:
    if not (CORE_SRC / "__main__.py").is_file():
        raise SystemExit(f"core not found at {CORE_SRC}")
    for stale in STALE_CORE_DSTS:   # (2026-10-26, 0.4.13) generated by an older build.py; never edited by hand
        if stale.exists():
            shutil.rmtree(stale)
            log(f"removed the stale old-name core copy {stale}")
    if CORE_DST.exists():
        shutil.rmtree(CORE_DST)
    shutil.copytree(CORE_SRC, CORE_DST, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    n = sum(1 for p in CORE_DST.rglob("*") if p.is_file())
    log(f"synced core: {CORE_SRC} -> {CORE_DST} ({n} files)")
    return n


# ---- 2. icon -------------------------------------------------------------------------------------
def _png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rgba_rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def make_icon(path: Path, size: int = 128) -> None:
    """Rounded dark-blue square with a white 'play' triangle and an orange dot (queue runner)."""
    bg, fg, accent = (30, 58, 95), (255, 255, 255), (242, 140, 40)
    radius = 22
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            # rounded-corner mask
            cx = min(max(x, radius), size - 1 - radius)
            cy = min(max(y, radius), size - 1 - radius)
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
            if not inside:
                row += bytes((0, 0, 0, 0))
                continue
            color = bg
            # play triangle: from (44,34) to (44,94) to (96,64)
            if 44 <= x <= 96 and abs(y - 64) <= (96 - x) * 30 / 52:
                color = fg
            if (x - 36) ** 2 + (y - 98) ** 2 <= 9 ** 2:
                color = accent
            row += bytes((*color, 255))
        rows.append(bytes(row))
    path.write_bytes(_png(size, size, rows))
    log(f"generated {path.name} ({size}x{size})")


# ---- 3a. vsce --------------------------------------------------------------------------------------
def _npx_argv() -> list[str] | None:
    node = _which("node")   # (2026-10-27, security SEC-L8) never a node.exe/npx.cmd in the current folder
    if node:
        cli = Path(node).resolve().parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
        if cli.is_file():
            return [node, str(cli)]  # avoids npx.cmd, so cmd.exe never re-parses the arguments
    npx = _which("npx")
    return [npx] if npx else None


def build_with_vsce(out: Path) -> bool:
    npx = _npx_argv()
    if not npx:
        log("vsce: npx not found")
        return False
    env = dict(os.environ)  # a TLS-inspecting proxy or antivirus needs NODE_EXTRA_CA_CERTS set by the caller
    argv = [*npx, "--yes", f"@vscode/vsce@{VSCE_VERSION}", "package", "--no-dependencies",
            "--allow-missing-repository", "--out", str(out)]
    log("vsce: " + " ".join(argv))
    try:
        r = subprocess.run(argv, cwd=EXT, env=env, shell=False, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"vsce: could not run: {exc}")
        return False
    for line in (r.stdout + r.stderr).splitlines():
        log(f"vsce| {line}")
    if r.returncode != 0 or not out.is_file():
        log(f"vsce: failed (exit {r.returncode})")
        return False
    return True


# ---- 3b. stdlib fallback --------------------------------------------------------------------------
def _is_link(path: Path) -> bool:
    """(2026-10-27, security SEC-M3) A symbolic link or (Windows) a junction or other reparse point, as
    scripts/export_public.is_link: copytree and the zip builder would package whatever it points at."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def links_under(root: Path, skip=lambda rel: False) -> list[str]:
    """(2026-10-27, security SEC-M3) Every link under `root` (relative, never followed); a folder or file
    `skip(rel)` is true for is not looked at (what the VSIX never packages)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for name in sorted(dirnames) + sorted(filenames):
            rel = (here / name).relative_to(root).as_posix()
            if skip(rel) or (name in dirnames and skip(rel + "/x")):   # "x": a folder pattern like **/a/**
                continue
            if _is_link(here / name):
                found.append(rel)
            elif name in dirnames:
                keep.append(name)
        dirnames[:] = keep   # a junction is never walked into (os.walk only skips symlinks)
    return found


def _ignored(rel: str) -> bool:
    if secret_name(rel):
        return True
    for pat in IGNORE:
        if fnmatch.fnmatch(rel, pat):
            return True
        if pat.startswith("**/") and (fnmatch.fnmatch(rel, pat[3:]) or fnmatch.fnmatch("/" + rel, "*/" + pat[3:])):
            return True
        if pat.endswith("/**") and (rel == pat[:-3] or rel.startswith(pat[:-2])):
            return True
    return False


def _manifest(pkg: dict) -> str:
    e = lambda s: escape(str(s), {'"': "&quot;"})  # noqa: E731
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{e(pkg['name'])}" Version="{e(pkg['version'])}" Publisher="{e(pkg['publisher'])}" />
    <DisplayName>{e(pkg.get('displayName', pkg['name']))}</DisplayName>
    <Description xml:space="preserve">{e(pkg.get('description', ''))}</Description>
    <Tags></Tags>
    <Categories>{e(','.join(pkg.get('categories', [])))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{e(pkg['engines']['vscode'])}" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace" />
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExecutesCode" Value="true" />
      <Property Id="Microsoft.VisualStudio.Services.GitHubFlavoredMarkdown" Value="true" />
      <Property Id="Microsoft.VisualStudio.Services.Content.Pricing" Value="Free"/>
    </Properties>
    <License>extension/LICENSE.txt</License>
    <Icon>extension/icon.png</Icon>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/icon.png" Addressable="true" />
  </Assets>
</PackageManifest>
"""


CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension=".json" ContentType="application/json"/><Default Extension=".vsixmanifest" ContentType="text/xml"/><Default Extension=".js" ContentType="application/javascript"/><Default Extension=".md" ContentType="text/markdown"/><Default Extension=".txt" ContentType="text/plain"/><Default Extension=".png" ContentType="image/png"/><Default Extension=".svg" ContentType="image/svg+xml"/><Default Extension=".py" ContentType="text/x-python"/>
</Types>
"""


def build_fallback(out: Path) -> None:
    pkg = json.loads((EXT / "package.json").read_text(encoding="utf-8"))
    files = sorted(p for p in EXT.rglob("*") if p.is_file() and not _ignored(p.relative_to(EXT).as_posix()))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", _manifest(pkg))
        for p in files:
            rel = p.relative_to(EXT).as_posix()
            if rel == "LICENSE":
                rel = "LICENSE.txt"  # vsce renames it the same way
            z.write(p, "extension/" + rel)
    log(f"fallback builder wrote {out} ({len(files)} extension files)")


# ---- 4. verify -------------------------------------------------------------------------------------
def export_scrub():
    """(2026-10-27, security SEC-M3) scripts/export_public.py of this checkout (the public export's scrub:
    PATTERNS, png_text, scrub()), or None where it is missing: the public clone does not carry it. A plain
    import from scripts/ (no importlib: the network contract forbids dynamic imports here)."""
    script = SCRIPTS / "export_public.py"
    if not script.is_file():
        return None
    if str(SCRIPTS) not in sys.path:
        sys.path.append(str(SCRIPTS))   # appended: nothing in scripts/ may shadow a stdlib module
    import export_public
    if Path(export_public.__file__).resolve() != script.resolve():
        raise SystemExit(f"verify: imported {export_public.__file__}, not {script}")
    return export_public


def content_hits(vsix: Path) -> list[str] | None:
    """(2026-10-27, security SEC-M3) The export scrub over every entry of the finished VSIX (extracted to a
    temp folder): keys, env-style secrets, e-mails, private and real names, private paths, non-UTF-8
    files, PNG text and eXIf. The declared four-part version (__init__.py, build_info.json) is not an
    "ip address". None when the scrub is not available."""
    ep = export_scrub()
    if ep is None:
        return None
    with tempfile.TemporaryDirectory(prefix="vsix-scan-") as tmp:
        with zipfile.ZipFile(vsix) as z:
            z.extractall(tmp)
        hits = ep.scrub(Path(tmp))
        init = Path(tmp) / "extension" / "python" / "sleeping_clanker" / "__init__.py"
        text = init.read_text("utf-8", "replace") if init.is_file() else ""
    versions = {a or b for a, b in ep.DECLARED_VERSION.findall(text)}
    return [h for h in hits if not any(h.endswith(f": ip address: {v}") for v in versions)]


def name_unknown() -> bool:
    """(2026-10-29, delta sweep lens A LOW-2) The scrub cannot look for the owner's real name: the global git
    identity holds no real-name words and there are no remembered name hashes (export_public.NAME_STORE).
    export_public.main() refuses an export then; verify() warns, and refuses --release. False when the scrub
    is not available (that is reported as its own unchecked item)."""
    ep = export_scrub()
    return ep is not None and not ep.personal_words() and not ep.name_hashes()[1]


def verify(vsix: Path, release: bool = False) -> list[str]:
    with zipfile.ZipFile(vsix) as z:
        bad = z.testzip()
        if bad:
            raise SystemExit(f"corrupt entry in {vsix}: {bad}")
        names = z.namelist()
    for n in names:
        print(f"  {n}")
    forbidden = [n for n in names if any(part in FORBIDDEN_PARTS for part in n.split("/")[:-1])
                 or secret_name(n)   # (2026-10-21, 0.4.4 tester fixes MED-5)
                 or any(old in n for old in OLD_CORE_NAMES)]   # (2026-10-26, 0.4.13) a pre-rename core never ships
    # (2026-10-27, security SEC-M3) an allow-list (git-tracked files) and the export's content scan; what
    # cannot be checked here (no git, no scripts/export_public.py) is a warning, and refuses --release
    allowed, hits, unchecked = allow_list(), content_hits(vsix), []
    untracked = [] if allowed is None else [n for n in names if n.lower() not in allowed]
    if allowed is None:
        unchecked.append("the git-tracked allow-list (no git or no checkout)")
    if hits is None:
        unchecked.append("the content scan (no scripts/export_public.py: the public clone)")
    elif name_unknown():
        # (2026-10-29, delta sweep lens A LOW-2) export_public.main()'s guard: the scan ran without the owner's
        # real name (a noreply global identity and no remembered hashes), so it could not look for it
        unchecked.append("the owner's real name (unknown: the global git identity holds no real-name words and "
                         "there are no remembered name hashes; run scripts/export_public.py --remember-name once)")
    for what in unchecked:
        log(f"{'REFUSED' if release else 'WARNING'}: not checked: {what}")
    for n in untracked:
        log(f"NOT TRACKED: {n}")
    for h in hits or []:
        log(f"SCAN: {h}")
    required = ["extension.vsixmanifest", "[Content_Types].xml", "extension/package.json",
                "extension/extension.js", "extension/python/sleeping_clanker/__main__.py",
                "extension/python/sleeping_clanker/__init__.py",
                # (2026-10-15, v0.3) the live log renderer and its grammar
                "extension/lib/transcript.js", "extension/lib/livelog.js", "extension/lib/click.js",
                "extension/syntaxes/autopilot-log.tmLanguage.json",
                # (2026-10-17, 0.4.0) the Task view: its pure modules, the host panel and the webview files
                "extension/lib/markdown.js", "extension/lib/chatmodel.js", "extension/lib/chatview.js",
                "extension/lib/taskview.js", "extension/media/chat.js", "extension/media/chat.css",
                # (2026-10-21) the UI language: runtime strings and the command titles per language
                "extension/lib/i18n.js", "extension/package.nls.json", "extension/package.nls.ru.json",
                "extension/package.nls.fi.json",
                # (2026-10-21, add panel) the "+ Add new task" panel: host module and webview files
                "extension/lib/addpanel.js", "extension/media/addpanel.js", "extension/media/addpanel.css",
                # (2026-10-26, integrate) every other lib module extension.js loads, so a missing one fails
                # the build instead of the activation (tests/test_rename_0413.py checks lib/*.js against this)
                "extension/lib/argv.js", "extension/lib/format.js", "extension/lib/fullaccess.js",
                "extension/lib/parse.js", "extension/lib/pickers.js", "extension/lib/poller.js",
                "extension/lib/proc.js", "extension/lib/split.js", "extension/lib/stats.js",
                "extension/lib/view.js", "extension/lib/activity.js", "extension/lib/move.js",
                "extension/lib/limithold.js", "extension/lib/usage.js"]
    missing = [r for r in required if r not in names]
    log(f"VSIX {vsix} size={vsix.stat().st_size} bytes entries={len(names)} "
        f"forbidden={len(forbidden)} missing={len(missing)} untracked={len(untracked)} "
        f"scan={'skipped' if hits is None else len(hits)}")
    for n in forbidden:
        log(f"FORBIDDEN: {n}")
    for n in missing:
        log(f"MISSING: {n}")
    if forbidden or missing or untracked or hits or (release and unchecked):
        raise SystemExit(1)
    return names


def main(argv: list[str] | None = None) -> int:
    # (2026-10-13) vsce prints box-drawing characters; a cp1252 console must not crash the build.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # (2026-10-27, security SEC-M10) the stdlib zip builder is the default; vsce (unpinned npm tree) opts in
    builder = ap.add_mutually_exclusive_group()
    builder.add_argument("--vsce", action="store_true",
                         help="package with npx @vscode/vsce (fetches ~200 unpinned npm packages); else the zip builder")
    builder.add_argument("--fallback-only", action="store_true", help="the stdlib zip builder (the default now)")
    ap.add_argument("--list-only", metavar="VSIX", help="only verify and list an existing VSIX")
    # (2026-10-26) the four-part version: bump it (and stop), or stamp a production build
    once = ap.add_mutually_exclusive_group()
    once.add_argument("--bump", choices=("build", "patch", "minor", "major"),
                      help="raise this version part (lower parts reset to 0), update package.json and exit")
    once.add_argument("--release", action="store_true",
                      help="stamp the build production: needs a clean commit and a dated CHANGELOG.md entry")
    args = ap.parse_args(argv)
    if args.list_only:
        verify(Path(args.list_only))
        return 0
    if args.bump:
        full, vscode = set_version(args.bump)
        log(f"version is now {full} (VS Code {vscode}); add its CHANGELOG.md entry, commit, then build")
        return 0
    full, vscode = set_version()
    commit = git_commit()
    channel = build_channel(full, commit, args.release)
    # (2026-10-27, security SEC-M3) a link would package whatever it points at (python/ is re-synced below)
    links = ([f"src/sleeping_clanker/{r}" for r in links_under(CORE_SRC, lambda r: "__pycache__" in r.split("/"))]
             + [f"vscode-extension/{r}" for r in links_under(EXT, lambda r: _ignored(r) or r.split("/")[0] == "python")])
    if links:
        raise SystemExit("refused: a link is never packaged; replace it with the real file:\n  " + "\n  ".join(links))
    sync_core()
    write_build_info(full, vscode, channel, commit)
    icon = EXT / "icon.png"
    if not icon.is_file():
        make_icon(icon)
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{VSIX_STEM}-{full}.vsix"  # (2026-10-26) the full four-part version, not the VS Code form
    if channel != "production":   # (2026-10-27, security SEC-L27) only a --release build gets the release name
        out = out.with_name(f"{VSIX_STEM}-{full}-{'test' if channel == 'testing' else 'dev'}.vsix")
    refused = out.with_name(out.stem + ".REFUSED.vsix")
    # (2026-10-27, S3S4 review 3) the VSIX is written and verified as <stem>.UNVERIFIED.vsix (out) and takes
    # its real name (final) only after verify() passed: a build interrupted inside the builder (Ctrl+C, a
    # kill) left a valid zip under the release name that was never verified
    final, out = out, out.with_name(out.stem + ".UNVERIFIED.vsix")
    for old in (final, refused, out):
        if old.exists():
            old.unlink()
    try:
        if not args.vsce or not build_with_vsce(out):
            if args.vsce:
                log("falling back to the stdlib VSIX builder")
            build_fallback(out)
        verify(out, release=args.release)
    except BaseException:   # (S3S4) any failure: BadZipFile, OSError, Ctrl+C, not only a refusal
        if out.exists():
            out.replace(refused)   # (2026-10-27, SEC-L27) a refused VSIX never keeps an installable name
            log(f"REFUSED: the VSIX is now {refused}")
        raise
    out.replace(final)
    log(f"OK {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
