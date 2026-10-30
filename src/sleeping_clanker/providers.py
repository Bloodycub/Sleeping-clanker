r"""Model providers and their API keys, from one key file (2026-10-20, docs/SPEC_V047_PROVIDERS.md).

Every model key lives in ONE .env-format file, `providers.env`, in the autopilot data dir
(%LOCALAPPDATA%\claud_autopilot\, or a project's data dir), never in a repo: (2026-10-21, 0.4.4 tester
fixes MED-3) a task that can write its repo must not be able to route the next run to its own endpoint
(`.env.example` in the repo is only the template). `run --provider NAME` picks which of them a
run uses, and ONLY that provider's key is put
into the claude.exe child env: a DeepSeek run never sees the OpenAI key.

How a non-Anthropic model reaches claude.exe: the CLI speaks the Anthropic Messages wire format, so a
third-party model is used through that provider's Anthropic-compatible endpoint
(ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN) with that provider's model id. A provider whose API is
NOT Anthropic-compatible (OpenAI/Codex) needs a translating gateway, so its base URL is empty by
default and the run is refused until the .env names one.

A selection also REMOVES the conflicting auth variables (the other key route, a stale
ANTHROPIC_BASE_URL, CLAUDE_CODE_USE_BEDROCK/_VERTEX/_FOUNDRY) from the child env, so a key left in the
owner's real environment cannot silently win over the chosen provider. The default provider
(`claude`, the claude.ai subscription) sets and removes NOTHING: the env reaching claude.exe is then
byte-identical to 0.4.6 and the owner's own variables stay their choice (DECISIONS.md, "API env
warning").

NOT VERIFIED LIVE (2026-10-20): the built-in base URLs and model ids come from each provider's docs,
not from a probe on this machine (no third-party key was available). Each is overridable in the .env
(`<PREFIX>_BASE_URL`, `<PREFIX>_MODEL`), so a stale default costs an edit, not a release.

(2026-10-26, network audit MEDIUM) Every base URL a selection would put into ANTHROPIC_BASE_URL, built
in or from the .env, is checked first (base_url_problem): https only, plain http only to the loopback
host, no credentials, ?query or #fragment in it. A URL that fails refuses the provider with a
ProviderError (the CLI's usage error, exit 2), before its key is put anywhere.

Keys are never logged, printed or put in a repr: log lines and `providers` name the VARIABLE only.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import MutableMapping
from urllib.parse import urlsplit, urlunsplit

from .log import get_logger

log = get_logger(__name__)

# (2026-10-21, 0.4.4 tester fixes MED-3) the key file in a data dir; profiles.KEY_FILE_NAME denies it to tools
ENV_FILE_NAME = "providers.env"
# the source checkout's .env, read by 0.4.4 drafts: now only reported (one WARNING), never read
CHECKOUT_ENV = Path(__file__).resolve().parents[2] / ".env"
ENV_FILE_VAR = "CLAUDE_AUTOPILOT_ENV_FILE"
# (2026-10-21, 0.4.4 tester fixes MED-4) the NAMES (never values) of the key variables apply() put into
# the env; runner.provider_key_refusal() reads it so no shell-capable profile runs with a key in its env
KEY_VARS_MARKER = "CLAUDE_AUTOPILOT_PROVIDER_KEY_VARS"
# provider variables that hold a secret (the key routes and the AWS secrets passed verbatim)
SECRET_VARS = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                         "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                         "AWS_SESSION_TOKEN"})
ENV_PROVIDER = "CLAUDE_AUTOPILOT_PROVIDER"
# a "key file" this big is something else (a dump, a log): read none of it
ENV_FILE_MAX = 64 * 1024
DEFAULT_PROVIDER = "claude"

# the claude.exe variables a selection drives
BASE_URL_VAR = "ANTHROPIC_BASE_URL"
API_KEY_VAR = "ANTHROPIC_API_KEY"
AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
# every variable that can route claude.exe somewhere else; the ones a selection does not set are
# removed from the child env (see the module docstring)
CONFLICT_VARS = (BASE_URL_VAR, API_KEY_VAR, AUTH_TOKEN_VAR, "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_MODEL",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL")


class ProviderError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    """One place a model can come from. `key_vars`: the .env names that may hold its key, first set
    one wins. `auth_var`: the claude.exe variable the key is passed as (empty = pass it under its own
    name, as AWS credentials are). `models`: known ids, the first is the default."""

    name: str
    label: str
    key_vars: tuple[str, ...] = ()
    base_url: str = ""
    models: tuple[str, ...] = ()
    auth_var: str = AUTH_TOKEN_VAR
    pass_vars: tuple[str, ...] = ()      # extra .env vars copied verbatim when set
    extra_env: tuple[tuple[str, str], ...] = ()
    needs_base_url: bool = False         # no built-in endpoint: the .env must give one
    needs_model: bool = False            # no built-in model id: the .env must give one
    supports_effort: bool = True         # --effort / --max-budget-usd are Anthropic-model features
    note: str = ""

    @property
    def prefix(self) -> str:
        """`claude-api` -> `CLAUDE_API`, the prefix of its .env override variables."""
        return re.sub(r"[^A-Z0-9]", "_", self.name.upper())

    @property
    def base_url_var(self) -> str:
        return f"{self.prefix}_BASE_URL"

    @property
    def model_var(self) -> str:
        return f"{self.prefix}_MODEL"

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""


# (2026-10-20) The model ids of Claude's own providers; `run --model` overrides any of them.
# (2026-10-21, v049 merge) THE one list of current Claude model ids, in this order; the first is the
# default (== runner.DEFAULT_MODEL). The extension keeps a copy (lib/argv.js TASK_MODELS) that
# vscode-extension/test/v049_merge.test.js checks against this line. An older id a stored item or
# `run --model` still holds (claude-opus-5, claude-sonnet-5, claude-haiku-4-5-20251001, ...) is not
# offered but keeps working: a model id is only checked for its shape (runner.TASK_MODEL_RE).
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        "claude", "Claude Code on the claude.ai subscription (default, no key)",
        models=CLAUDE_MODELS, auth_var="",
        note="no API key: claude.exe uses its own /login session. Nothing is added to or removed "
             "from the child env, so runs stay on the subscription.",
    ),
    Provider(
        "claude-api", "Claude through an Anthropic API key (billed per token)",
        key_vars=("ANTHROPIC_API_KEY",), models=CLAUDE_MODELS, auth_var=API_KEY_VAR,
        note="same models as `claude`, paid from the Console key instead of the subscription.",
    ),
    Provider(
        "deepseek", "DeepSeek", key_vars=("DEEPSEEK_API_KEY",),
        base_url="https://api.deepseek.com/anthropic",
        models=("deepseek-chat", "deepseek-reasoner"), supports_effort=False,
    ),
    Provider(
        "kimi", "Moonshot Kimi", key_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        base_url="https://api.moonshot.ai/anthropic",
        models=("kimi-k2-turbo-preview", "kimi-k2-0905-preview"), supports_effort=False,
    ),
    Provider(
        "glm", "Z.ai GLM", key_vars=("ZAI_API_KEY", "GLM_API_KEY"),
        base_url="https://api.z.ai/api/anthropic",
        models=("glm-4.6", "glm-4.5-air"), supports_effort=False,
    ),
    Provider(
        "qwen", "Alibaba Qwen (DashScope)", key_vars=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        base_url="https://dashscope-intl.aliyuncs.com/api/v2/apps/claude-code-proxy",
        models=("qwen3-coder-plus",), supports_effort=False,
    ),
    Provider(
        "codex", "OpenAI / Codex models through an Anthropic-compatible gateway",
        key_vars=("OPENAI_API_KEY", "CODEX_API_KEY"), needs_base_url=True,
        models=("gpt-5.1-codex", "gpt-5.1"), supports_effort=False,
        note="OpenAI's API is NOT Anthropic-compatible, so claude.exe cannot call it directly: put "
             "a translating gateway (e.g. a local LiteLLM proxy) in CODEX_BASE_URL.",
    ),
    Provider(
        "bedrock", "Claude on AWS Bedrock", key_vars=("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID"),
        auth_var="", pass_vars=("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
                                "AWS_DEFAULT_REGION", "AWS_PROFILE"),
        extra_env=(("CLAUDE_CODE_USE_BEDROCK", "1"),), needs_model=True,
        note="Bedrock model ids are region-specific: set BEDROCK_MODEL in providers.env.",
    ),
    Provider(
        "custom", "Any other Anthropic-compatible endpoint", key_vars=("CUSTOM_API_KEY",),
        needs_base_url=True, needs_model=True, supports_effort=False,
        note="the escape hatch: set CUSTOM_BASE_URL, CUSTOM_MODEL and CUSTOM_API_KEY in providers.env.",
    ),
)
PROVIDER_NAMES = tuple(p.name for p in PROVIDERS)


def provider(name: str) -> Provider:
    for p in PROVIDERS:
        if p.name == str(name).strip().lower():
            return p
    raise ProviderError(f"unknown provider {name!r}; choose one of {', '.join(PROVIDER_NAMES)} "
                        "(`autopilot providers` lists them and which keys are set)")


# ---- the .env file ---------------------------------------------------------------------------------
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _value(raw: str) -> str:
    """One .env value: quotes stripped, no interpolation. An unquoted ` #...` tail is a comment."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def load_env_file(path: str | os.PathLike | None) -> dict[str, str]:
    """The KEY=VALUE pairs of `path` (missing file = {}). Fails SOFT and QUIETLY: an unreadable file
    or a malformed line is logged by its line NUMBER only (the text may be a key) and skipped. Plain
    `KEY=VALUE`, an optional `export ` prefix, `#` comments, single or double quotes; no `${...}`."""
    if path is None:
        return {}
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    if size > ENV_FILE_MAX:
        log.warning("env file %s is %d bytes (over %d): not read as a key file", path, size, ENV_FILE_MAX)
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        log.warning("env file %s unreadable (%s); no keys loaded", path, exc)
        return {}
    out: dict[str, str] = {}
    for num, raw in enumerate(text.splitlines(), 1):   # splitlines: CRLF and LF alike
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line[:7].lower() == "export ":
            line = line[7:].lstrip()
        key, eq, value = line.partition("=")
        key = key.strip()
        if not eq or not _KEY_RE.match(key):
            log.warning("env file %s line %d: not KEY=VALUE, skipped", path, num)
            continue
        out[key] = _value(value)
    log.debug("env file %s: %d variable(s): %s", path, len(out), ", ".join(sorted(out)))
    return out


def env_file_candidates(explicit: str | os.PathLike | None = None,
                        project_dir: str | os.PathLike | None = None,
                        env: MutableMapping[str, str] | dict | None = None,
                        base_dir: str | os.PathLike | None = None) -> list[Path]:
    r"""Where the key file is looked for, in order: --env-file, $CLAUDE_AUTOPILOT_ENV_FILE (both the
    owner's explicit choice), then `providers.env` in the project's data dir (per project) and in the
    base data dir (%LOCALAPPDATA%\claud_autopilot, one file for every project). The data dirs are
    outside every repo.

    (2026-10-21, 0.4.4 tester fixes MED-3) No repo is ever searched: not the cwd, and no longer the
    source checkout's `.env` either. When the autopilot works on its own repo the checkout IS the task
    cwd, and a task could Write `./.env` to route the next run (every prompt and file) to any endpoint.
    A checkout .env is only reported (warn_checkout_env).
    """
    env = os.environ if env is None else env
    out: list[Path] = []
    for raw in (explicit, (env.get(ENV_FILE_VAR) or "").strip()):
        if raw:
            out.append(Path(raw))
    for folder in (project_dir, base_dir):
        if folder:
            out.append(Path(folder) / ENV_FILE_NAME)
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def load_keys(explicit: str | os.PathLike | None = None,
              project_dir: str | os.PathLike | None = None,
              env: MutableMapping[str, str] | dict | None = None,
              base_dir: str | os.PathLike | None = None
              ) -> tuple[Path | None, dict[str, str], list[Path]]:
    """(the .env used, its values, every candidate). An explicit --env-file that does not exist is a
    ProviderError; a missing file anywhere else just means "no keys from a file"."""
    candidates = env_file_candidates(explicit, project_dir, env, base_dir)
    warn_checkout_env()
    if explicit and not Path(explicit).is_file():
        raise ProviderError(f"env file {explicit} does not exist")
    for path in candidates:
        if path.is_file():
            return path, load_env_file(path), candidates
    return None, {}, candidates


_checkout_warned: list[bool] = []


def warn_checkout_env(path: Path | None = None) -> bool:
    """(2026-10-21, 0.4.4 tester fixes MED-3) One WARNING per process when the source checkout has a .env:
    it is NOT read any more. The path is logged, never a byte of the file. True when it warned now."""
    path = CHECKOUT_ENV if path is None else Path(path)
    if _checkout_warned or not path.is_file():
        return False
    _checkout_warned.append(True)
    log.warning("providers: %s is IGNORED (a repo file a task could write); move the keys to "
                "providers.env in the autopilot data dir (%%LOCALAPPDATA%%\\claud_autopilot\\providers.env) "
                "and delete it", path)
    return True


def redact_url(url: str | None) -> str:
    """(2026-10-21, 0.4.4 tester fixes LOW-4) A base URL safe to log or print: no user:password@, no
    ?query and no #fragment (either may carry a key)."""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        return "<unparsable url>"
    if not parts.scheme and not host:
        return "<unparsable url>"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


# (2026-10-26, network audit MEDIUM) plain http is accepted only to this host name or a loopback IP
LOOPBACK_HOST = "localhost"


def _is_loopback(host: str) -> bool:
    """`host` (urlsplit's hostname: lower case, no [brackets]) is localhost or a loopback IP address
    (IPv4 127/8 or the IPv6 loopback). A name that only resolves to one is not: no lookup is made."""
    if host == LOOPBACK_HOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def base_url_problem(url: str | None) -> str | None:
    """(2026-10-26, network audit MEDIUM) Why `url` must not become ANTHROPIC_BASE_URL, or None when it
    may. The provider's key is sent to that endpoint, so: the scheme is https; plain http only to the
    loopback host (the key never leaves the machine, e.g. a local translating gateway); no
    user:password@, ?query or #fragment (each can carry a key); printable ASCII without spaces or
    backslashes (two URL parsers must not read different hosts). The reason never repeats the URL."""
    raw = str(url or "")
    if not raw:
        return "it is empty"
    if not (raw.isascii() and raw.isprintable()) or " " in raw or "\\" in raw:
        return "it holds a space, a backslash, a control or a non-ASCII character"
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        parts.port   # noqa: B018 - raises ValueError for a port that is not a number in range
    except ValueError:
        return "it is not a valid URL"
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        return "the scheme must be https"
    if not host:
        return "it names no host"
    if "@" in parts.netloc:
        return "it holds credentials (user:password@); the key belongs in the provider's key variable"
    if "?" in raw or "#" in raw:
        return "it holds a ?query or #fragment, which may carry a key"
    if scheme == "http" and not _is_loopback(host):
        return "plain http is allowed only to localhost or a loopback IP address; use https"
    return None


def _shown_url(url: str) -> str:
    """A refused base URL as it may be shown: redacted, and only when it is printable ASCII."""
    shown = redact_url(url)
    return shown if shown.isascii() and shown.isprintable() else "<unprintable url>"


def select_name(flag: str | None = None, values: dict[str, str] | None = None,
                env: MutableMapping[str, str] | dict | None = None) -> str:
    """The provider a run uses: `--provider` > the real environment > the .env > `claude`."""
    env = os.environ if env is None else env
    for raw in (flag, env.get(ENV_PROVIDER), (values or {}).get(ENV_PROVIDER)):
        if raw and str(raw).strip():
            return str(raw).strip().lower()
    return DEFAULT_PROVIDER


# ---- resolving one selection ----------------------------------------------------------------------
@dataclass(frozen=True)
class Selection:
    """What one provider choice does: the model to pass, and the child-env changes it needs."""

    provider_name: str
    model: str
    base_url: str = ""
    key_var: str | None = None
    supports_effort: bool = True
    env: dict[str, str] = field(default_factory=dict)   # variables to SET in the child env
    unset: tuple[str, ...] = ()                         # variables to REMOVE from the child env

    @property
    def secret_vars(self) -> tuple[str, ...]:
        """(2026-10-21, 0.4.4 tester fixes MED-4) The variables of `env` that hold a key."""
        return tuple(sorted(k for k in self.env if k in SECRET_VARS))

    @property
    def is_default(self) -> bool:
        return self.provider_name == DEFAULT_PROVIDER

    def __repr__(self) -> str:
        """Values are redacted: a Selection must be safe to log, repr or put in a traceback."""
        names = ", ".join(f"{k}=<set>" for k in sorted(self.env))
        return (f"Selection(provider={self.provider_name!r}, model={self.model!r}, "
                f"base_url={redact_url(self.base_url)!r}, key_var={self.key_var!r}, env={{{names}}}, "
                f"unset={self.unset!r})")


def key_status(p: Provider, values: dict[str, str]) -> tuple[str | None, bool]:
    """(the .env variable that holds this provider's key, whether it is set). The first declared
    variable is reported when none is set; (None, False) for a provider that needs no key."""
    for var in p.key_vars:
        if (values.get(var) or "").strip():
            return var, True
    return (p.key_vars[0] if p.key_vars else None), False


def missing_for(p: Provider, values: dict[str, str]) -> list[str]:
    """The .env variables this provider still needs before a run can use it."""
    var, have = key_status(p, values)
    out: list[str] = []
    if var and not have:
        out.append(" or ".join(p.key_vars))
    if p.needs_base_url and not (values.get(p.base_url_var) or p.base_url).strip():
        out.append(p.base_url_var)
    if p.needs_model and not (values.get(p.model_var) or p.default_model).strip():
        out.append(p.model_var)
    return out


def resolve(name: str, values: dict[str, str] | None = None, model: str | None = None) -> Selection:
    """The Selection for provider `name`, or a ProviderError naming exactly what the .env still needs.

    `model` (from `run --model`) wins over `<PREFIX>_MODEL` and over the built-in default.
    """
    p = provider(name)
    values = values or {}
    base_url = (values.get(p.base_url_var) or p.base_url).strip()
    chosen = (str(model).strip() if model else "") or (values.get(p.model_var) or "").strip() \
        or p.default_model
    # a `run --model` value satisfies a provider that has no built-in model id
    missing = [m for m in missing_for(p, values) if not (m == p.model_var and chosen)]
    if missing:
        msg = f"provider {p.name}: set {', '.join(missing)} in providers.env"
        raise ProviderError(f"{msg}; {p.note}" if p.note else msg)
    if not chosen:
        raise ProviderError(f"provider {p.name}: no model; set {p.model_var} in providers.env or pass "
                            "`run --model`")
    # (2026-10-26, network audit MEDIUM) the endpoint is checked before the key goes anywhere
    problem = base_url_problem(base_url) if base_url else None
    if problem:
        source = p.base_url_var if (values.get(p.base_url_var) or "").strip() else "the built-in base URL"
        raise ProviderError(f"provider {p.name}: {source} {_shown_url(base_url)} refused: {problem}; "
                            "its key was not used")
    key_var, have_key = key_status(p, values)
    child: dict[str, str] = {}
    if base_url:
        child[BASE_URL_VAR] = base_url
    if key_var and have_key:
        child[p.auth_var or key_var] = values[key_var].strip()
    for var in p.pass_vars:
        extra = (values.get(var) or "").strip()
        if extra:
            child[var] = extra
    child.update(dict(p.extra_env))
    # the default provider touches nothing (see the module docstring)
    unset = () if p.name == DEFAULT_PROVIDER else tuple(v for v in CONFLICT_VARS if v not in child)
    return Selection(provider_name=p.name, model=chosen, base_url=base_url,
                     key_var=key_var if have_key else None, supports_effort=p.supports_effort,
                     env=child, unset=unset)


def apply(sel: Selection, env: MutableMapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Put `sel` into `env` (default os.environ), which runner.child_env() copies into the claude.exe
    child. Returns (variables set, variables removed) for the log; VALUES ARE NEVER RETURNED."""
    target = os.environ if env is None else env
    for key in sorted(sel.env):
        target[key] = sel.env[key]
    removed = [key for key in sel.unset if key in target]
    for key in removed:
        del target[key]
    # (2026-10-21, 0.4.4 tester fixes MED-4) the key NAMES, so no shell profile is started with them
    if sel.secret_vars:
        target[KEY_VARS_MARKER] = ",".join(sel.secret_vars)
    else:
        target.pop(KEY_VARS_MARKER, None)
    return sorted(sel.env), removed


def needs_key(name: str) -> bool:
    """(2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) Whether provider `name` puts an API key into
    the child env: every provider but the subscription (`claude`) has key variables."""
    return bool(provider(name).key_vars)


def attempt_env(base: MutableMapping[str, str] | dict, run_set, sel: Selection
                ) -> tuple[dict[str, str], list[str], list[str]]:
    """(2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) The env of ONE attempt whose item chose its own
    provider `sel`: a COPY of `base` (the runner's env, with the run's provider already applied) minus the
    variables the run's selection set (`run_set`, names) and its key marker, with `sel` applied on top.
    `base` itself is never changed, so the next item without a provider gets the runner's env back.
    Returns (env, variables set, variables removed); the lists are NAMES only."""
    env = dict(base)
    dropped = [k for k in (*tuple(run_set or ()), KEY_VARS_MARKER) if k in env]
    for key in dropped:
        del env[key]
    was_set, removed = apply(sel, env)
    removed = sorted(set(removed) | {k for k in dropped if k != KEY_VARS_MARKER and k not in sel.env})
    return env, was_set, removed


def key_vars_in(env: MutableMapping[str, str] | dict) -> list[str]:
    """(2026-10-21, 0.4.4 tester fixes MED-4) The provider key variables apply() put into `env` that are
    still set there (names only). (2026-10-26, security L2) Plus every other secret-named variable still
    set (is_secret_env_name), e.g. the owner's own ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN: the
    shell refusal and the log redaction treat them as keys too. (2026-10-27, SEC-M8 review round 3) Also
    a variable whose VALUE carries a credential (is_secret_env)."""
    names = [n.strip() for n in (env.get(KEY_VARS_MARKER) or "").split(",")]
    names += sorted(k for k in env if k not in names and k != KEY_VARS_MARKER and is_secret_env(k, env.get(k)))
    return [n for n in names if n and (env.get(n) or "").strip()]


# (2026-10-26, security L2) what never reaches the claude.exe child (runner.child_env): a secret-named
# variable (e.g. GITHUB_TOKEN, OPENAI_API_KEY) unless it is one of claude.exe's own login routes, and the
# switches that turn off or log TLS or inject Node code.
# (2026-10-27, security SEC-M8) one broad name rule for scrub_child_env and key_vars_in: report.py's words
# (KEY, TOKEN, SECRET, PASSWORD, PASSWD, PWD, CREDENTIAL, AUTH) plus PASS, PW, PAT, DSN, WEBHOOK, a word
# ending in APIKEY/PASSWORD/PASSWD/SECRET/TOKEN/PASSPHRASE (bare APIKEY, TOKEN, PASSWORD; PGPASSWORD),
# DATABASE_URL-style URLs and connection strings ((2026-10-27, SEC-M8 review) also SECRETKEY, ACCESSKEY,
# PRIVATEKEY ... compound KEY words and .NET's ConnectionStrings__X, as report.py does). A word is a run
# of letters and digits between `_`, `-`, `.` or the ends, so MAX_TOKENS, TOKENIZER_MODE, GIT_AUTHOR_NAME or GIT_ASKPASS are not secret-named.
# (2026-10-27, SEC-M8 review round 2) a word may also end in digits (OPENAI_API_KEY2, DB_PASSWORD1), be
# plural (GITHUB_TOKENS, API_TOKENS) or be one compound word ending in KEY, TOKEN, AUTH, PWD, DSN or PASS
# (OPENAIKEY, STRIPEKEY, BASICAUTH, DBPASS; not ASKPASS, BYPASS, COMPASS), and SECRET, PASSWORD, PASSWD,
# PASSPHRASE, APIKEY, CREDENTIAL, WEBHOOK, AUTHORIZATION and CONNSTR count anywhere in a word
# (SECRETVALUE). Still not secret-named: KEYBOARD_LAYOUT, TOKENIZER_MODE, GIT_AUTHOR_NAME, GIT_ASKPASS,
# PASSENGER_HOME and a token COUNT (COUNT_TOKENS_RE). report.py's plain substring rule is not used here:
# it would also drop claude.exe's own MAX_THINKING_TOKENS / CLAUDE_CODE_MAX_OUTPUT_TOKENS and the
# GIT_AUTHOR_NAME / GIT_ASKPASS that git in the child reads.
# (2026-10-27, SEC-M8 review round 3) KEY, TOKEN and AUTH count anywhere in a word too, as report.py's
# rule does (TOKENVALUE, AUTHHEADER, PRIVATEKEYB64, SESSIONKEYVALUE), except in KEYBOARD, KEYRING (pip's
# and uv's KEYRING_PROVIDER), TOKENIZER/TOKENISER and AUTHOR/AUTHORITY; plus a COOKIE word
# (SESSION_COOKIE; not COOKIECUTTER), the broker and JDBC URLs (CELERY_BROKER_URL, RABBITMQ_URL,
# JDBC_URL, CACHE_URL) and Azure Functions' AzureWebJobsStorage/-Dashboard connection strings.
# (2026-10-27, SEC-M8 review round 4) and anywhere in a name, with no word bounds (SECRET_NAME_ANYWHERE):
# a connection string (RedisConnectionString, DBCONNECTIONSTRING, ConnectionStrings__X), the
# Bitwarden/1Password CLI sessions, GitLab's CI job JWT, Sonar's login token, a Slack hook URL and the
# run-together GHPAT, SMTPPW and DATABASEURL. vscode-extension/lib/chatview.js copies this list.
SECRET_NAME_ANYWHERE = (r"CONNECTION_?STRING|BW_SESSION|OP_SESSION_|CI_JOB_JWT|SONAR_LOGIN|SLACK_HOOK_URL"
                        r"|GHPAT|SMTPPW|DATABASEURL")
_NAME_WORD_START = r"(?<![A-Za-z0-9])"
_NAME_WORD_END = r"[0-9]*(?![A-Za-z0-9])"
SECRET_ENV_NAME_RE = re.compile(
    r"(?i)" + _NAME_WORD_START + r"(?:PW|PAT|CREDS?"
    r"|[A-Z0-9]*(?:PWD|DSN)S?"
    r"|[A-Z0-9]*(?<!ASK)(?<!BY)(?<!COM)PASS"
    r"|[A-Z0-9]*(?:SECRET|PASSWORD|PASSWD|PASSPHRASE|APIKEY|CREDENTIAL|WEBHOOK|AUTHORI[SZ]ATION|CONNSTR"
    r"|KEY(?!BOARD|RING)|TOKEN(?!I[SZ])|AUTH(?!OR)|COOKIE(?!CUTTER))[A-Z0-9]*"
    r"|AZUREWEBJOBS(?:STORAGE|DASHBOARD)"
    r"|(?:DATABASE|DB|REDIS|MONGO|MONGODB|POSTGRES|POSTGRESQL|MYSQL|AMQPS?|BROKER|RABBITMQ|JDBC|CACHE)_UR[LI])"
    + _NAME_WORD_END + r"|(?:" + SECRET_NAME_ANYWHERE + r")")
# (2026-10-27, SEC-M8 review round 3) a VALUE that carries a credential whatever its name: a URL with a
# password (amqp://user:pass@host, redis://:pass@host, jdbc:mysql://u:p@h) or a field of a connection
# string, URL query or option list that names a password, secret, token, access key or signature
# (AccountKey=, ;Password=, ?password=, a SAS URL's &sig=, -Dx.keyStorePassword=)
# (2026-10-27, SEC-M8 review round 4) a `,` separates fields too (StackExchange.Redis's
# host:6380,password=x,ssl=True)
SECRET_ENV_VALUE_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s@:]*:[^/\s@]+@"
    r"|(?:^|[;?&,\s])(?:sig|[\w.\-]*(?:password|passwd|pwd|passphrase|secret|token|accountkey|accesskey"
    r"|accesssignature|apikey|api[_\-]key))\s*=\s*[^\s;&,]")
# (2026-10-27, SEC-M8 review round 3) the proxy variables claude.exe needs to reach the API at all: the
# value rule leaves them alone (a proxy's user:password@ would else cut the child off the network)
PROXY_ENV_VARS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})
# (2026-10-27, SEC-M8 review round 2) a token COUNT is no secret: claude.exe's own MAX_THINKING_TOKENS,
# CLAUDE_CODE_MAX_OUTPUT_TOKENS, MAX_MCP_OUTPUT_TOKENS ... (plural TOKENS right after a count word only;
# GITHUB_TOKENS and API_TOKENS stay secret-named)
COUNT_TOKENS_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:MAX|MIN|OUTPUT|INPUT|THINKING|CONTEXT|BUDGET|NUM|TOTAL)"
                             r"[_.\-]?TOKENS(?![A-Za-z0-9])")
# (2026-10-27, security SEC-M8) names the rule matches whose value is no secret: the shell's folders, the
# ssh-agent socket path, this app's key-NAME marker, and claude.exe's own switches and mTLS key file path
NOT_SECRET_ENV_NAMES = frozenset({"PWD", "OLDPWD", "SSH_AUTH_SOCK", KEY_VARS_MARKER,
                                  "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                                  "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
                                  "CLAUDE_CODE_CLIENT_KEY",
                                  # (2026-10-27, SEC-M8 review) credential-FILE paths Bedrock and Vertex read
                                  "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
                                  "GOOGLE_APPLICATION_CREDENTIALS"})
# (2026-10-27, integrate S1 x S6) git's command-line config NAMES: GIT_CONFIG_KEY_<n> holds a config key such
# as core.pager, never a secret, but its KEY word matched the rule above, so runner.child_env's own safe git
# config (SEC-L19) lost its keys and every git in the child failed ("missing config key"). Exact names only;
# GIT_CONFIG_VALUE_<n> still goes through the value rule (is_secret_env).
GIT_CONFIG_KEY_NAME_RE = re.compile(r"GIT_CONFIG_KEY_[0-9]+")
CHILD_ENV_DROP = frozenset({"NODE_TLS_REJECT_UNAUTHORIZED", "SSLKEYLOGFILE", "NODE_OPTIONS"})
# (2026-10-27, security SEC-M8) the AWS login variables are a route only while Bedrock is on: kept when a
# bedrock selection named them (KEY_VARS_MARKER) or the env itself turns Bedrock on (the owner's own
# CLAUDE_CODE_USE_BEDROCK on the default provider), else the owner's AWS keys never reach the child
AWS_LOGIN_VARS = frozenset(v for v in SECRET_VARS if v.startswith("AWS_"))
BEDROCK_ON_VAR = "CLAUDE_CODE_USE_BEDROCK"
# how claude.exe logs in from the env: every route a selection sets (SECRET_VARS: API key, gateway token;
# Bedrock see AWS_LOGIN_VARS), the `claude setup-token` OAuth token, the Foundry key and the mTLS key
# passphrase. The claude.ai /login session itself is a file in ~/.claude, not an env variable, so the
# subscription needs none of these.
CHILD_AUTH_VARS = (SECRET_VARS - AWS_LOGIN_VARS) | {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY",
                                                    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"}


def is_secret_env_name(name: str) -> bool:
    """(2026-10-26, security L2) A variable whose value is a secret: a provider key route or a
    secret-shaped name (SECRET_ENV_NAME_RE, but never NOT_SECRET_ENV_NAMES or a COUNT_TOKENS_RE count)."""
    upper = name.upper()
    if upper in NOT_SECRET_ENV_NAMES or GIT_CONFIG_KEY_NAME_RE.fullmatch(upper):
        return False
    return upper in SECRET_VARS or bool(SECRET_ENV_NAME_RE.search(COUNT_TOKENS_RE.sub("_", name)))


def is_secret_env(name: str, value: str | None) -> bool:
    """(2026-10-27, SEC-M8 review round 3) A secret-named variable (is_secret_env_name), or one whose
    value carries a credential (SECRET_ENV_VALUE_RE), but never a NOT_SECRET_ENV_NAMES or proxy one."""
    if is_secret_env_name(name):
        return True
    upper = name.upper()
    return (upper not in NOT_SECRET_ENV_NAMES and upper not in PROXY_ENV_VARS
            and bool(SECRET_ENV_VALUE_RE.search(value or "")))


def _bedrock_on(env: MutableMapping[str, str]) -> bool:
    """(2026-10-27, security SEC-M8) claude.exe's own truthy test of CLAUDE_CODE_USE_BEDROCK."""
    return (env.get(BEDROCK_ON_VAR) or "").strip().lower() in ("1", "true", "yes", "on")


def scrub_child_env(env: MutableMapping[str, str]) -> list[str]:
    """(2026-10-26, security L2) Remove from `env`, in place, every variable the claude.exe child must not
    get (secret names or credential values, is_secret_env, but CHILD_AUTH_VARS, the selection's own keys and, while
    Bedrock is on, AWS_LOGIN_VARS; CHILD_ENV_DROP). Returns the removed NAMES, never values."""
    keep = CHILD_AUTH_VARS | {n.strip().upper() for n in (env.get(KEY_VARS_MARKER) or "").split(",") if n.strip()}
    if _bedrock_on(env):
        keep = keep | AWS_LOGIN_VARS
    gone = sorted(k for k in env if k.upper() in CHILD_ENV_DROP
                  or (is_secret_env(k, env[k]) and k.upper() not in keep))
    for k in gone:
        del env[k]
    return gone


def describe(values: dict[str, str] | None = None) -> list[dict]:
    """One row per provider for `autopilot providers [--json]`. No key value is ever included."""
    values = values or {}
    rows: list[dict] = []
    for p in PROVIDERS:
        var, have = key_status(p, values)
        missing = missing_for(p, values)
        base_url = (values.get(p.base_url_var) or p.base_url).strip()
        # (2026-10-26, network audit MEDIUM) a base URL resolve() would refuse: not ready, and why
        refused = base_url_problem(base_url) if base_url else None
        rows.append({
            "name": p.name,
            "label": p.label,
            "key_var": var,
            "key_set": have,
            "base_url": (_shown_url(base_url) if refused else redact_url(base_url)) or None,
            "model": (values.get(p.model_var) or p.default_model).strip() or None,
            "models": list(p.models),
            "supports_effort": p.supports_effort,
            "ready": not missing and not refused,
            "missing": missing,
            "refused": f"{p.base_url_var}: {refused}" if refused else None,
            "note": p.note,
        })
    return rows
