"""(2026-10-21, docs/SPEC_V048_REPORT.md) `report`: a GitHub issue draft with the secrets taken out.

Nothing is sent from here: no token, no network call. The draft goes to <data dir>/reports/ and the
command prints a github.com "new issue" link with the title and body filled in. Only the owner's own
"Submit new issue" click on that page publishes it, after they have read it.

What a draft holds: the version, Python and Windows versions, the queue counts, the tail of
logs/errors.log (WARNING and up) and, with an item id, that item's status fields and error. Never a
prompt, a task's result, a run log (the whole conversation) or app.log. The errors.log lines and the
item's error CAN quote short pieces of Claude's own text (a blocked reason, an error message, a
stray output line); they go in redacted, not left out. Every text goes through redact(): the key
file's secret values and the secret environment variables, known key shapes (sk-ant-, ghp_, AKIA,
JWT, private key blocks …), NAME=value pairs whose name says key/token/secret/password,
Authorization headers, "password <value>", URL user:password@ and key-like query values, e-mail
addresses, the home folder (also its 8.3 short form), the user and PC names, the project folder and
every other absolute Windows path (cut to `<path>` plus its last part). (2026-10-27, security SEC-L7)
Also the project folder's bare name, the lane names, session UUIDs, the git user.name words, host
names and LAN addresses. check_clean() refuses a draft in which a known secret value survived.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .version import describe as describe_version

# the public repo of the source; a report only opens a prefilled page there, and until the repo exists
# that page is a 404 and nothing is posted. (2026-10-29, Clanker rename, SEC-L26) the slug follows the
# product name: the owner renames the GitHub repo himself (GitHub redirects the old name)
DEFAULT_REPO = "Bloodycub/Sleeping-clanker"
ENV_REPO = "CLAUDE_AUTOPILOT_REPORT_REPO"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
REPORTS_DIRNAME = "reports"
DEFAULT_LOG_LINES = 80
MAX_LOG_LINES = 400
# browsers and GitHub accept longer, but a new-issue link past ~8 KB is cut or refused
URL_MAX = 8000
TITLE_MAX = 120
ERROR_MAX = 1500
REDACTED = "[REDACTED]"
# a variable name that says it holds a secret
SECRET_NAME_RE = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|AUTH)")
SECRET_VALUE_MIN = 6  # a shorter "secret" (e.g. "1") would wipe ordinary text

# known key shapes; each match is replaced whole. (2026-10-27, security SEC-L7) Every shape
# scripts/export_public.py PATTERNS catches is caught here too (a header without its dashes, a 30+
# character AIza key, a JWT with a short signature), plus the hf_, glpat-, npm_, Stripe live/test,
# Bedrock ABSK, GLM (Zhipu), Google ya29 and PyPI shapes and chat webhook URLs;
# tests/test_report_redact_shapes.py pins the parity on one list of made-up samples.
_SHAPES = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?:-----)?[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    r"\bsk-ant-[A-Za-z0-9_\-]{8,}",
    r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_\-]{30,}",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+",
    r"\bhf_[A-Za-z0-9]{30,}",
    r"\bglpat-[A-Za-z0-9_\-]{20,}",
    r"\bnpm_[A-Za-z0-9]{30,}",
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",
    r"\bABSK[A-Za-z0-9+/=]{20,}",
    r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b",
    r"\bya29\.[A-Za-z0-9_\-]{20,}",
    r"\bpypi-[A-Za-z0-9_\-]{20,}",
    # (2026-10-27, SEC-L7 round 3) also without the scheme (the bare Slack hook host and path, or `//` first)
    # and with `\` or JSON `\/` separators, else the path rules cut it to `<path>\<secret>`
    # (2026-10-27, SEC-L7 review round 4) and Discord's versioned API path (api/v10/webhooks)
    r"(?i)(?:\bhttps?:|(?<![\w.\-]))[\\/]*(?:hooks\.slack\.com[\\/]+(?:services|workflows|triggers)"
    r"|(?:[\w-]+\.)?discord(?:app)?\.com[\\/]+api[\\/]+(?:v\d+[\\/]+)?webhooks"
    r"|[\w-]+\.webhook\.office\.com[\\/]+webhookb2"
    r"|outlook\.office(?:365)?\.com[\\/]+webhook)[\\/][^\s\"'<>]+",   # (2026-10-27, SEC-L7 round 2) old Teams form
)]
# "Bearer xxx", "token xxx": the word stays
_AUTH_WORD_RE = re.compile(r"(?i)\b(bearer|token|basic)(\s+)[A-Za-z0-9._~+/=\-]{12,}")
# NAME=value / NAME: value / "NAME": "value" with a secret-sounding NAME. (2026-10-27, security SEC-L7)
# also DB_PASS / SMTP_PW (a separator first, so "tests pass: 3" stays) and AUTH not followed by a letter
# (NPM_CONFIG__AUTH, DOCKER_AUTH_CONFIG; never "Authorization", whose scheme word stays, or "author").
# (2026-10-27, SEC-L7 review round 2) PASS/PW may also end a compound name (DBPASS, SMTPPW), but never
# ASKPASS (GIT_ASKPASS), BYPASS or COMPASS, and a bare word still needs no rule here ("tests pass: 3")
# (2026-10-27, SEC-L7 review round 3) also WEBHOOK, a PAT or DSN word ending the name (VSCE_PAT,
# SENTRY_DSN; never PATH or COMPAT) and a COOKIE after `_`/`.` or a letter (SESSION_COOKIE; the
# Cookie / Set-Cookie headers keep their own rule below)
_PAIR_RE = re.compile(r"""(?i)(["']?\b[A-Z0-9_.\-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PASSPHRASE"""
                      r"""|WEBHOOK|(?<![A-Z0-9])(?:PAT|DSN)[0-9]*\b|(?:[_.]|(?<=[A-Z0-9]))COOKIES?"""
                      r"""|(?:[_.\-]|(?<=[A-Z0-9])(?<!ASK)(?<!BY)(?<!COM))(?:PASS|PW)\b|AUTH(?![A-Z]))"""
                      r"""[A-Z0-9_]*["']?\s*[=:]\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;}\]]+)""")
# (2026-10-21, security review M4) "Authorization: <value>" (a scheme word stays), and a password
# word followed by its value with no "=" ("password hunter2", "pwd is x"); a few plain words stay
_AUTH_HEADER_RE = re.compile(r"""(?i)(\b(?:proxy-)?authori[sz]ation["']?\s*[:=]\s*["']?)"""
                             r"""((?:bearer|basic|token|digest|negotiate)\s+)?(?!\[REDACTED\])([^\s"',;}\]]+)""")
_PASSWORD_WORD_RE = re.compile(r"(?i)\b(passwords?|passphrases?|passwd|pwd|pw)(\s*[:=]\s*|\s+(?:is\s+|was\s+)?)"
                               r"(?!\[REDACTED\])([^\s,;}\]]+)")
_PASSWORD_KEEP = frozenset((
    "for", "is", "to", "the", "a", "an", "and", "or", "of", "in", "on", "at", "was", "not", "field",
    "prompt", "manager", "reset", "required", "missing", "set", "none", "null", "empty", "please",
    "again", "with", "from", "you", "your", "my", "it", "that", "this", "change", "changed", "wrong",
    "invalid", "incorrect", "expired", "file", "here", "there", "true", "false"))
# the 8.3 short form of a user folder (C:\Users\JOHNSM~1), in any of the slash forms logs use
_SEP = r"(?:\\\\|\\|/)"
_SHORT_HOME_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:" + _SEP + r"Users" + _SEP + r"""[^\\/\s"'<>|:*?]*~[0-9]+""")
# any other absolute Windows path (after the project and home folders are replaced): the folders go,
# the last part (the file name) stays. Inner folders may hold single spaces when a separator follows.
_SEG = r"""[^\\/\s:*?"'<>|`]+"""
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9%<])[A-Za-z]:" + _SEP + r"(?:" + _SEG + r"(?: +" + _SEG + r")*"
                          + _SEP + r")*(" + _SEG + r")?")
# (2026-10-21, re-test M-A) the folder names replaced before the path rules, and where such a name must end
_PATH_NAMES = ("<project>", "%USERPROFILE%")
_PART_END = r"""(?![^\\/\s"'<>|:*?`])"""
# (2026-10-21, tester round 2 M-1) more path shapes that carry client and project names. A part may
# hold runs of spaces when a separator follows it.
_SP_SEG = _SEG + r"(?: +(?!%)" + _SEG + r")*"  # a spaced part never runs into the next %USERPROFILE%
# the data dir's project folder `projects\<name>-<hash8>`: its slug is the project folder's name
_DATA_SLUG_RE = re.compile(r"(?i)\b(projects" + _SEP + r")[A-Za-z0-9._-]+-[0-9a-f]{8}(?![A-Za-z0-9._-])")
# (2026-10-21, re-test M-B) every path rule takes `\`, `/` and JSON `\\` as one separator class, in
# any mix (os.path.join(home, "Documents/Clients/Acme") logs `C:\Users\x\Documents/Clients/Acme`).
# folders under the home or project folder once its name is replaced: only the last part stays
_UNDER_NAME_RE = re.compile(r"(%USERPROFILE%|<project>)(" + _SEP + r")(?:" + _SP_SEG + _SEP + r")+(" + _SEG + r")")
# UNC `\\server\share\x` (also JSON-escaped `\\\\server\\share`, and `//server/share`), and a rooted
# `\clients\Acme\x` (3+ parts, no spaces)
_UNC_RE = re.compile(r"(?<![\w\\/:%>])(?:\\\\\\\\|\\\\|//)" + _SP_SEG + r"(?:" + _SEP + _SP_SEG + r")*"
                     + _SEP + r"(" + _SEG + r")")
# a folder name (no regex or shell punctuation such as `+`, `;`, `*`), and one with runs of spaces when a
# separator follows: `Kestrel Bank Oy\` (re-test M-B), but never `\d+\s+ in a message; line\n`
_NAME_SEG = r"[\w.$~@#&!=-]+"
_SP_NAME = _NAME_SEG + r"(?: +(?!%)" + _NAME_SEG + r")*"
_ROOTED_RE = re.compile(r"(?<![\w\\/:%>.~$-])\\" + _NAME_SEG + _SEP + r"(?:" + _SP_NAME + _SEP + r")+(" + _SEG + r")")
# Git Bash `/a/Projects/Acme/x.sh`, `/c/Users/<name>/x` (hooks run in Git Bash, so logs hold these)
_GITBASH_RE = re.compile(r"(?<![\w/.:~\\%>-])/[A-Za-z]" + _SEP + r"(?:" + _SP_SEG + _SEP + r")*(" + _SEG + r")")
# (re-test M-B / L-D) prefixes that hide a path from the rules above, taken off first:
# `file:///C:/x` (so the home and project folders are still seen) and
# `Microsoft.PowerShell.Core\FileSystem::\\server\share\x`. Any other `file://host/share/x` is a path
# token for the fail-safe below.
_FILE_URI_DRIVE_RE = re.compile(r"(?i)\bfile:[\\/]*(?=[A-Za-z]:[\\/])")
_PS_PROVIDER_RE = re.compile(r"(?i)(?:\bMicrosoft\.PowerShell\.Core(?:\\\\|\\))?\bFileSystem::")
# (re-test M-B) the fail-safe last pass: a token that still looks like a path (2+ separators, one inner
# part a name of 2+ characters) is cut to `<path>\last`; http(s) URLs are left alone (they are not paths)
_URL_TOKEN = r"""https?://[^\s"']*"""
_TOKEN_START = r"""(?<![^\s"'<>|()\[\]{},;`=])"""
_LAST = r"""[^\s"'<>|()\[\]{},;`\\/]*"""
# rooted (a separator, a drive or `file:` first): inner parts may hold spaces (`\d\Work\Kestrel Bank Oy\x`);
# relative (`lib/a/x.js`): no spaces, so `lib/x.js and owner/repo` stays two tokens
_PATH_TOKEN = (_TOKEN_START + r"(?:(?:[A-Za-z]:|file:)?[\\/]+(?:(?:" + _SP_NAME + r")?[\\/]+)+"
               + r"|[\w.$~@#&!=:-]+[\\/]+(?:(?:" + _NAME_SEG + r")?[\\/]+)+)" + _LAST)
_FAILSAFE_RE = re.compile(r"(?i)(" + _URL_TOKEN + r")|(" + _PATH_TOKEN + r")")
_NAME_PART_RE = re.compile(r"[A-Za-z0-9_.$~@#&!=: -]*[A-Za-z][A-Za-z0-9_.$~@#&!=: -]*")
# a Cookie / Set-Cookie header's whole value, and "cookie name=value"
_COOKIE_HEADER_RE = re.compile(r"""(?i)(\b(?:set-)?cookie["']?\s*[:=]\s*+)(?!\[REDACTED\])[^\r\n"']+""")
_COOKIE_WORD_RE = re.compile(r"""(?i)(\bcookies?\s+[A-Za-z0-9_.\-]+=)(?!\[REDACTED\])[^\s;,"']+""")
# "pass: x", "pass=x", "passcode: x" (only with ":" or "=": "pass the test" is ordinary text)
_PASS_COLON_RE = re.compile(r"(?i)\b(pass(?:code)?)(\s*[:=]\s*)(?!\[REDACTED\])([^\s,;}\]]+)")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@]+@")
_URL_QUERY_RE = re.compile(r"(?i)([?&](?:[a-z0-9_\-]*(?:key|token|secret|password|sig|signature)|code)=)"
                           r"(?!\[REDACTED\])[^&\s#]+")
# (tester round 2 M-1) also URL-encoded: name%40example.com
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+(?:@|%40)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# (2026-10-27, security SEC-L7) session ids (UUIDs), LAN addresses (the private and link-local IPv4
# ranges; loopback stays) and LAN host names (.local, .lan, .internal, .corp, .home.arpa ...; a file name
# such as settings.local.json is not one: no word, `-` or `.word` may follow)
_UUID_RE = re.compile(r"(?i)(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f-])"
                      r"|(?<![\w-])[0-9a-f]{32}(?![\w-])")   # (2026-10-27, SEC-L7 round 3) uuid.hex form
_LAN_IP_RE = re.compile(r"(?<![\w.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168|169\.254"
                        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"   # (2026-10-27, SEC-L7 round 2) + 100.64/10
                        r"\.\d{1,3}\.\d{1,3}(?!\.?\d)(?!\w)")
# (2026-10-27, SEC-L7 review round 2) the IPv6 unique-local (fc00::/7, mostly fd..) and link-local
# (fe80::/10) addresses, with an optional zone (%12, %eth0); the loopback and the public ranges stay
_LAN_IP6_RE = re.compile(r"(?i)(?<![\w:.])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f])(?::[0-9a-f]{0,4}){2,7}"
                         r"(?:%[0-9a-z]+)?(?![\w:])")
_MARKER_WORDS = frozenset(("path", "project", "project-name", "user", "pc", "host", "lane", "name", "uuid",
                           "lan-ip", "email", "redacted", "userprofile",
                           "lan", "ip"))   # (2026-10-27, SEC-L7 review) the parts of <lan-ip>
_LAN_HOST_RE = re.compile(r"(?i)(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
                          r"(?:local|lan|internal|intranet|corp|localdomain|home\.arpa)(?![\w-]|\.[\w-])"
                          # (2026-10-27, SEC-L7 round 3) such a label inside a public name (a build host
                          # under corp in a .com name); the last label is a known TLD: settings.local.json stays
                          r"|(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*(?:local|lan|internal|intranet|corp)\."
                          r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*(?:com|net|org|io|co|biz|info|cloud|eu|fi|se|no|dk"
                          r"|ee|de|nl|fr|uk|us|ca|ru)(?![\w-]|\.[\w-])")


class ReportError(Exception):
    pass


@dataclass
class Redactor:
    """Replaces secrets and personal names in text and counts what it replaced."""
    secrets: list[str] = field(default_factory=list)
    # (literal, replacement), longest first so a home path wins over the user name inside it
    names: list[tuple[str, str]] = field(default_factory=list)
    count: int = 0

    def redact(self, text: str) -> str:
        if not text:
            return text
        for s in self.secrets:
            if s in text:
                self.count += text.count(s)
                text = text.replace(s, REDACTED)
        text = self._sub(_DATA_SLUG_RE, lambda m: m.group(1) + "<project>", text)
        for rx in _SHAPES:
            text = self._sub(rx, lambda m: REDACTED, text)
        text = self._sub(_AUTH_HEADER_RE, lambda m: m.group(1) + (m.group(2) or "") + REDACTED, text)
        text = self._sub(_AUTH_WORD_RE, lambda m: m.group(1) + m.group(2) + REDACTED, text)
        text = self._sub(_PAIR_RE, _pair_repl, text)
        text = self._sub(_PASSWORD_WORD_RE, _password_repl, text)
        text = self._sub(_PASS_COLON_RE, _pass_repl, text)
        text = self._sub(_COOKIE_HEADER_RE, lambda m: m.group(1) + REDACTED, text)
        text = self._sub(_COOKIE_WORD_RE, lambda m: m.group(1) + REDACTED, text)
        text = self._sub(_URL_USERINFO_RE, lambda m: m.group(1) + REDACTED + "@", text)
        text = self._sub(_URL_QUERY_RE, lambda m: m.group(1) + REDACTED, text)
        text = self._sub(_EMAIL_RE, lambda m: "[email]", text)
        # (2026-10-21, re-test M-A) the home and project folders first, and only as a whole folder (a
        # path part must end there: `C:\Users\alice-old` is not the home); the user and PC names only
        # AFTER every path is shortened, else `D:\<user>\Clients\Acme` (`_SEG` refuses `<`) kept its folders
        people = [(lit, repl) for lit, repl in self.names if repl not in _PATH_NAMES]
        # (re-test M-B / L-D) `file:` and PowerShell provider prefixes go, so the path under them is seen
        text = self._sub(_FILE_URI_DRIVE_RE, lambda m: "", text)
        text = self._sub(_PS_PROVIDER_RE, lambda m: "", text)
        done: set[str] = set()
        for literal, repl in self.names:
            if repl in _PATH_NAMES:
                # (re-test M-B) the folder in any separator mix: its parts joined by any separator
                body = _SEP.join(re.escape(p) for p in re.split(r"[\\/]+", literal))
                if body in done:
                    continue
                done.add(body)
                rx = re.compile(r"(?i)(?<![A-Za-z0-9])" + body + _PART_END)
                text = self._sub(rx, lambda m, r=repl: r, text)
        text = self._sub(_SHORT_HOME_RE, lambda m: "%USERPROFILE%", text)
        text = self._sub(_UNDER_NAME_RE, lambda m: m.group(1) + m.group(2) + "<path>" + m.group(2) + m.group(3),
                         text)
        text = self._sub(_ABS_PATH_RE, _path_repl, text)
        text = self._sub(_UNC_RE, _path_repl, text)
        text = self._sub(_ROOTED_RE, _path_repl, text)
        text = self._sub(_GITBASH_RE, lambda m: "<path>/" + m.group(1), text)
        text = self._failsafe(text)
        # (2026-10-27, security SEC-L7) after the path rules, like the names below (`_SEG` refuses `<`)
        text = self._sub(_UUID_RE, lambda m: "<uuid>", text)
        text = self._sub(_LAN_IP_RE, lambda m: "<lan-ip>", text)
        text = self._sub(_LAN_IP6_RE, lambda m: "<lan-ip>", text)
        text = self._sub(_LAN_HOST_RE, lambda m: "<host>", text)
        for literal, repl in people:
            rx = re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(literal) + r"(?![A-Za-z0-9])")
            text = self._sub(rx, lambda m, r=repl: r, text)
        return text

    def _failsafe(self, text: str) -> str:
        """(re-test M-B) The last pass: whatever still looks like a path loses its folders."""
        def repl(m: re.Match) -> str:
            tok = m.group(2)
            if tok is None:  # an http(s) URL: not a path
                return m.group(0)
            parts = re.split(r"[\\/]+", tok)
            if not any(len(p) >= 2 and _NAME_PART_RE.fullmatch(p) for p in parts[:-1]):
                return tok  # `s/a/b/`, `\d+\s+`: not a path
            self.count += 1
            return "<path>" + ("\\" + parts[-1] if parts[-1] else "")
        return _FAILSAFE_RE.sub(repl, text)

    def _sub(self, rx: re.Pattern, repl, text: str) -> str:
        out, n = rx.subn(repl, text)
        # a pair whose value is only a number is kept as it is (max_tokens=5000) and not counted
        self.count += n - out.count("\x00keep\x00")
        return out.replace("\x00keep\x00", "")

    def check_clean(self, text: str) -> None:
        """Fail loudly if a known secret value is still in `text` (in any letter case or URL-encoded)."""
        low = text.lower()
        for s in self.secrets:
            if s.lower() in low or quote(s, safe="").lower() in low:
                raise ReportError("a secret value survived the redaction; no draft was written")


def _pair_repl(m: re.Match) -> str:
    value = m.group(2).strip("\"'")
    if not value or value.startswith(REDACTED[:-1]) or re.fullmatch(r"[0-9.,:\-]+|true|false|none|null|set|missing",
                                                       value, re.I):
        return m.group(0) + "\x00keep\x00"
    quote_char = m.group(2)[0] if m.group(2)[:1] in ("'", '"') else ""
    return m.group(1) + quote_char + REDACTED + quote_char


def _password_repl(m: re.Match) -> str:
    if m.group(3).lower().strip(".:!?") in _PASSWORD_KEEP:
        return m.group(0) + "\x00keep\x00"
    return m.group(1) + m.group(2) + REDACTED


def _pass_repl(m: re.Match) -> str:
    """(tester round 2 M-1) "pass: hunter2" goes; "pass: 3" (a test count) and plain words stay."""
    value = m.group(3).lower().strip(".:!?")
    if value in _PASSWORD_KEEP or re.fullmatch(r"[0-9]{1,4}(?:/[0-9]{1,4})?", value):
        return m.group(0) + "\x00keep\x00"
    return m.group(1) + m.group(2) + REDACTED


def _path_repl(m: re.Match) -> str:
    """(2026-10-21, security review M4) `A:\\Clients\\Acme\\infra\\db.sql` -> `<path>\\db.sql`."""
    last = m.group(1) or ""
    return "<path>" + ("\\" + last if last else "")


def short_path(p: str) -> str | None:
    """The 8.3 short form of an existing folder (GetShortPathNameW), or None when there is none."""
    if os.name != "nt" or not p:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.kernel32.GetShortPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        n = fn(p, buf, len(buf))
    except Exception:  # noqa: BLE001 - no short name is not an error
        return None
    if not n or n >= len(buf):
        return None
    short = buf.value
    return short if short and short.lower() != p.rstrip("\\/").lower() else None


def _path_forms(p: str) -> list[str]:
    """A Windows path as logs write it: backslashes, forward slashes and JSON-escaped backslashes."""
    p = p.rstrip("\\/")
    forms = [p, p.replace("\\", "/"), p.replace("\\", "\\\\")]
    # (tester round 2 M-1) the Git Bash form: C:\Users\x -> /c/Users/x
    m = re.match(r"^([A-Za-z]):[\\/](.+)$", p)
    if m:
        forms.append("/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/"))
    return list(dict.fromkeys(forms))


def _git_name_words(env, project_root: str | None) -> list[str]:
    """(2026-10-27, security SEC-L7) The words (3+ letters) of the git user.name: GIT_AUTHOR_NAME,
    GIT_COMMITTER_NAME and `[user] name` in the git config files of `env`'s home and of the project.
    The files are only read (no git process); a missing or unreadable one is skipped.
    (2026-10-27, SEC-L7 review round 2) Also every file an `[include]` or `[includeIf "..."]` path= names
    (followed whatever its condition, at most _GIT_INCLUDE_DEPTH deep and _GIT_CONFIG_FILES_MAX files),
    and the config of a worktree or submodule whose `.git` is a file (_project_git_configs)."""
    found = [env.get("GIT_AUTHOR_NAME") or "", env.get("GIT_COMMITTER_NAME") or ""]
    home = env.get("USERPROFILE") or env.get("HOME") or ""
    files = []
    if home:
        files.append(Path(home) / ".gitconfig")
    xdg = env.get("XDG_CONFIG_HOME") or (str(Path(home) / ".config") if home else "")
    if xdg:
        files.append(Path(xdg) / "git" / "config")
    if project_root:
        files += _project_git_configs(project_root)
    queue = [(f, 0) for f in files]
    seen: set[str] = set()
    while queue and len(seen) < _GIT_CONFIG_FILES_MAX:
        f, depth = queue.pop(0)
        key = os.path.normcase(os.path.abspath(str(f)))
        if key in seen:
            continue
        seen.add(key)
        try:
            if not f.is_file() or f.stat().st_size > 256 * 1024:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section = ""
        for line in text.splitlines():
            s = line.strip()
            head = re.match(r"\[([^\]]*)\]\s*(.*)$", s)
            if head:
                # a key may follow the header on the same line: `[user] name = x`
                section, s = head.group(1).strip().lower(), head.group(2)
            m = re.match(r"(?i)(name|path)\s*=\s*(.*)$", s)
            if not m:
                continue
            value = m.group(2).split("#")[0].split(";")[0].strip().strip('"')
            if m.group(1).lower() == "name" and section == "user":
                found.append(value)
            elif (m.group(1).lower() == "path" and value and depth < _GIT_INCLUDE_DEPTH
                  and (section == "include" or section.startswith("includeif"))):
                if value.startswith("~") and home:
                    queue.append((Path(home) / value[1:].lstrip("\\/"), depth + 1))
                else:
                    queue.append((f.parent / value, depth + 1))   # an absolute path replaces the base
    return sorted({w for w in re.findall(r"[^\W\d_]{3,}", " ".join(found))})


# (2026-10-27, SEC-L7 review round 2) git's own include depth limit, and a cap on the files read
_GIT_INCLUDE_DEPTH = 10
_GIT_CONFIG_FILES_MAX = 32


def _project_git_configs(project_root: str) -> list[Path]:
    """(2026-10-27, SEC-L7 review round 2) The project's git config files: `.git/config`, or, when `.git`
    is a file (`gitdir: <dir>`, a worktree or submodule), that dir's config.worktree and config and the
    config of its common dir (the `commondir` file, a worktree's main `.git`). Only read, never run."""
    dotgit = Path(project_root) / ".git"
    try:
        if dotgit.is_dir():
            return [dotgit / "config"]
        if not dotgit.is_file() or dotgit.stat().st_size > 4096:
            return []
        m = re.match(r"(?i)gitdir:\s*(.+)", dotgit.read_text(encoding="utf-8", errors="replace").strip())
        if not m:
            return []
        gitdir = Path(project_root) / m.group(1).strip()   # an absolute gitdir replaces the base
        out = [gitdir / "config.worktree", gitdir / "config"]
        common = gitdir / "commondir"
        if common.is_file() and common.stat().st_size <= 4096:
            out.append(gitdir / common.read_text(encoding="utf-8", errors="replace").strip() / "config")
        return out
    except OSError:
        return []


def lane_names(data_dir: Path | str | None, lane: str | None = None) -> list[str]:
    """(2026-10-27, security SEC-L7) The project's non-default lane (queue) names, found from a lane's or
    the project's data dir, plus `lane`: they can name a client, so a draft shows them as <lane>."""
    from .store import LANES_DIRNAME, is_default_lane, list_lanes
    names = set()
    if data_dir:
        d = Path(data_dir)
        project_dir = d.parent.parent if d.parent.name.lower() == LANES_DIRNAME else d
        names = {n for n, _ in list_lanes(project_dir) if not is_default_lane(n)}
    if lane and not is_default_lane(lane):
        names.add(str(lane))
    return sorted(names)


def build_redactor(key_values: dict[str, str], env: dict | None = None,
                   project_root: str | None = None, lanes: list[str] | None = None) -> Redactor:
    """The secret values of the key file and of the environment, plus the personal names to hide.
    (2026-10-27, security SEC-L7) Also the project folder's bare name, the `lanes`, the git user.name
    words and the host names, each as a whole word after every path rule."""
    from .providers import SECRET_VARS, is_secret_env_name
    env = os.environ if env is None else env
    found: set[str] = set()
    for source in (key_values, env):
        for name, value in source.items():
            value = (value or "").strip()
            # (2026-10-27, security SEC-M8/L7) plus the child-env rule (GITHUB_PAT, SENTRY_DSN, DATABASE_URL)
            if len(value) < SECRET_VALUE_MIN or not (name in SECRET_VARS or SECRET_NAME_RE.search(name)
                                                     or is_secret_env_name(name)):
                continue
            # a word, a list of variable names or a number is a setting ("enabled",
            # "ANTHROPIC_API_KEY", "3600"), not a key: keys carry digits
            if name not in SECRET_VARS and re.fullmatch(r"[A-Za-z_]+(?:,[A-Za-z_]+)*|[0-9.]+", value):
                continue
            found.add(value)
    names: list[tuple[str, str]] = []
    if project_root:
        names += [(f, "<project>") for f in _path_forms(str(project_root))]
    home = env.get("USERPROFILE") or env.get("HOME") or ""
    if home and len(home) > 3:
        names += [(f, "%USERPROFILE%") for f in _path_forms(home)]
        # (2026-10-21, security review M4) C:\Users\JOHNSM~1 too (Temp paths are often written so)
        short = short_path(home)
        if short and len(short) > 3:
            names += [(f, "%USERPROFILE%") for f in _path_forms(short)]
    for var, repl in (("USERNAME", "<user>"), ("USER", "<user>"), ("COMPUTERNAME", "<pc>")):
        v = (env.get(var) or "").strip()
        if len(v) >= 3:
            names.append((v, repl))
    # (2026-10-27, security SEC-L7) host names (Git Bash's HOSTNAME, the domain, the logon server), the
    # project folder's own name, the lanes and the git user.name words: whole words of 3+ characters,
    # never one of the placeholder words themselves (a lane called `path` would mangle `<path>`)
    words = [((env.get("HOSTNAME") or "").strip(), "<pc>"), ((env.get("USERDOMAIN") or "").strip(), "<host>"),
             ((env.get("LOGONSERVER") or "").strip().lstrip("\\"), "<host>")]
    if project_root:
        words.append((re.split(r"[\\/]+", str(project_root).rstrip("\\/"))[-1], "<project-name>"))
    words += [(lane, "<lane>") for lane in (lanes or ())]
    words += [(w, "<name>") for w in _git_name_words(env, project_root)]
    seen = {lit.lower() for lit, _ in names}
    for lit, repl in words:
        if len(lit) >= 3 and ":" not in lit and lit.lower() not in seen and lit.lower() not in _MARKER_WORDS:
            seen.add(lit.lower())
            names.append((lit, repl))
    names.sort(key=lambda t: len(t[0]), reverse=True)
    return Redactor(secrets=sorted(found, key=len, reverse=True), names=names)


def resolve_repo(flag: str | None, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    repo = (flag or env.get(ENV_REPO) or DEFAULT_REPO).strip()
    if not REPO_RE.match(repo):
        raise ReportError(f"repo must be OWNER/NAME, got {repo!r}")
    return repo


def log_tail(data_dir: Path, lines: int) -> list[str]:
    """The last `lines` lines of <data dir>/logs/errors.log (WARNING and up); [] when there is none."""
    if lines <= 0:
        return []
    path = Path(data_dir) / "logs" / "errors.log"
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))  # enough for MAX_LOG_LINES long lines
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    if size > 256 * 1024 and rows:
        rows = rows[1:]  # the first row may start mid-line
    return rows[-lines:]


def item_lines(item: dict) -> list[str]:
    """An item's status fields. No prompt, result, cwd or session id; denials by tool name only."""
    rows = [f"- id: {item.get('id')}"]
    for key in ("kind", "status", "profile", "attempts", "limit_waits", "rounds", "max_rounds",
                "num_turns", "billing", "created_at", "started_at", "finished_at"):
        if item.get(key) not in (None, "", []):
            rows.append(f"- {key}: {item.get(key)}")
    denials = item.get("permission_denials") or []
    if denials:
        tools = sorted({str(d.get("tool_name", "?")) if isinstance(d, dict) else "?" for d in denials})
        rows.append(f"- permission_denials: {len(denials)} ({', '.join(tools)})")
    err = item.get("error")
    if err:
        err = str(err)
        if len(err) > ERROR_MAX:
            err = err[:ERROR_MAX] + " …"
        rows += ["- error:", "", "```text", err.replace("```", "'''"), "```"]
    return rows


def default_title(log_rows: list[str], item: dict | None) -> str:
    if item and item.get("status") in ("failed", "blocked"):
        return f"autopilot {__version__}: item {item.get('status')}"
    for row in reversed(log_rows):
        parts = row.split(" ", 4)  # time LEVEL module run_id message
        if len(parts) == 5 and parts[1] in ("ERROR", "CRITICAL"):
            return f"autopilot {__version__}: {parts[4]}"
    return f"autopilot {__version__}: problem report"


def _fence(rows: list[str]) -> list[str]:
    return ["```text", *(r.replace("```", "'''") for r in rows), "```"]


def body_text(*, description: str, counts: dict, runner_alive: bool, lane: str, log_rows: list[str],
              log_note: str, item: dict | None, redactions: int) -> str:
    rows = ["## What happened", "", description.strip() or "(say what you did, what you expected and "
            "what happened instead)", "", "## Environment", "",
            f"- sleeping_clanker {describe_version(__version__)}",  # (2026-10-26) + channel, commit, build time
            f"- Python {platform.python_version()} on {platform.system()} {platform.version()}",
            f"- queue `{lane}`: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v) +
            ("" if any(counts.values()) else "empty"),
            f"- runner alive: {'yes' if runner_alive else 'no'}"]
    if item is not None:
        rows += ["", "## Item", "", *item_lines(item)]
    rows += ["", f"## errors.log ({log_note})", ""]
    rows += _fence(log_rows) if log_rows else ["(no warnings or errors logged)"]
    rows += ["", "---", f"_Made by `autopilot report`: {redactions} secret or personal value(s) "
             "replaced; prompts, task results and run logs are not included, but the errors.log lines "
             "and the item's error can quote short pieces of Claude's text (redacted)._"]
    return "\n".join(rows) + "\n"


def issue_url(repo: str, title: str, body: str) -> str:
    return (f"https://github.com/{repo}/issues/new?title={quote(title, safe='')}"
            f"&body={quote(body, safe='')}")


@dataclass
class Report:
    file: Path
    url: str
    title: str
    repo: str
    redactions: int
    log_lines: int
    url_log_lines: int


def make_report(*, data_dir: Path, lane: str, counts: dict, runner_alive: bool, key_values: dict,
                repo: str, now_stamp: str, title: str | None = None, description: str = "",
                item: dict | None = None, lines: int = DEFAULT_LOG_LINES,
                project_root: str | None = None, env: dict | None = None) -> Report:
    """Build, redact and write the draft; return its path and the prefilled new-issue link."""
    # (2026-10-27, security SEC-L7) every lane name of the project is hidden, the run's own one too
    lanes = lane_names(data_dir, lane)
    red = build_redactor(key_values, env, project_root, lanes)
    lane = "<lane>" if lane in lanes else lane
    log_rows = [red.redact(r) for r in log_tail(data_dir, lines)]
    item_r = None
    if item is not None:
        item_r = {k: (red.redact(v) if isinstance(v, str) else v) for k, v in item.items()
                  if k not in ("prompt", "result", "cwd", "session_id", "attachments")}
    description = red.redact(description or "")
    title = red.redact((title or "").strip()) or red.redact(default_title(log_rows, item_r))
    title = " ".join(title.split())[:TITLE_MAX]

    def body(rows: list[str], note: str) -> str:
        return body_text(description=description, counts=counts, runner_alive=runner_alive, lane=lane,
                         log_rows=rows, log_note=note, item=item_r, redactions=red.count)

    # count the redactions first, then render (the footer shows the count)
    full = body(log_rows, f"last {len(log_rows)} line(s)")
    path = Path(data_dir) / REPORTS_DIRNAME / f"report-{now_stamp}.md"
    # the link carries as many of the newest log lines as fit; the file always has them all
    keep = len(log_rows)
    url = issue_url(repo, title, full)
    while len(url) > URL_MAX and keep > 0:
        keep = keep // 2
        note = (f"last {keep} of {len(log_rows)} line(s); the rest is in "
                f"{REPORTS_DIRNAME}/{path.name}, drag it into the issue")
        url = issue_url(repo, title, body(log_rows[len(log_rows) - keep:] if keep else [], note))
    short = body([], f"too long for a link: attach {REPORTS_DIRNAME}/{path.name}")
    while len(url) > URL_MAX and short:  # a very long description or item error: cut the text itself
        short = short[:int(len(short) * 0.8)]
        url = issue_url(repo, title, short + f"\n\n(cut; the whole report is {REPORTS_DIRNAME}/{path.name})")
    red.check_clean(full + title)
    red.check_clean(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{full}", encoding="utf-8", newline="\n")
    return Report(file=path, url=url, title=title, repo=repo, redactions=red.count,
                  log_lines=len(log_rows), url_log_lines=keep)


def stdout_safe(text: str) -> str:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(enc, errors="replace").decode(enc, errors="replace")
