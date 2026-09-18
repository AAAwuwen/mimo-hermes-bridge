# mimo-hermes-bridge

> 中文说明 / Chinese: **[README.zh-CN.md](README.zh-CN.md)**

Let two local AI agents talk to each other: **[MiMoCode](https://mimo.xiaomi.com)**
(an OpenCode fork) and **[Hermes Agent](https://hermes.nousresearch.com)**.

They both speak MCP and both have a one-shot CLI, but out of the box they have no
idea the other exists. This repository is one stdlib-only Python file that (a) wires
them together and (b) proves the wiring works, so nobody has to rediscover the same
handful of traps.

```
                 one machine
   +-------------------------------+      +------------------------------+
   |  MiMoCode  (OpenCode fork)     |      |  Hermes Agent                |
   |                               |      |                              |
   |  mimo run "..." -m p/m        |----->|  hermes -z "..."             |  channel A
   |    (one-shot CLI)             |      |   (one-shot CLI)             |  text out
   |    --session <id> resumes     |      |   --resume <id> resumes      |  only
   |                               |      |                              |
   |  mcp.hermes = [hermes.exe,    |<-----|  hermes mcp serve            |  channel B
   |                "mcp","serve"] | stdio|   (MCP server over stdio)    |  structured
   |    -> 10 tools as hermes_*    |      |                              |  tools
   +-------------------------------+      +------------------------------+
```

Both directions exist because they fail differently: the MCP channel gives MiMoCode
*structured* tools (read conversations, send messages, poll events) but only inside a
MiMoCode process that loaded the config; the CLI channel is one-shot, returns plain
text, and works from scripts, cron, and CI where no MCP host is running.

## What "connected" does and does not mean

**The two directions are not symmetric, and this tool does not pretend otherwise.**

| direction | mechanism | what you actually get |
|---|---|---|
| MiMoCode → Hermes | MCP, declared in the MiMoCode config | 10 structured `hermes_*` tools, inside any MiMoCode process that loads the config |
| Hermes → MiMoCode | `mimo run` (what `bridge.py ask` uses) | **plain text only** — no structured return, no session continuity guarantee |

`setup` wires **only** the MiMoCode side. Registering MiMoCode as a server *for Hermes*
is done with `hermes mcp add`, which is a discovery-first **interactive** command — it
cannot be scripted deterministically, so wiring it into `setup` would break both
idempotency and unattended runs. Do it yourself if you want that direction:

```bash
hermes mcp add            # interactive: pick/add a server, follow the prompts
hermes mcp list           # confirm it registered
hermes mcp test <name>    # probe the connection
```

The two channels also have independent failure modes and credentials, so a green
`check` does not guarantee every channel works. See the traps below.

## Built by two agents, not one

This repository is itself the output of a **two-agent collaboration protocol**: one agent wrote
the requirements and accepted the work, the other implemented it. The rules live in the repo,
not in a chat log — `AGENTS.md` is auto-loaded by agents that enter this directory.

| rule | where it lives |
|---|---|
| Communication is structured **JSON** (`from/to/round/kind/...`), never free-form prose | `AGENTS.md` §1 |
| A topic gets **at most three rounds**; still unresolved → escalate to the human, never self-decide and continue | `AGENTS.md` §2 |
| **Mutual challenge is mandatory** — the other side must attack the claim before accepting it; a rejection carries its reason, an acceptance names the argument it adopted; self-criticism is expected | `AGENTS.md` §3 |
| **No personal data** anywhere: placeholders only, plus a pre-publish scan for usernames, machine names, real paths, private IPs and key-shaped strings | `AGENTS.md` §4 |
| **Acceptance = the reviewer re-runs the same commands.** Self-reported success does not count | `AGENTS.md` §5 |
| **Clean code is verifiable**: `ruff check` + `ruff format --check` + tests green — no `noqa` hiding, no behaviour change just to satisfy a linter | `AGENTS.md` §6 |

`DECISIONS.md` is the ledger: every disagreement, both sides' arguments in their own words,
and who prevailed on which grounds. It records the calls that went against the original
requirements, too — see `D6-1` (the "bidirectional" claim was withdrawn) and `H1`.

## Verification scope (read this before assuming it works for you)

**End-to-end verified:** MiMoCode (an OpenCode fork) ↔ Hermes Agent, on Windows 11 — both
channels, using the commands in this README. That is the combination this repo was built and
tested against; the numbers in `README` and `DECISIONS.md` come from that machine.

**Same mechanism, NOT verified by us:** any other MCP client can mount `hermes mcp serve` —
Codex has `codex mcp add <name> -- <stdio command>` (config in `~/.codex/config.toml`, and
project-scoped `.codex/config.toml`), Claude Code has `claude mcp add`. The **MCP half**
(channel B) should transfer as-is. The **`ask --to mimo` half** (channel A) is
MiMoCode/OpenCode-specific and would need an adapter for another CLI. Neither has been tested
here — treat them as untested, not as supported.

We would rather under-claim. A repo that says "works everywhere" and then fails on your setup
wastes your afternoon; a repo that tells you exactly what was tested saves it.

## Prerequisites for channel A (read this first)

`ask --to mimo` drives **your** MiMoCode CLI. It does **not** ship a provider, a model list,
or a credential, and it never writes one into your config.

If your MiMoCode is not signed in and has no third-party provider configured, the CLI itself
answers with something like `MiMo free API service has ended. Sign in or configure a
third-party API.` — and channel A cannot work. **That is your call, not this repo's**: sign in,
or point MiMoCode at a provider you choose. This project deliberately stays out of it.

Channel B (the `hermes mcp serve` entry) has no such dependency: it only runs program code you
already have.

## Requirements

- Python **3.10+** (3.11+ recommended) — no third-party packages, ever.
- MiMoCode installed and on disk (`mimo`).
- Hermes Agent installed and on disk (`hermes`, with `hermes mcp serve` available).
- Windows is the primary target (Git-Bash/MSYS **and** PowerShell); macOS/Linux work too.
- `uv` (optional) — only for running the linter, not to use the tool.

### Verified against

Integration behaviour is version-sensitive. These are the versions this tool was
actually exercised against; re-run `python bridge.py check` after upgrading either side.

| component | version verified |
|---|---|
| Python | 3.11.16 |
| MiMoCode (`mimo`) | 0.1.14 |
| Hermes Agent (`hermes`) | v0.21.2 (2026.9.11), upstream `5eb99eb2` |
| MCP protocol seen in handshake | `2024-11-05` |
| ruff (lint contract only) | 0.16.8 via `uvx` |

## Quick start

```bash
python bridge.py doctor          # read-only: where is everything?
python bridge.py setup           # merge the MCP entry into your config (backs up first)
python bridge.py check           # wiring + Hermes endpoint health; exit 0 = healthy
python bridge.py ask --to hermes "what is on my plate today?"
python bridge.py ask --to mimo "summarise this repo in three bullets"
```

`setup` prints the backup path and is safe to re-run: if `mcp.hermes` already matches
what it would write, it writes nothing at all.

## Exit codes

A stable contract; scripts may depend on it.

| code | meaning |
|---|---|
| `0` | success / healthy |
| `1` | `check` found a FAIL — the wiring is broken |
| `2` | usage error or missing input (no binary, no model, unset credential, unparsable config) |
| `3` | the other side replied, but with nothing in it |

## Commands

### `setup` — detect, then merge (idempotent)

```bash
python bridge.py setup [--config PATH] [--hermes-bin PATH] [--dry-run]
python bridge.py setup --restore [BACKUP]
```

- Locates the MiMoCode config (`mimocode.jsonc`) and the `hermes` executable.
- Merges this into the config, creating the `mcp` object if it is missing:

  ```jsonc
  "mcp": {
    "hermes": {
      "type": "local",
      "command": ["<absolute path to hermes>", "mcp", "serve"],
      "enabled": true
    }
  }
  ```

- **Backs the config up first** and prints the backup path
  (`mimocode.jsonc.bak.YYYYmmdd_HHMMSS`).
- **Idempotent** in the strong sense: *already working ⇒ do not touch*. If `mcp.hermes`
  already routes to a launcher that exists on disk — even a **different** one from the
  one this tool would pick — it writes nothing. Pass `--hermes-bin` to pin a specific
  launcher. Re-writing a working config would only churn it and drop comments.
- Edits the text in place, so your comments and formatting survive. The single
  exception is when an existing `mcp.hermes` has to be *replaced* — then the config is
  re-serialised as clean JSON and the tool tells you comments were lost.
- `--dry-run` shows the plan without touching anything.

#### `--restore` — roll back

```bash
python bridge.py setup --restore                    # newest backup next to the config
python bridge.py setup --restore path/to/backup     # a specific backup file
```

A backup you cannot restore from is only half a safety feature, so this closes the loop.
Restoring is itself a config change, so the current file is backed up too — under a
*different* tag, `mimocode.jsonc.prerestore.YYYYmmdd_HHMMSS`. That distinction matters:
if the safety copy were also named `*.bak.*`, the *next* `--restore` would pick up that
copy as "the newest backup" and undo the restore. Restoring twice is a stable no-op.
A backup that is not valid JSONC is refused rather than written over a good config.

### `check` — is the wiring sound?

```bash
python bridge.py check [--live] [--no-mcp-probe] [--mcp-timeout 30]
```

Prints a PASS/FAIL/WARN/INFO table and **exits non-zero if anything FAILed**.

State the claim precisely, because the observation boundary is real:

> `check` verifies **the config is in place and the Hermes endpoint is alive**. It does
> **not**, and cannot, verify that a running MiMoCode process actually loaded those
> tools — that state lives inside that process. Only `check --live` sends real traffic.

| check | what it means |
|---|---|
| `hermes bin` / `mimo bin` | the executables were found on disk |
| `mimocode.jsonc` + `jsonc parse` | config exists and is valid JSONC |
| `mcp.hermes` | entry present, `enabled: true`, `command[0]` exists on disk |
| `mcp handshake` | **real MCP stdio handshake** — spawns `hermes mcp serve`, sends `initialize` + `tools/list` |
| `mcp tool surface` | the tools the server advertises (MiMoCode prefixes them with the server key) |
| `model` / `credentials` | a `provider/model` could be derived; whether an `apiKey` resolves (values are never printed) |

Every run also prints a standing reminder that config changes only reach **new**
MiMoCode processes. That reminder is not decoration — see trap 1.

`--live` additionally sends a real message each way (costs tokens, needs credentials).
The default is deliberately offline: an MCP handshake proves the channel is wired
without spending a cent.

### `ask` — one message to the other side

```bash
python bridge.py ask --to hermes "message"          # runs: hermes -z "message"
python bridge.py ask --to mimo   "message"          # runs: mimo run "message" -m <model>
```

Options: `--model provider/model`, `--dir DIR` (working directory for `mimo run`),
`--session ID`, `--timeout SECONDS` (default 180).

**Both sides start a fresh session by default.** To continue an existing one you must
name it explicitly with `--session ID` (`mimo run --session <id>` /
`hermes --resume <id>`). There is deliberately **no** `--continue` shorthand: that would
resume "the last session", which may be one an open desktop window is using, and quietly
injecting a message into a conversation the user did not intend is a real risk, not a
stylistic one.

The model is resolved as `--model` → `$MIMO_BRIDGE_MODEL` → auto-detected as the first
provider in your MiMoCode config that declares models. If nothing resolves, or the
binary/credentials are missing, you get a plain sentence naming what to fix — never a
stack trace. ANSI escape codes are stripped (`mimo run` colours its output even when
piped).

### `doctor` — read-only diagnostics

```bash
python bridge.py doctor [--hermes-home PATH]
```

Resolved paths, whether each config/credential file exists, declared providers, the
env vars `bridge.py` reads, and both CLI versions. **Writes nothing.** Environment
variable values are reported as `set`/`unset` only — no secrets.

**`doctor` and `check` are not two names for one thing, and are not meant to be merged:**

| | `doctor` | `check` |
|---|---|---|
| purpose | inspect — report facts | judge — decide if the wiring is sound |
| can fail? | **no** — always exits `0` | yes — non-zero on FAIL |
| exit code | never meaningful | the whole point |
| use from | a human debugging | scripts, cron, CI |

Collapsing them would push the "should I look at the exit code?" decision onto every
caller, which is exactly the decision each command exists to make for you.

### `render` — expand the example fragment

```bash
python bridge.py render --set HERMES_BIN=/path/to/hermes.exe
```

Renders `examples/mimocode.jsonc.fragment` to JSON on stdout.

**Scope: it renders the fragment and nothing else. It does not participate in wiring.**
It exists because two requirements pull in opposite directions — the repo must not carry
a real machine path, so the fragment ships with a `{{HERMES_BIN}}` placeholder, yet a
fragment you cannot paste is not usable. `render` resolves that, and in doing so keeps
the fragment *testable*: it shares its substitution code with `setup`, so a broken
fragment fails the test suite instead of silently becoming an unverified dead file. For
actual wiring, use `setup`.

## Configuration

Everything is resolved at runtime: **CLI flag → environment variable → platform
default**. Nothing is hard-coded, and no secrets live in this repository.

| variable | meaning | default |
|---|---|---|
| `MIMO_CONFIG` | path to `mimocode.jsonc` | `$HOME/.config/mimocode/mimocode.jsonc` |
| `MIMO_BIN` | path to the `mimo` launcher | `PATH`, then known install dirs |
| `HERMES_BIN` | path to the `hermes` launcher | `PATH`, then known install dirs |
| `HERMES_HOME` | Hermes data directory | `%LOCALAPPDATA%\Hermes` |
| `MIMO_BRIDGE_MODEL` | default `provider/model` for `ask --to mimo` | auto-detected |
| `MIMO_BRIDGE_TIMEOUT` | subprocess timeout, seconds | `180` |

Matching flags exist for every one of them (`--config`, `--mimo-bin`, `--hermes-bin`,
`--hermes-home`, `--model`, `--timeout`).

## Traps (learned the hard way)

1. **A desktop session is not a CLI session.** `mimo run` always starts or resumes its
   *own* session; it never touches the session the desktop app has open. And config
   changes are read **only by new processes** — the desktop app must open a new session
   (or restart) before it sees the `hermes` MCP tools. `check` will happily PASS while a
   long-running desktop window still shows no `hermes_*` tools; that is expected, and
   every `check` run now says so explicitly.
2. **Provider whitelist.** MiMoCode only accepts the `opencode/` and `mimo/` provider
   prefixes out of the box. Anything else (`zai`, `deepseek`, `openrouter`, a private
   gateway) needs a **custom provider** block.
3. **Custom provider shape.** Use `"npm": "@ai-sdk/openai-compatible"` plus
   `options.baseURL` / `options.apiKey` / `options.headers`, then declare your models
   under `models`. The model string for `ask` is `<your-provider-key>/<model-key>`, not
   `<npm package>/<model>`.
4. **`x-opencode-session` is mandatory on the zen/go endpoints.** Omit the header and
   you get `Request is missing x-opencode-session`. Put it in
   `provider.<key>.options.headers`. `ask --to mimo` recognises that message and tells
   you so.
5. **`mimo` is often not where you think it is.** npm frequently installs it into
   *Hermes'* bundled Node directory, which is not on MiMoCode's own `PATH`. That is why
   `setup` writes an **absolute** path into the MCP `command` array and why `bridge.py`
   has extra fallback locations. If detection fails, set `MIMO_BIN` / `HERMES_BIN`.
6. **Subprocesses do not get a shell.** MCP `command[0]` is spawned directly — no
   `PATH` lookup, no `~` expansion, no quoting. Absolute paths only.
7. **The desktop app audits every input** (`/api/audit/check`) and ships with
   `perm-autopass` for `bash` and `external_directory`. The directory boundary is on
   you: be explicit about `--dir`, and do not paste secrets into prompts.
8. **ANSI colour survives piping.** `mimo run` emits escape codes even when stdout is
   not a terminal, so naive log capture looks like `[0m[0m> build`. `bridge.py` strips
   them; your own scripts probably should too.
9. **Legacy code pages.** On a Chinese Windows install, the console code page is not
   UTF-8; `bridge.py` reconfigures its own stdout/stderr to UTF-8 so non-ASCII paths do
   not crash it (or turn into mojibake in captured output).
10. **Git-Bash gives you POSIX paths, and native Python silently misreads them.**
    `mktemp -d` returns something like `/tmp/tmp.abc123`, which a native Windows Python
    reads as `<DRIVE>:\tmp\tmp.abc123` — a **different file**, with a misleading
    "file not found". `bridge.py` translates via `cygpath` when it detects MSYS
    (`MSYSTEM` set) and the path does not exist. Only absolute `/`-prefixed,
    non-existent paths are touched; relative paths are never rewritten.
11. **MCP tool names differ on each side of the wire.** `hermes mcp serve` advertises
    `messages_send`, `conversations_list`, … The `hermes_` prefix is added by the
    **client** (MiMoCode), from the server key in your config. Writing docs or scripts
    from the server-side names is a common, confusing mistake.
12. **Two `hermes` launchers can both be valid.** A launcher under the app dir and the
    venv console script next to it are different files that both work. Your config may
    name one while `bridge.py` resolves the other; that is a note, not a fault — which
    is exactly why `setup` will not churn a working config, and why `check` reports the
    difference as `INFO` rather than `WARN`.
13. **A backup is itself a new copy of the secret.** `mimocode.jsonc.bak.*` contains the
    same `apiKey` in plaintext. Backing up is not purely protective — it multiplies the
    number of places a live key sits on disk. Delete stale backups.
14. **`hermes -z` and `hermes mcp serve` are different code paths** with potentially
    different credential scopes. Either can work while the other fails, so a green
    `check` does not prove the CLI channel, and a working `ask --to hermes` does not
    prove the MCP tools.
15. **On Windows, killing a process kills only that process.** A hung MCP server with
    children can leave an orphan tree behind. The handshake has a timeout and closes
    the pipes, but a wedged server is not guaranteed to be reaped cleanly.
16. **A Hermes home can be briefly unreadable.** Seen in the field: `hermes -z` exited 1
    with `Cannot initialize Hermes directory <HOME>: [WinError 5] access is denied`, and
    the *very next* run of the same command succeeded with nothing changed on disk — the
    directory was momentarily locked (another Hermes process tightening ACLs, a scanner
    holding it). `ask` now treats that as transient, **retries exactly once**, and prints
    the **last** lines of stderr instead of only the first, so a flake no longer reads
    like a broken install. A real permission problem still fails, with next steps.
17. **`ask` accepts the message before or after the flags.** `bridge.py ask "msg" --to
    hermes` used to die with `the following arguments are required: --to`: the message
    positional used `argparse.REMAINDER`, and REMAINDER stops option parsing at the first
    positional. Both orders work now.
18. **`check` WARNs — and still exits 0 — when no model can be derived.** A model is only
    needed by `ask --to mimo`; the MCP channel and `ask --to hermes` need none. It is a
    warning rather than a failure on purpose: a FAIL there pushed readers towards `setup`,
    i.e. towards this repo writing a provider block into your config, which it will not do
    (see "Prerequisites for channel A").

## Security notes

- `bridge.py` **never prints credential values.** `doctor`/`check` only report whether a
  key resolved, and env vars as `set`/`unset`.
- Your MiMoCode config holds a live `apiKey`. It is a secret. `.gitignore` here covers
  `*.bak.*`, `*.prerestore.*`, `*.env`, local log/transcript files, and the vendored
  `.mimocode/` tree — but keep the config itself out of any repo.
- Everything runs as your user. MCP is a local stdio pipe; nothing listens on a port,
  and no traffic leaves the machine beyond the model providers you already configured.

## Tests

```bash
python tests/test_bridge.py            # stdlib unittest, no pytest required
python tests/test_bridge.py -v         # verbose
```

The suite never touches your real configuration: every case runs inside a temporary
directory and drives `bridge.py` as a subprocess where behaviour matters. It covers
JSONC parsing (comments, trailing commas, strings that contain `//`), idempotent merging,
the backup/restore round trip, placeholder substitution, the `ask` argv contract, and the
degraded paths of `check` / `ask`.

## Lint

```bash
uvx ruff check .
uvx ruff format --check .
```

`ruff.toml` pins the rule set and the 100-character line limit. `pt` (flake8-pytest-style)
is deliberately not selected: this project uses stdlib `unittest`, not pytest, so those
rules do not apply. `.mimocode/` is excluded because it is a vendored npm dependency
tree, not our source.

## Layout

```
bridge.py                            the whole tool (stdlib only)
examples/mimocode.jsonc.fragment     paste-ready MCP snippet ({{HERMES_BIN}} placeholder)
tests/test_bridge.py                 stdlib unittest suite
ruff.toml                            lint contract (rule set + line length)
LICENSE                              MIT
DECISIONS.md                         decision ledger: who argued what, and why
PROJECT_BLUEPRINT.md                 requirements + acceptance criteria (working doc)
```

## License

MIT — see [LICENSE](LICENSE).
