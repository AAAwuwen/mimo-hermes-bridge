#!/usr/bin/env python3
"""mimo-hermes-bridge -- make MiMoCode (an OpenCode fork) and Hermes Agent talk.

Single file, stdlib only, Python 3.10+.

No absolute paths and no credentials live in this file.  Everything is resolved at
runtime from CLI flags, then environment variables, then platform defaults:

    MIMO_CONFIG        path to mimocode.jsonc
    MIMO_BIN           path to the `mimo` executable
    HERMES_BIN         path to the `hermes` executable
    HERMES_HOME        Hermes data directory
    MIMO_BRIDGE_MODEL  default provider/model for `ask --to mimo`
    MIMO_BRIDGE_TIMEOUT  default subprocess timeout in seconds

Subcommands: setup / check / ask / doctor / render
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

VERSION = "1.0.0"

PROJECT_ROOT = Path(__file__).resolve().parent
FRAGMENT_PATH = PROJECT_ROOT / "examples" / "mimocode.jsonc.fragment"
DEFAULT_TIMEOUT = 180

MCP_SERVER_NAME = "hermes"

# Exit-code contract, documented in README.
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2
EXIT_EMPTY_REPLY = 3

# run_capture() sentinels.  These are *not* process exit codes: the command
# never ran, or it ran and had to be killed.
RC_COULD_NOT_START = -1
RC_TIMED_OUT = -2
RC_LAUNCH_FAILURES = (RC_COULD_NOT_START, RC_TIMED_OUT)

# Cosmetic caps for table output.
TOOLS_PREVIEW = 4
DETAIL_PREVIEW = 120
LIVE_PREVIEW = 80

# JSON-RPC ids we assign, so the two handshake replies stay distinguishable.
MCP_INIT_REQUEST_ID = 1
MCP_TOOLS_REQUEST_ID = 2

# One row of the `check` table: (check name, status, detail).
Row = tuple[str, str, str]

# Backup filename tags.  `setup` writes SETUP_BACKUP_TAG; `--restore` writes its
# own safety copy under RESTORE_BACKUP_TAG, so `--restore` cannot pick up that
# copy as "the newest backup" and thereby undo the restore it just performed.
SETUP_BACKUP_TAG = "bak"
RESTORE_BACKUP_TAG = "prerestore"

# --------------------------------------------------------------------------
# JSONC failures -- one class per failure mode, so the wording callers print
# lives with the exception instead of being repeated at every raise site
# --------------------------------------------------------------------------


class JsoncError(ValueError):
    """A JSONC document we cannot read or safely edit.

    `message` is the text callers show via `str(exc)`; subclasses override it.
    """

    message = "invalid JSONC text"

    def __init__(self) -> None:
        super().__init__(self.message)


class UnterminatedStringError(JsoncError):
    """A quoted string that never closes."""

    message = "unterminated string"


class UnexpectedEndOfInputError(JsoncError):
    """The document stops in the middle of a value."""

    message = "unexpected end of input"


class UnbalancedBracesError(JsoncError):
    """A container that opens and never closes."""

    message = "unbalanced braces"


class NoRootObjectError(JsoncError):
    """The document holds no root object at all."""

    message = "no root object found"


class RootNotAnObjectError(JsoncError, TypeError):
    """The root parses, but is not a JSON object.

    TypeError is the honest category for a wrong type; JsoncError stays a base so
    callers that treat every unreadable config as a ValueError keep working.
    """

    message = "config root is not a JSON object"


# --------------------------------------------------------------------------
# tiny JSONC reader  (JSON with // line comments, /* block */ comments and
# trailing commas tolerated)  -- MiMoCode configs are .jsonc
# --------------------------------------------------------------------------


def strip_jsonc(text: str) -> str:
    """Blank out comments and trailing commas, preserving byte offsets/shape.

    Comments are replaced with spaces (newlines kept) so that error messages
    and line numbers stay meaningful.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        out.append(ch)
        i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text: str) -> str:
    """Blank out a comma that is immediately followed by a closing bracket."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                out.append(" ")
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_jsonc_text(text: str) -> Any:
    """Parse JSONC text (comments and trailing commas tolerated)."""
    return json.loads(strip_jsonc(text))


def load_jsonc(path: Path) -> Any:
    """Parse a JSONC file, read as UTF-8."""
    return load_jsonc_text(path.read_text(encoding="utf-8"))


def _member(container: Any, name: str) -> Any:
    """`container[name]` when `container` is a JSON object, else None.

    Every level of a config is optional, so each lookup has to survive a parent
    that is absent or not an object.
    """
    return container.get(name) if isinstance(container, dict) else None


# --------------------------------------------------------------------------
# structural text editing: find a key at depth 1 of the root object so we can
# insert our entry without destroying the user's comments or formatting.
# --------------------------------------------------------------------------


def _skip_string(text: str, i: int) -> int:
    """Return the index just past the closing quote of the string opening at `i`."""
    i += 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    raise UnterminatedStringError


def _skip_value(text: str, i: int) -> int:
    """Return the index just past the value starting at `i` (scalar or container)."""
    n = len(text)
    if i >= n:
        raise UnexpectedEndOfInputError
    ch = text[i]
    if ch == '"':
        return _skip_string(text, i)
    if ch in "[{":
        depth = 0
        while i < n:
            c = text[i]
            if c == '"':
                i = _skip_string(text, i)
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise UnbalancedBracesError
    while i < n and text[i] not in ",}] \t\r\n":
        i += 1
    return i


def iter_root_keys(text: str):
    """Yield one tuple per key of the root object.

    Each tuple is (key_name, key_start, value_start, value_end).
    """
    clean = strip_jsonc(text)
    i = clean.find("{")
    if i < 0:
        return
    i += 1
    n = len(clean)
    while i < n:
        while i < n and clean[i] in " \t\r\n,":
            i += 1
        if i >= n or clean[i] == "}":
            return
        if clean[i] != '"':
            return
        key_start = i
        key_end = _skip_string(clean, i)
        key = json.loads(clean[i:key_end])
        j = key_end
        while j < n and clean[j] in " \t\r\n":
            j += 1
        if j >= n or clean[j] != ":":
            return
        j += 1
        while j < n and clean[j] in " \t\r\n":
            j += 1
        val_end = _skip_value(clean, j)
        yield key, key_start, j, val_end
        i = val_end


def _indent_of(text: str, index: int) -> str:
    """The whitespace before `index` on its own line, or "" if it is not alone."""
    line_start = text.rfind("\n", 0, index) + 1
    prefix = text[line_start:index]
    return prefix if prefix.strip() == "" else ""


def indent_unit(indent: str) -> str:
    """One nesting step, matching the document's own style (tabs vs spaces)."""
    return "\t" if indent and set(indent) == {"\t"} else "  "


def detect_child_indent(text: str) -> str:
    """Indentation of root-level keys (i.e. how deep a container's children sit)."""
    for _, key_start, _, _ in iter_root_keys(text):
        ind = _indent_of(text, key_start)
        if ind:
            return ind
    return "  "


def render_entry_text(entry: dict, name: str, indent: str, unit: str = "  ") -> str:
    """Serialise one key/value pair as indented JSON text, in the document's style."""
    blob = json.dumps({name: entry}, indent=2, ensure_ascii=False)
    rendered = []
    for line in blob.split("\n")[1:-1]:
        stripped = line.lstrip(" ")
        level = (len(line) - len(stripped)) // 2
        rendered.append(indent + unit * level + stripped)
    return "\n".join(rendered)


def merge_named_entry(
    text: str,
    container: str,
    name: str,
    entry: Any,
) -> tuple[str, bool, str]:
    """Idempotently ensure `container.name == entry` inside a JSONC document.

    Returns (new_text, changed, how).  `how` is one of:
      unchanged | insert-empty | insert-into | append-container | rewrite
    The original text is preserved (comments and all) whenever that is safe;
    a full re-serialise is only used when an existing value must be replaced.
    """
    data = load_jsonc_text(text)
    if not isinstance(data, dict):
        raise RootNotAnObjectError

    existing_container = data.get(container)
    existing = _member(existing_container, name)
    if existing is not None and existing == entry:
        return text, False, "unchanged"
    if existing is not None:
        data[container][name] = entry
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n", True, "rewrite"

    # `indent` is the indentation of root-level keys, so our entry -- nested one
    # level deeper, inside the container -- sits at indent + unit.
    indent = detect_child_indent(text)
    unit = indent_unit(indent)
    deep = indent + unit
    container_span = next(
        ((s, e) for k, _ks, s, e in iter_root_keys(text) if k == container),
        None,
    )

    if container_span and text[container_span[0]] == "{":
        # locate the matching close brace of the container object
        container_end = _skip_value(text, container_span[0])
        inner_start, inner_end = container_span[0] + 1, container_end - 1
        body = text[inner_start:inner_end]
        entry_text = render_entry_text(entry, name, deep, unit)
        if body.strip() == "":
            new_body = f"\n{entry_text}\n{indent}"
            merged = text[:inner_start] + new_body + text[inner_end:]
            return merged, True, "insert-empty"
        kept = body.rstrip("\n")
        tail_ws = body[len(kept) :]
        new_body = f"\n{entry_text},{kept}\n{indent}{tail_ws}"
        return text[:inner_start] + new_body + text[inner_end:], True, "insert-into"

    # no container key yet -> add it as a new root-level key
    root_end = text.rfind("}")
    if root_end < 0:
        raise NoRootObjectError
    body = text[:root_end].rstrip()
    has_keys = any(True for _ in iter_root_keys(text))
    sep = "," if has_keys and not body.endswith(",") else ""
    inserted = (
        f'{sep}\n{indent}"{container}": {{\n'
        f"{render_entry_text(entry, name, deep, unit)}\n"
        f"{indent}}}\n"
    )
    return body + inserted + "}" + text[root_end + 1 :], True, "append-container"


# --------------------------------------------------------------------------
# placeholders in example fragments -- never bake a real path into a template
# --------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class PlaceholderError(Exception):
    """A `{{KEY}}` placeholder with no value to substitute."""


def substitute_placeholders(value: Any, mapping: dict) -> Any:
    """Recursively replace {{KEY}} placeholders.

    A string that is *exactly* one placeholder takes the mapping value as-is
    (so a list or bool can be injected); otherwise placeholders are inlined as
    text.  Unknown placeholders raise PlaceholderError so a half-rendered
    config can never be written by accident.
    """
    missing: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            whole = PLACEHOLDER_RE.fullmatch(node)
            if whole:
                key = whole.group(1)
                if key not in mapping:
                    missing.append(key)
                    return node
                return mapping[key]

            def repl(m: re.Match[str]) -> str:
                key = m.group(1)
                if key not in mapping:
                    missing.append(key)
                    return m.group(0)
                return str(mapping[key])

            return PLACEHOLDER_RE.sub(repl, node)
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    result = walk(value)
    if missing:
        raise PlaceholderError(
            "unresolved placeholder(s): "
            + ", ".join(sorted(set(missing)))
            + " -- known keys: "
            + ", ".join(sorted(mapping))
        )
    return result


def render_fragment(path: Path, mapping: dict) -> Any:
    """Parse a JSONC fragment and substitute its placeholders."""
    return substitute_placeholders(load_jsonc(path), mapping)


# --------------------------------------------------------------------------
# entry equivalence -- a config that already routes to a *working* launcher is
# left alone even if it names a different (also valid) one than we would pick
# --------------------------------------------------------------------------


def normalize_entry(entry: Any) -> dict | None:
    """The comparison-relevant shape of an MCP entry, or None if it is not one."""
    if not isinstance(entry, dict):
        return None
    cmd = entry.get("command")
    if not isinstance(cmd, list) or not cmd:
        return None
    return {
        "type": entry.get("type"),
        "command": [str(c) for c in cmd],
        "enabled": entry.get("enabled", True),
    }


def entry_is_satisfied(existing: Any, desired: Any) -> bool:
    """True when `existing` already routes to the same working launcher as `desired`."""
    got = normalize_entry(existing)
    want = normalize_entry(desired)
    if got is None or want is None:
        return False
    if got["type"] != want["type"] or not got["enabled"]:
        return False
    if got["command"][1:] != want["command"][1:]:
        return False
    return Path(got["command"][0]).exists()


# --------------------------------------------------------------------------
# environment discovery -- flags > env vars > platform defaults
# --------------------------------------------------------------------------


def env_value(name: str) -> str | None:
    """The environment variable, treating an empty string as unset."""
    val = os.environ.get(name)
    return val or None


def home_dir() -> Path:
    """The user's home directory, with no Windows-specific guessing."""
    return Path(os.path.expanduser("~"))


def local_appdata() -> Path | None:
    """%LOCALAPPDATA%, or None on platforms that have no such thing."""
    val = env_value("LOCALAPPDATA")
    return Path(val) if val else None


def which_any(names: list[str]) -> Path | None:
    """The first of `names` found on PATH, so both `hermes` and `hermes.exe` work."""
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _same_file(a: Path, b: Path) -> bool:
    """True when both paths are the same file (case-insensitive on Windows)."""
    try:
        if a.exists() and b.exists() and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return a.resolve() == b.resolve()


def to_native_path(value: str) -> Path:
    """Resolve a user-supplied path string, translating MSYS paths on Windows.

    Git-Bash hands out POSIX-looking paths (/tmp/x, /c/Users/x) but a native
    Windows Python reads /tmp/x as D:\\tmp\\x -- a *different* file, silently.
    That is a nasty footgun for anyone scripting this tool from Git-Bash, so we
    ask cygpath when the path as given does not exist.  Only invoked for
    absolute POSIX-looking paths, and only when MSYS is actually in play, so a
    genuine POSIX path is never rewritten.
    """
    path = Path(value).expanduser()
    if path.exists() or not value.startswith("/"):
        return path
    if sys.platform != "win32" or not os.environ.get("MSYSTEM"):
        return path
    cygpath = shutil.which("cygpath")
    if not cygpath:
        return path
    rc, out, _ = run_capture([cygpath, "-w", value], timeout=15)
    translated = out.strip()
    if rc == 0 and translated:
        return Path(translated)
    return path


def _explicit_or_env(explicit: str | None, env_name: str) -> Path | None:
    """Flag first, then the environment variable -- both as native paths.

    Every path option resolves this way, so the precedence lives here once rather
    than being re-implemented per option.
    """
    for candidate in (explicit, env_value(env_name)):
        if candidate:
            return to_native_path(candidate)
    return None


def _local_roots() -> list[Path]:
    """Directories an installer may have used: LOCALAPPDATA, then HOME's AppData."""
    return [b for b in (local_appdata(), home_dir() / "AppData" / "Local") if b]


def resolve_mimo_config(explicit: str | None) -> Path:
    """The MiMoCode config: flag, MIMO_CONFIG, then the home-directory default."""
    pinned = _explicit_or_env(explicit, "MIMO_CONFIG")
    return pinned or home_dir() / ".config" / "mimocode" / "mimocode.jsonc"


def resolve_hermes_home(explicit: str | None) -> Path:
    """The Hermes data directory: flag, HERMES_HOME, then LOCALAPPDATA/Hermes."""
    pinned = _explicit_or_env(explicit, "HERMES_HOME")
    return pinned or (local_appdata() or home_dir()) / "Hermes"


def resolve_hermes_bin(explicit: str | None) -> Path | None:
    """Locate the `hermes` executable, or None when this box has none we can see."""
    pinned = _explicit_or_env(explicit, "HERMES_BIN")
    if pinned:
        return pinned
    found = which_any(["hermes", "hermes.exe"])
    if found:
        return found
    for base in _local_roots():
        candidate = base / "hermes" / "bin" / "hermes.exe"
        if candidate.exists():
            return candidate
    return None


def resolve_mimo_bin(explicit: str | None) -> Path | None:
    """Locate the `mimo` executable, or None when this box has none we can see."""
    pinned = _explicit_or_env(explicit, "MIMO_BIN")
    if pinned:
        return pinned
    found = which_any(["mimo", "mimo.exe", "mimo.cmd", "mimo.bat"])
    if found:
        return found
    # Known trap: npm installs `mimo` into the *Hermes* node dir, which is not
    # on MiMoCode's own PATH.  Only the directory roots are hardcoded-neutral
    # (they come from LOCALAPPDATA / HOME); the relative layout is the guess.
    for base in _local_roots():
        for rel in ("hermes/node/mimo", "hermes/node/mimo.cmd", "npm/mimo.cmd"):
            candidate = base / rel
            if candidate.exists():
                return candidate
    return None


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])",
)


def strip_ansi(text: str) -> str:
    """Drop ANSI escapes and normalise line endings, so agent output stays readable."""
    return ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")


def timestamp() -> str:
    """A local-time stamp for backup filenames, sortable as a string."""
    return time.strftime("%Y%m%d_%H%M%S")


def backup_file(path: Path, tag: str = SETUP_BACKUP_TAG) -> Path:
    """Copy `path` next to itself and return the copy.

    `tag` keeps the two kinds of copy apart: `setup` writes SETUP_BACKUP_TAG,
    while a restore writes RESTORE_BACKUP_TAG.  Without that split, `--restore`
    would later pick the restore's own safety copy as "the newest backup" and
    undo itself -- a toggle instead of a roll-back.
    """
    stamp = timestamp()
    dest = path.with_name(f"{path.name}.{tag}.{stamp}")
    counter = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.{tag}.{stamp}_{counter}")
        counter += 1
    shutil.copy2(path, dest)
    return dest


def run_capture(
    cmd: list[str],
    timeout: int = 60,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a command to completion and return (returncode, stdout, stderr).

    A returncode of RC_COULD_NOT_START / RC_TIMED_OUT means the command never
    really ran -- callers must not read those as exit codes from the tool.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,  # the caller inspects the return code itself
        )
    except FileNotFoundError:
        return RC_COULD_NOT_START, "", f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return RC_TIMED_OUT, "", f"timed out after {timeout}s: {' '.join(cmd)}"
    except OSError as exc:
        return RC_COULD_NOT_START, "", f"could not start {cmd[0]}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def first_line(text: str) -> str:
    """The first non-blank line of an agent's output, ANSI escapes removed."""
    for line in strip_ansi(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------
# MCP handshake: the only honest way to prove the Hermes channel is live
# --------------------------------------------------------------------------


def mcp_probe(command: list[str], timeout: int = 30) -> dict:
    """Speak just enough MCP/stdio to list the server's tools."""
    result = _empty_probe_result()
    proc = _start_probe(command, result)
    if proc is None:
        return result

    lines: list[str] = []
    done = threading.Event()
    reader = threading.Thread(
        args=(proc.stdout, lines, done),
        target=_collect_replies,
        daemon=True,
    )
    reader.start()

    if not _send_handshake(proc, result):
        _kill(proc)
        return result

    done.wait(timeout)
    _kill(proc)

    if not lines:
        result["error"] = "MCP server produced no output (is `hermes mcp serve` working?)"
        return result
    _read_replies(lines, result)
    _explain_silence(result)
    return result


def _empty_probe_result() -> dict:
    """The report every caller sees, whether or not the server ever answered."""
    return {"ok": False, "tools": [], "protocol": None, "server": None, "error": None}


def _start_probe(command: list[str], result: dict) -> subprocess.Popen[str] | None:
    """Start the MCP server, recording why it would not start instead of raising."""
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except (FileNotFoundError, OSError) as exc:
        result["error"] = f"could not start MCP server: {exc}"
        return None


def _collect_replies(stream: IO[str], lines: list[str], done: threading.Event) -> None:
    """Collect replies until `tools/list` answers, then stop reading.

    Reads on its own thread: a server that never answers must not hang the probe,
    so the reader is abandoned via `done` instead of being joined.
    """
    try:
        for line in stream:
            lines.append(line)
            if '"id":2' in line.replace(" ", ""):
                break
    except (ValueError, OSError):
        pass
    finally:
        done.set()


def _send_handshake(proc: subprocess.Popen[str], result: dict) -> bool:
    """Write the three opening requests; False means the server hung up on us."""
    try:
        for request in _handshake_requests():
            proc.stdin.write(request + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        result["error"] = f"MCP server closed stdin: {exc}"
        return False
    return True


def _handshake_requests() -> list[str]:
    """initialize, initialized notification, tools/list -- as newline-delimited JSON."""
    return [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": MCP_INIT_REQUEST_ID,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mimo-hermes-bridge", "version": VERSION},
                },
            }
        ),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": MCP_TOOLS_REQUEST_ID,
                "method": "tools/list",
                "params": {},
            }
        ),
    ]


def _read_replies(lines: list[str], result: dict) -> None:
    """Fold every JSON-RPC reply we captured into `result`."""
    for raw in lines:
        msg = _parse_reply(raw)
        if msg is None:
            continue
        if msg.get("id") == MCP_INIT_REQUEST_ID and "result" in msg:
            result["protocol"] = msg["result"].get("protocolVersion")
            info = msg["result"].get("serverInfo") or {}
            result["server"] = info.get("name")
        elif msg.get("id") == MCP_TOOLS_REQUEST_ID:
            if "error" in msg:
                result["error"] = f"tools/list failed: {msg['error']}"
                return
            _record_tools(msg, result)


def _parse_reply(raw: str) -> dict | None:
    """The JSON object on one stdout line, or None when the line is not one."""
    line = raw.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _record_tools(msg: dict, result: dict) -> None:
    """A `tools/list` result: sorted tool names, and the probe counts as passed."""
    tools = (msg.get("result") or {}).get("tools") or []
    result["tools"] = sorted(t.get("name", "?") for t in tools)
    result["ok"] = True


def _explain_silence(result: dict) -> None:
    """For a probe that answered but never finished: name the missing half."""
    if result["ok"] or result["error"] is not None:
        return
    if result["protocol"] is None:
        result["error"] = "no MCP `initialize` response"
    else:
        result["error"] = "no MCP `tools/list` response"


def _close_quietly(stream: IO[str] | None) -> None:
    """Close a pipe, tolerating what a process that already exited can raise."""
    if stream is None:
        return
    with contextlib.suppress(OSError):
        stream.close()


def _kill(proc: subprocess.Popen[str]) -> None:
    """Tear a probe down without letting the teardown itself raise."""
    with contextlib.suppress(OSError):
        if proc.poll() is None:
            proc.kill()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        _close_quietly(stream)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)


# --------------------------------------------------------------------------
# model auto-detection for the MiMo channel
# --------------------------------------------------------------------------


def detect_model(config_path: Path, config: dict) -> tuple[str | None, str]:
    """Derive 'provider/model' from the MiMoCode config. Returns (model, note)."""
    providers = config.get("provider")
    if not isinstance(providers, dict) or not providers:
        return None, "no `provider` block in config"
    for pname, pdata in providers.items():
        models = (pdata or {}).get("models")
        if isinstance(models, dict) and models:
            mname = next(iter(models))
            return f"{pname}/{mname}", f"auto-detected from {config_path.name}"
    return None, "`provider` block defines no models"


def missing_credential_hint(config: dict) -> str | None:
    """Why the MiMo channel would fail to authenticate, or None when it looks fine."""
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return None
    for pname, pdata in providers.items():
        options = (pdata or {}).get("options") or {}
        if "apiKey" in options:
            key = options.get("apiKey")
            if isinstance(key, str) and key.startswith("{env:"):
                var = key[len("{env:") :].rstrip("}")
                if not os.environ.get(var):
                    return f"provider '{pname}' needs env var {var} (not set)"
            return None
    return None


def _provider_options(config: dict) -> list[dict]:
    """The `options` block of every provider, with the missing ones smoothed to {}."""
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return []
    return [(pdata or {}).get("options") or {} for pdata in providers.values()]


def any_provider_declares_api_key(config: dict) -> bool:
    """True when any provider block declares an apiKey (its value is never read)."""
    return any("apiKey" in options for options in _provider_options(config))


# --------------------------------------------------------------------------
# table rendering
# --------------------------------------------------------------------------

STATUS_ORDER = {"FAIL": 0, "WARN": 1, "PASS": 2, "INFO": 3}


def render_table(rows: list[Row]) -> str:
    """The check table as text: one row per line, columns padded to the widest."""
    if not rows:
        return ""
    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(r[1]) for r in rows)
    lines = []
    for check, status, detail in rows:
        lines.append(f"  {check.ljust(w1)}  {status.ljust(w2)}  {detail}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def find_latest_backup(config_path: Path) -> Path | None:
    """The newest `setup` backup next to the config, or None.

    Only SETUP_BACKUP_TAG files count -- a restore's own safety copy must never
    be a roll-back candidate, or `--restore` would flip the config back and forth.
    """
    candidates = sorted(
        config_path.parent.glob(f"{config_path.name}.{SETUP_BACKUP_TAG}.*"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    return candidates[-1] if candidates else None


def cmd_restore(args, config_path: Path) -> int:
    """Roll the config back to a backup -- the inverse of setup's backup step.

    Restoring is itself a config change, so the current file is backed up first;
    that keeps the roll-back reversible instead of merely moving the risk.
    """
    requested = args.restore or None
    source = to_native_path(requested) if requested else find_latest_backup(config_path)
    if source is None:
        print(f"[restore] FAIL  no backup of {config_path.name} found")
        expected = f"{config_path.name}.bak.<timestamp> in {config_path.parent}"
        print(f"[restore]       expected {expected}")
        return EXIT_CHECK_FAILED

    wanted, refused = _read_backup(source, config_path)
    if refused is not None:
        return refused

    if config_path.read_text(encoding="utf-8") == wanted:
        same = f"{config_path.name} already matches {source.name}"
        print(f"[restore] {same} -- no change (idempotent).")
        return EXIT_OK
    if args.dry_run:
        print(f"[restore] --dry-run: would copy {source} over {config_path}")
        return EXIT_OK

    safety = backup_file(config_path, tag=RESTORE_BACKUP_TAG)
    print(f"[restore] current config saved to : {safety}")
    config_path.write_text(wanted, encoding="utf-8")
    print(f"[restore] restored {config_path} from {source.name}")
    print("[restore] restart MiMoCode / open a new session for changes to load.")
    return EXIT_OK


def _read_backup(source: Path, config_path: Path) -> tuple[str, int | None]:
    """Read the backup we intend to restore, refusing anything unusable.

    Returns (text, None) when the text is safe to write; otherwise ("", exit
    code) after printing why -- a corrupt or unreadable backup must never be
    copied over a config that currently works.
    """
    if not source.is_file():
        print(f"[restore] FAIL  backup not found: {source}")
        return "", EXIT_CHECK_FAILED
    if not config_path.exists():
        print(f"[restore] FAIL  no config to restore over: {config_path}")
        return "", EXIT_CHECK_FAILED

    wanted = source.read_text(encoding="utf-8")
    try:
        usable = isinstance(load_jsonc_text(wanted), dict)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[restore] FAIL  {source} is not valid JSONC: {exc}")
        print("[restore]       refusing to overwrite a working config with it.")
        return "", EXIT_CHECK_FAILED
    if not usable:
        print(f"[restore] FAIL  {source} does not contain a JSON object")
        return "", EXIT_CHECK_FAILED
    return wanted, None


def cmd_setup(args) -> int:
    """Merge the hermes MCP entry into the config (idempotent, backs up first)."""
    config_path = resolve_mimo_config(args.config)
    print(f"[setup] MiMoCode config : {config_path}")

    if args.restore is not None:
        return cmd_restore(args, config_path)

    if args.dry_run:
        print("[setup] --dry-run: no file will be written")

    missing = _ensure_config_exists(config_path, args.dry_run)
    if missing is not None:
        return missing

    hermes_bin = _require_hermes_bin(args.hermes_bin)
    if hermes_bin is None:
        return EXIT_CHECK_FAILED

    entry = {
        "type": "local",
        "command": [str(hermes_bin), "mcp", "serve"],
        "enabled": True,
    }
    _warn_if_fragment_disagrees(hermes_bin, entry)

    merged = _parse_and_merge(config_path, entry)
    if merged is None:
        return EXIT_CHECK_FAILED
    current, new_text, changed, how = merged

    skipped = _setup_noop_exit(args, current, entry, changed, how)
    if skipped is not None:
        return skipped

    _apply_merge(config_path, new_text, how)
    return EXIT_OK


def _ensure_config_exists(config_path: Path, dry_run: bool) -> int | None:
    """Create an empty config when there is none; None means 'carry on'."""
    if config_path.exists():
        return None
    if dry_run:
        print(f"[setup] FAIL  config not found: {config_path}")
        print("[setup]       create it first, or pass --config <path>")
        return EXIT_CHECK_FAILED
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}\n", encoding="utf-8")
    print(f"[setup] created empty config: {config_path}")
    return None


def _require_hermes_bin(explicit: str | None) -> Path | None:
    """The `hermes` launcher, or None after explaining what would go wrong.

    Writing a path that does not exist is not an error the user would see until
    MiMoCode tries to spawn the server, so the check happens here instead.
    """
    hermes_bin = resolve_hermes_bin(explicit)
    if hermes_bin is not None and hermes_bin.exists():
        print(f"[setup] hermes binary   : {hermes_bin}")
        return hermes_bin
    print(
        "[setup] FAIL  cannot locate the `hermes` executable"
        + (f": {hermes_bin} does not exist" if hermes_bin else ".")
    )
    print("[setup]       set HERMES_BIN=/full/path/to/hermes.exe (or use --hermes-bin);")
    print("[setup]       writing a path that does not exist would only fail later, at spawn time.")
    return None


def _warn_if_fragment_disagrees(hermes_bin: Path, entry: dict) -> None:
    """Keep the shipped fragment honest: it must render to exactly this entry.

    A fragment that renders to something else would silently diverge from what
    `setup` writes, so the copy-paste path and the command path must agree.
    """
    if not FRAGMENT_PATH.exists():
        return
    try:
        rendered = render_fragment(FRAGMENT_PATH, {"HERMES_BIN": str(hermes_bin)})
        frag_entry = (rendered.get("mcp") or {}).get(MCP_SERVER_NAME)
        if frag_entry and frag_entry != entry:
            print(f"[setup] WARN  {FRAGMENT_PATH.name} renders to a different entry:")
            print(f"[setup]       {json.dumps(frag_entry, ensure_ascii=False)}")
    except (PlaceholderError, json.JSONDecodeError, ValueError, KeyError) as exc:
        print(f"[setup] WARN  fragment not usable: {exc}")


def _parse_and_merge(
    config_path: Path,
    entry: dict,
) -> tuple[Any, str, bool, str] | None:
    """Parse the config and merge our entry into it, or None after reporting why."""
    original = config_path.read_text(encoding="utf-8")
    try:
        current = load_jsonc_text(original)
        new_text, changed, how = merge_named_entry(
            original,
            "mcp",
            MCP_SERVER_NAME,
            entry,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[setup] FAIL  cannot parse {config_path}: {exc}")
        return None
    return current, new_text, changed, how


def _setup_noop_exit(
    args,
    current: Any,
    entry: dict,
    changed: bool,
    how: str,
) -> int | None:
    """The exit code for a setup that has nothing left to write, else None."""
    target = f"mcp.{MCP_SERVER_NAME}"
    mcp = _member(current, "mcp")
    existing = _member(mcp, MCP_SERVER_NAME)
    if changed and entry_is_satisfied(existing, entry):
        # Already routed to a working launcher -- possibly a different but
        # equally valid one.  Rewriting it would churn the config for no gain.
        print(f"[setup] {target} already routes to a working launcher:")
        print(f"[setup]       {existing['command'][0]}")
        print("[setup] no change (idempotent). Use --hermes-bin to pin a different one.")
        return EXIT_OK
    if not changed:
        print(f"[setup] {target} already matches -- no change (idempotent).")
        return EXIT_OK
    if args.dry_run:
        print(f"[setup] --dry-run: would apply '{how}' to {target}")
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return EXIT_OK
    return None


def _apply_merge(config_path: Path, new_text: str, how: str) -> None:
    """Back the config up, then write the merged text over it."""
    if how == "rewrite":
        print("[setup] NOTE  an existing mcp.hermes value had to be replaced;")
        print("[setup]       the config is re-serialised, so comments in it are lost.")
    backup = backup_file(config_path)
    print(f"[setup] backup          : {backup}")
    config_path.write_text(new_text, encoding="utf-8")
    print(f"[setup] applied '{how}' -> mcp.{MCP_SERVER_NAME} in {config_path}")
    print("[setup] restart MiMoCode / open a new session for changes to load.")


def _check_rows(args) -> tuple[list[Row], bool]:
    """Every row of the `check` table, plus the overall pass/fail verdict."""
    config_path = resolve_mimo_config(args.config)
    hermes_bin = resolve_hermes_bin(args.hermes_bin)
    mimo_bin = resolve_mimo_bin(args.mimo_bin)

    version = sys.version.split()[0]
    rows: list[Row] = [("python", "INFO", f"{version} on {sys.platform}")]
    rows += _rows_for_binaries(hermes_bin, mimo_bin)
    config_rows, config = _rows_for_config(config_path)
    rows += config_rows
    if config is not None:
        rows += _rows_for_mcp(args, config, hermes_bin)
        rows += _rows_for_model(args, config_path, config)
    if args.live:
        rows += _live_rows(args, hermes_bin, mimo_bin, config_path, config)
    # A FAIL anywhere in the table is the verdict; the sort in cmd_check is cosmetic.
    return rows, all(status != "FAIL" for _, status, _ in rows)


def _rows_for_binaries(hermes_bin: Path | None, mimo_bin: Path | None) -> list[Row]:
    """One row per launcher; each FAIL names the variable that would fix it."""
    rows: list[Row] = []
    if hermes_bin and hermes_bin.exists():
        rows.append(("hermes bin", "PASS", str(hermes_bin)))
    else:
        rows.append(
            (
                "hermes bin",
                "FAIL",
                "not found -- set HERMES_BIN=/full/path/to/hermes.exe (or pass --hermes-bin)",
            )
        )
    if mimo_bin and mimo_bin.exists():
        rows.append(("mimo bin", "PASS", str(mimo_bin)))
    else:
        rows.append(
            (
                "mimo bin",
                "FAIL",
                "not found -- set MIMO_BIN=/full/path/to/mimo (or pass --mimo-bin)",
            )
        )
    return rows


def _rows_for_config(config_path: Path) -> tuple[list[Row], Any]:
    """Rows about the config file itself, plus the parsed config when it loads."""
    if not config_path.exists():
        return [
            (
                "mimocode.jsonc",
                "FAIL",
                f"missing: {config_path} -- run `python bridge.py setup` (or set MIMO_CONFIG)",
            )
        ], None
    rows: list[Row] = [("mimocode.jsonc", "PASS", str(config_path))]
    try:
        config = load_jsonc(config_path)
    except (json.JSONDecodeError, ValueError) as exc:
        rows.append(("jsonc parse", "FAIL", f"{config_path} is not valid JSONC: {exc}"))
        return rows, None
    return rows, config


def _rows_for_mcp(args, config: dict, hermes_bin: Path | None) -> list[Row]:
    """Rows about the mcp.hermes entry, including a live handshake when we can."""
    mcp = config.get("mcp")
    entry = _member(mcp, MCP_SERVER_NAME)
    if not isinstance(entry, dict):
        return [
            (
                "mcp.hermes",
                "FAIL",
                f"no mcp.{MCP_SERVER_NAME} entry -- run `python bridge.py setup`",
            )
        ]
    cmd = entry.get("command")
    if entry.get("enabled") is False:
        return [("mcp.hermes", "FAIL", "entry exists but `enabled` is false")]
    if not isinstance(cmd, list) or not cmd:
        return [("mcp.hermes", "FAIL", "entry has no `command` array")]
    rows = [_row_for_launcher(cmd, hermes_bin)]
    rows += _rows_for_handshake(args, cmd)
    return rows


def _row_for_launcher(cmd: list, hermes_bin: Path | None) -> Row:
    """What the config points at, and whether that file is actually there."""
    configured = Path(str(cmd[0]))
    if not configured.exists():
        return (
            "mcp.hermes",
            "FAIL",
            f"command[0] points at a file that does not exist: {configured}"
            " -- run `python bridge.py setup`",
        )
    if hermes_bin is not None and not _same_file(configured, hermes_bin):
        return (
            "mcp.hermes",
            "INFO",
            f"config uses {configured}; bridge resolves {hermes_bin} "
            "(both exist, so this is only a note)",
        )
    return ("mcp.hermes", "PASS", " ".join(str(c) for c in cmd))


def _rows_for_handshake(args, cmd: list) -> list[Row]:
    """The MCP handshake, and the tool surface the server advertised."""
    if args.no_mcp_probe:
        return [("mcp handshake", "WARN", "skipped (--no-mcp-probe)")]
    probe = mcp_probe([str(b) for b in cmd], timeout=args.mcp_timeout)
    if not probe["ok"]:
        invoked = " ".join(str(c) for c in cmd)
        return [("mcp handshake", "FAIL", f"`{invoked}` failed: {probe['error']}")]
    names = probe["tools"]
    rows: list[Row] = [
        (
            "mcp handshake",
            "PASS",
            f"protocol={probe['protocol']} server={probe['server']} tools={len(names)}",
        )
    ]
    if not names:
        detail = "server answered but exposed no tools"
        rows.append(("mcp tool surface", "WARN", detail))
        return rows
    shown = ", ".join(names[:TOOLS_PREVIEW])
    rows.append(
        (
            "mcp tool surface",
            "PASS",
            f"{shown}{' ...' if len(names) > TOOLS_PREVIEW else ''}"
            " (MiMoCode namespaces these as hermes_<name>)",
        )
    )
    return rows


def _rows_for_model(args, config_path: Path, config: dict) -> list[Row]:
    """Which provider/model `ask --to mimo` will use, and whether its key resolves."""
    rows: list[Row] = []
    model, note = detect_model(config_path, config)
    from_env = os.environ.get("MIMO_BRIDGE_MODEL")
    if args.model:
        rows.append(("model", "PASS", f"{args.model} (from --model)"))
    elif from_env:
        rows.append(("model", "PASS", f"{from_env} (from MIMO_BRIDGE_MODEL)"))
    elif model:
        rows.append(("model", "PASS", f"{model} ({note})"))
    else:
        detail = f"cannot derive provider/model ({note}) -- set MIMO_BRIDGE_MODEL"
        rows.append(("model", "FAIL", detail))
    hint = missing_credential_hint(config)
    providers = config.get("provider")
    if not isinstance(providers, dict) or not providers:
        detail = "no `provider` block declared -- nothing to resolve"
        rows.append(("credentials", "INFO", detail))
    elif hint:
        detail = hint + " -- export it before using `ask --to mimo`"
        rows.append(("credentials", "FAIL", detail))
    elif any_provider_declares_api_key(config):
        detail = "provider apiKey resolved (values never printed)"
        rows.append(("credentials", "PASS", detail))
    else:
        rows.append(
            (
                "credentials",
                "INFO",
                "no apiKey declared; the provider may use OAuth or the environment",
            )
        )
    return rows


def _live_rows(
    args,
    hermes_bin: Path | None,
    mimo_bin: Path | None,
    config_path: Path,
    config: Any,
) -> list[Row]:
    """One real message each way -- the only rows that prove a channel answers."""
    rows: list[Row] = []
    if hermes_bin:
        rc, out, err = run_capture(
            [str(hermes_bin), "-z", "reply with the single word: pong"],
            timeout=args.timeout,
        )
        answered = rc == 0 and bool(out.strip())
        rows.append(_live_row("hermes", rc, out, err, answered=answered))
    if mimo_bin and config:
        model = args.model or os.environ.get("MIMO_BRIDGE_MODEL")
        if not model:
            model = detect_model(config_path, config)[0]
        if not model:
            rows.append(("live: -> mimo", "FAIL", "no model resolved"))
        else:
            rc, out, err = run_capture(
                [str(mimo_bin), "run", "reply with the single word: pong", "-m", model],
                timeout=args.timeout,
            )
            answered = rc == 0 and bool(strip_ansi(out).strip())
            rows.append(_live_row("mimo", rc, out, err, answered=answered))
    return rows


def _live_row(label: str, rc: int, out: str, err: str, answered: bool) -> Row:
    """A live run's row: the first line it printed, or why it printed nothing."""
    if answered:
        return (f"live: -> {label}", "PASS", first_line(out)[:LIVE_PREVIEW])
    detail = first_line(err or out)[:DETAIL_PREVIEW] or f"exit code {rc}"
    return (f"live: -> {label}", "FAIL", detail)


def cmd_check(args) -> int:
    """Health check of the wiring; non-zero exit when anything is broken."""
    print("mimo-hermes-bridge check")
    print("=" * 60)
    rows, ok = _check_rows(args)
    rows.sort(key=lambda r: STATUS_ORDER.get(r[1], 9))
    print(render_table(rows))
    print("=" * 60)
    fails = [r for r in rows if r[1] == "FAIL"]
    warns = [r for r in rows if r[1] == "WARN"]
    if ok:
        print(f"RESULT: PASS ({len(warns)} warning(s))")
    else:
        print(f"RESULT: FAIL ({len(fails)} failure(s), {len(warns)} warning(s))")
        print(
            "next steps: `python bridge.py setup` to fix the config, "
            "`python bridge.py doctor` to inspect"
        )
    # Stated unconditionally: a PASS only covers what is on disk and spawnable,
    # while a MiMoCode desktop window keeps the tool set it loaded at startup.
    print("note: this only covers what is on disk and can be spawned. Config changes")
    print("      reach MiMoCode in a NEW process -- an already-open desktop session")
    print("      keeps its loaded tools until you open a new session or restart it.")
    return EXIT_OK if ok else EXIT_CHECK_FAILED


def _report_reply(label: str, cmd: list[str], timeout: int, hint=None) -> int:
    """Run a one-shot agent command and report its reply.

    `hint` is an optional callable taking the combined stdout+stderr and
    returning extra lines to print when the command failed.
    """
    rc, out, err = run_capture(cmd, timeout=timeout)
    if rc in RC_LAUNCH_FAILURES:
        print(f"[ask] FAIL  {err}", file=sys.stderr)
        return EXIT_USAGE
    text = strip_ansi(out).strip()
    if text:
        print(text)
    if rc != 0:
        print(f"[ask] {label} exited {rc}", file=sys.stderr)
        tail = first_line(err)
        if tail:
            print(f"[ask] stderr: {tail}", file=sys.stderr)
        for line in hint((err or "") + (out or "")) if hint else ():
            print(line, file=sys.stderr)
        return rc
    if not text:
        print(f"[ask] {label} returned an empty reply.", file=sys.stderr)
        return EXIT_EMPTY_REPLY
    return EXIT_OK


def _hermes_failure_hint(combined: str) -> list[str]:
    """Extra guidance when a Hermes run failed, from what it printed."""
    low = combined.lower()
    if "auth" in low or "credential" in low:
        return [
            "[ask] hint: no Hermes credentials for that provider -- try `hermes login`",
            "[ask]       or select one with `hermes model`.",
        ]
    return []


def _mimo_failure_hint(combined: str, model: str) -> list[str]:
    """Extra guidance when a MiMo run failed, from what it printed."""
    low = combined.lower()
    if "missing x-opencode-session" in low:
        return [
            "[ask] hint: that zen/go endpoint requires the x-opencode-session header.",
            "[ask]       add it under provider.<name>.options.headers in your config.",
        ]
    if "model" in low and "not found" in low:
        return [f"[ask] hint: model '{model}' may not exist in your provider block."]
    return []


def build_hermes_argv(hermes_bin: Path, message: str, session: str | None) -> list[str]:
    """argv for a one-shot Hermes question.  `session` maps onto `hermes --resume`."""
    cmd = [str(hermes_bin)]
    if session:
        cmd += ["--resume", session]
    return [*cmd, "-z", message]


def build_mimo_argv(
    mimo_bin: Path,
    message: str,
    model: str,
    workdir: str | None,
    session: str | None,
) -> list[str]:
    """argv for a one-shot MiMoCode question.

    `session` must be an explicit id.  We never pass `--continue`: that resumes
    whatever ran last, which may be the session an open desktop window is using,
    and quietly injecting a message into someone's live conversation is not a
    risk worth taking to save a flag.
    """
    cmd = [str(mimo_bin), "run", message, "-m", model]
    if workdir:
        cmd += ["--dir", workdir]
    if session:
        cmd += ["--session", session]
    return cmd


def _ask_hermes(args, message: str) -> int:
    """Send the message to Hermes, or explain why we cannot."""
    hermes_bin = resolve_hermes_bin(args.hermes_bin)
    if hermes_bin is None or not hermes_bin.exists():
        print("[ask] FAIL  cannot locate the `hermes` executable.", file=sys.stderr)
        print(
            "[ask]       set HERMES_BIN=/full/path/to/hermes.exe (or --hermes-bin)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    cmd = build_hermes_argv(hermes_bin, message, args.session)
    return _report_reply("hermes", cmd, args.timeout, _hermes_failure_hint)


def _resolve_ask_model(args, config_path: Path, config) -> tuple[str | None, str]:
    """Model for the MiMo side: flag, then env, then the config's first provider."""
    model = args.model or os.environ.get("MIMO_BRIDGE_MODEL")
    if model or not config:
        return model, ""
    model, note = detect_model(config_path, config)
    return model, note


def _ask_mimo(args, message: str) -> int:
    """Send the message to MiMoCode, or explain why we cannot."""
    config_path = resolve_mimo_config(args.config)
    config = None
    if config_path.exists():
        try:
            config = load_jsonc(config_path)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[ask] FAIL  cannot parse {config_path}: {exc}", file=sys.stderr)
            print("[ask]       fix the JSONC, or pass --config <path>", file=sys.stderr)
            return EXIT_USAGE

    mimo_bin = resolve_mimo_bin(args.mimo_bin)
    if mimo_bin is None or not mimo_bin.exists():
        print("[ask] FAIL  cannot locate the `mimo` executable.", file=sys.stderr)
        print(
            "[ask]       note: npm often installs it under the Hermes node dir, which",
            file=sys.stderr,
        )
        print(
            "[ask]       is not on MiMoCode's PATH. Set MIMO_BIN=/full/path/to/mimo",
            file=sys.stderr,
        )
        return EXIT_USAGE

    model, _ = _resolve_ask_model(args, config_path, config)
    if not model:
        print("[ask] FAIL  no provider/model resolved.", file=sys.stderr)
        print(
            "[ask]       set MIMO_BRIDGE_MODEL=<provider>/<model> (or pass --model,",
            file=sys.stderr,
        )
        print(
            "[ask]       or add a `provider` block with models to your mimocode config).",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if config:
        hint = missing_credential_hint(config)
        if hint:
            print(f"[ask] FAIL  {hint}", file=sys.stderr)
            print("[ask]       export the variable, then re-run.", file=sys.stderr)
            return EXIT_USAGE

    cmd = build_mimo_argv(mimo_bin, message, model, args.dir, args.session)
    return _report_reply(
        "mimo",
        cmd,
        args.timeout,
        lambda combined: _mimo_failure_hint(combined, model),
    )


def cmd_ask(args) -> int:
    """Send one message to the other side and print that side's reply."""
    message = " ".join(args.message).strip()
    if not message:
        print(
            '[ask] nothing to send -- usage: bridge.py ask --to mimo|hermes "message"',
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.to == "hermes":
        return _ask_hermes(args, message)
    return _ask_mimo(args, message)


def _presence(path: Path) -> str:
    """Whether a path a user is about to look at is actually there."""
    return "exists" if path.exists() else "MISSING"


def cmd_doctor(args) -> int:
    """Print everything bridge.py can see about this machine, writing nothing."""
    print("mimo-hermes-bridge doctor (read-only: nothing is written)")
    print("=" * 60)
    config_path = resolve_mimo_config(args.config)
    hermes_home = resolve_hermes_home(args.hermes_home)
    hermes_bin = resolve_hermes_bin(args.hermes_bin)
    mimo_bin = resolve_mimo_bin(args.mimo_bin)

    print(f"bridge version      : {VERSION}")
    print(f"python              : {sys.version.split()[0]} ({sys.executable})")
    print(f"mimo config         : {config_path}  [{_presence(config_path)}]")
    print(f"mimo binary         : {mimo_bin or 'NOT FOUND'}")
    print(f"hermes binary       : {hermes_bin or 'NOT FOUND'}")
    print(f"hermes home         : {hermes_home}  [{_presence(hermes_home)}]")

    _print_config_summary(config_path)
    _print_hermes_home_files(hermes_home)
    _print_env_report()

    print("-" * 60)
    _print_version_lines(mimo_bin, hermes_bin, args.timeout)
    print("=" * 60)
    return EXIT_OK


def _print_config_summary(config_path: Path) -> None:
    """Print what the MiMoCode config says about mcp, model and credentials."""
    if not config_path.exists():
        return
    try:
        config = load_jsonc(config_path)
        mcp = config.get("mcp")
        entry = _member(mcp, MCP_SERVER_NAME)
        print(f"mcp.{MCP_SERVER_NAME:<16}: {'present' if entry else 'absent'}")
        if entry:
            argv = " ".join(str(c) for c in entry.get("command", []))
            print(f"  command           : {argv}")
            print(f"  enabled           : {entry.get('enabled')}")
        model, note = detect_model(config_path, config)
        print(f"model               : {model or 'unresolved'} ({note})")
        hint = missing_credential_hint(config)
        resolved = hint or "ok (apiKey resolved; value not printed)"
        print(f"credentials         : {resolved}")
        providers = config.get("provider") or {}
        declared = ", ".join(providers) if providers else "(none)"
        print(f"providers declared  : {declared}")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"config parse        : FAILED -- {exc}")


def _print_hermes_home_files(hermes_home: Path) -> None:
    """Print the Hermes files a user may need to open by hand."""
    for label, path in (
        ("hermes config.yaml", hermes_home / "config.yaml"),
        ("hermes .env", hermes_home / ".env"),
        ("hermes auth.json", hermes_home / "auth.json"),
        ("hermes data dir", hermes_home),
    ):
        print(f"{label:<20}: {_presence(path)} ({path})")


def _print_env_report() -> None:
    """Print the environment bridge.py reads -- values, except HERMES_HOME's path."""
    for name in (
        "MIMO_CONFIG",
        "MIMO_BIN",
        "HERMES_BIN",
        "HERMES_HOME",
        "MIMO_BRIDGE_MODEL",
        "MIMO_BRIDGE_TIMEOUT",
    ):
        val = os.environ.get(name)
        shown = "set" if name == "HERMES_HOME" else val
        print(f"env {name:<18}: {shown or 'unset'}")


def _print_version_lines(
    mimo_bin: Path | None,
    hermes_bin: Path | None,
    timeout: int,
) -> None:
    """Ask each CLI for its version -- the only part of doctor that spawns a process."""
    for label, binary in (("mimo", mimo_bin), ("hermes", hermes_bin)):
        if binary and binary.exists():
            rc, out, err = run_capture([str(binary), "--version"], timeout=timeout)
            version = first_line(out) or first_line(err) or f"exit {rc}"
            print(f"{label + ' --version':<20}: {version}")


def cmd_render(args) -> int:
    """Render a JSONC fragment with placeholders substituted, and print the result."""
    # Keep the placeholder when hermes cannot be located: rendering is also used
    # to inspect what the fragment *would* look like.
    fallback = "{{HERMES_BIN}}"
    mappings = {"HERMES_BIN": str(resolve_hermes_bin(args.hermes_bin) or fallback)}
    for pair in args.set or []:
        if "=" not in pair:
            print(f"[render] bad --set {pair!r}, expected KEY=value", file=sys.stderr)
            return EXIT_USAGE
        key, _, val = pair.partition("=")
        mappings[key] = val
    fragment = Path(args.fragment or FRAGMENT_PATH)
    if not fragment.exists():
        print(f"[render] fragment not found: {fragment}", file=sys.stderr)
        return EXIT_USAGE
    try:
        data = render_fragment(fragment, mappings)
    except PlaceholderError as exc:
        print(f"[render] FAIL  {exc}", file=sys.stderr)
        return EXIT_CHECK_FAILED
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface: shared path/timeout options, then one parser per subcommand."""
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description="Connect MiMoCode (OpenCode fork) and Hermes Agent on one machine.",
    )
    parser.add_argument("--version", action="version", version=f"bridge.py {VERSION}")
    shared_env = argparse.ArgumentParser(add_help=False)
    shared_env.add_argument(
        "--config",
        help="path to mimocode.jsonc (env: MIMO_CONFIG)",
    )
    shared_env.add_argument(
        "--hermes-bin",
        help="path to hermes executable (env: HERMES_BIN)",
    )
    shared_env.add_argument(
        "--mimo-bin",
        help="path to mimo executable (env: MIMO_BIN)",
    )
    shared_env.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("MIMO_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT)),
        help="subprocess timeout in seconds",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser(
        "setup",
        parents=[shared_env],
        help="detect both sides and merge config (idempotent, backs up first)",
    )
    p_setup.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change",
    )
    p_setup.add_argument(
        "--restore",
        nargs="?",
        const="",
        metavar="BACKUP",
        help="roll the config back to a backup (default: the newest one)",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_check = sub.add_parser(
        "check",
        parents=[shared_env],
        help="health check of the wiring, non-zero exit on failure",
    )
    p_check.add_argument("--model", help="provider/model for the MiMo side")
    p_check.add_argument(
        "--live",
        action="store_true",
        help="also send a real message each way (costs tokens)",
    )
    p_check.add_argument(
        "--no-mcp-probe",
        action="store_true",
        help="skip the MCP handshake",
    )
    p_check.add_argument(
        "--mcp-timeout",
        type=int,
        default=30,
        help="MCP handshake timeout",
    )
    p_check.set_defaults(func=cmd_check)

    p_ask = sub.add_parser(
        "ask",
        parents=[shared_env],
        help="send one message to the other side",
    )
    p_ask.add_argument("--to", choices=["mimo", "hermes"], required=True)
    p_ask.add_argument("--model", help="provider/model override for the MiMo side")
    p_ask.add_argument("--dir", help="working directory for `mimo run`")
    p_ask.add_argument(
        "--session",
        metavar="ID",
        help="continue one specific session (mimo: --session, hermes: "
        "--resume). Default is a fresh session on both sides.",
    )
    p_ask.add_argument("message", nargs=argparse.REMAINDER)
    p_ask.set_defaults(func=cmd_ask)

    p_doctor = sub.add_parser(
        "doctor",
        parents=[shared_env],
        help="read-only diagnostics",
    )
    p_doctor.add_argument("--hermes-home", help="Hermes data dir (env: HERMES_HOME)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_render = sub.add_parser(
        "render",
        parents=[shared_env],
        help="render a fragment to JSON",
    )
    p_render.add_argument(
        "--fragment",
        help="fragment path (default: examples/mimocode.jsonc.fragment)",
    )
    p_render.add_argument(
        "--set",
        action="append",
        metavar="KEY=value",
        help="override a placeholder",
    )
    p_render.set_defaults(func=cmd_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse the CLI and hand off to the subcommand; returns its exit code."""
    # Windows consoles default to a legacy code page; non-ASCII paths (very
    # common) would otherwise crash a print() or mangle subprocess output.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
