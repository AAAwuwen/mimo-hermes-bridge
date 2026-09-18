#!/usr/bin/env python3
"""Tests for bridge.py.

Runnable two ways:

    python tests/test_bridge.py          # no pytest needed
    python -m pytest tests/              # works too

Nothing here touches your real MiMoCode or Hermes configuration: every case runs in a
temporary directory, and the cases that care about process behaviour drive bridge.py as
a subprocess.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402  (only importable after the sys.path insert above)

FAKE_KEY = "<REDACTED-TEST-KEY>"
FAKE_HERMES = "C:\\Users\\<USER>\\hermes\\bin\\hermes.exe"


def clean_env(**overrides: str) -> dict:
    """A subprocess environment with every bridge variable deliberately unset."""
    env = dict(os.environ)
    for name in (
        "MIMO_CONFIG",
        "MIMO_BIN",
        "HERMES_BIN",
        "HERMES_HOME",
        "MIMO_BRIDGE_MODEL",
        "MIMO_BRIDGE_TIMEOUT",
    ):
        env.pop(name, None)
    env.update({k: v for k, v in overrides.items() if v is not None})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_bridge(*args: str, env: dict | None = None, cwd: Path | None = None):
    """Drive bridge.py as a real subprocess, the way a user or MiMoCode would."""
    return subprocess.run(
        [sys.executable, str(ROOT / "bridge.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env or clean_env(),
        cwd=str(cwd or ROOT),
        timeout=120,
        check=False,  # the tests assert on the exit code themselves
    )


# The key is injected via a slot rather than %-formatting: JSON braces inside an
# f-string would all have to be doubled, which buries the document we are testing.
KEY_SLOT = "__FAKE_KEY__"

GOOD_CONFIG = """{
  // keep this comment -- setup must not eat it
  "provider": {
    "testprov": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://example.invalid/v1", "apiKey": "__FAKE_KEY__" },
      "models": { "test-model": { "name": "Test Model" } }
    }
  },
  "mcp": {
    "someoneelse": { "type": "local", "command": ["/bin/true"], "enabled": true }
  },
  "trailingRoot": 1
}
""".replace(KEY_SLOT, FAKE_KEY)


def expected_entry(hermes_bin: str) -> dict:
    return {
        "type": "local",
        "command": [hermes_bin, "mcp", "serve"],
        "enabled": True,
    }


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bridge-tests-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write_config(self, text: str, name: str = "mimocode.jsonc") -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_setup(self, config: Path, hermes_bin: str, dry_run: bool = False):
        """Drive `setup` against this case's config, the way a user would."""
        argv = ["setup", "--config", str(config), "--hermes-bin", hermes_bin]
        if dry_run:
            argv.insert(1, "--dry-run")
        return run_bridge(*argv)

    def backup_names(self) -> list[str]:
        """The `setup` backups currently sitting next to the config."""
        return sorted(p.name for p in self.tmp.glob("mimocode.jsonc.bak.*"))


# --------------------------------------------------------------------------
# JSONC reading
# --------------------------------------------------------------------------


class JsoncTests(unittest.TestCase):
    def test_line_comments_and_trailing_commas(self):
        text = '{\n  // a comment\n  "a": 1,\n  "b": [1, 2,],\n}\n'
        self.assertEqual(bridge.load_jsonc_text(text), {"a": 1, "b": [1, 2]})

    def test_block_comments(self):
        text = '{ /* leading */ "a": 1 /* trailing */ }'
        self.assertEqual(bridge.load_jsonc_text(text), {"a": 1})

    def test_comment_markers_inside_strings_survive(self):
        raw = {
            "url": "https://example.invalid//not-a-comment",
            "note": "a /* b */ c",
            "escaped": 'quote \\" and slash \\\\ done',
        }
        text = json.dumps(raw)
        self.assertEqual(bridge.load_jsonc_text(text), raw)

    def test_empty_object(self):
        self.assertEqual(bridge.load_jsonc_text("{}"), {})
        self.assertEqual(bridge.load_jsonc_text("{}\n"), {})

    def test_invalid_json_still_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            bridge.load_jsonc_text('{ "a": }')


# --------------------------------------------------------------------------
# placeholder substitution
# --------------------------------------------------------------------------


class PlaceholderTests(unittest.TestCase):
    def test_exact_placeholder_takes_the_raw_value(self):
        out = bridge.substitute_placeholders({"v": "{{FLAG}}"}, {"FLAG": True})
        self.assertIs(out["v"], True)

    def test_list_can_be_injected(self):
        out = bridge.substitute_placeholders({"v": "{{CMD}}"}, {"CMD": ["a", "b"]})
        self.assertEqual(out["v"], ["a", "b"])

    def test_inline_placeholder_inside_a_longer_string(self):
        template = "--config={{PATH}} --verbose"
        out = bridge.substitute_placeholders(template, {"PATH": "/x"})
        self.assertEqual(out, "--config=/x --verbose")

    def test_windows_path_survives_a_json_round_trip(self):
        rendered = bridge.substitute_placeholders(
            {"p": "{{HERMES_BIN}}"}, {"HERMES_BIN": FAKE_HERMES}
        )
        self.assertEqual(rendered["p"], FAKE_HERMES)
        reloaded = json.loads(json.dumps(rendered))
        self.assertEqual(reloaded["p"], FAKE_HERMES)

    def test_unknown_placeholder_raises_and_names_the_key(self):
        with self.assertRaises(bridge.PlaceholderError) as ctx:
            bridge.substitute_placeholders({"p": "{{NOPE}}"}, {"HERMES_BIN": "x"})
        self.assertIn("NOPE", str(ctx.exception))


# --------------------------------------------------------------------------
# idempotent merging
# --------------------------------------------------------------------------


class MergeTests(unittest.TestCase):
    def test_insert_into_populated_container_preserves_everything_else(self):
        text, changed, how = bridge.merge_named_entry(
            GOOD_CONFIG, "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        self.assertTrue(changed)
        self.assertEqual(how, "insert-into")
        self.assertIn("// keep this comment", text)
        self.assertIn('"someoneelse"', text)
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(FAKE_HERMES))
        self.assertEqual(
            data["mcp"]["someoneelse"],
            {"type": "local", "command": ["/bin/true"], "enabled": True},
        )
        self.assertEqual(data["trailingRoot"], 1)
        models = data["provider"]["testprov"]["models"]
        self.assertEqual(models, {"test-model": {"name": "Test Model"}})

    def test_second_merge_is_a_no_op_returning_identical_text(self):
        first, changed1, _ = bridge.merge_named_entry(
            GOOD_CONFIG, "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        second, changed2, how2 = bridge.merge_named_entry(
            first, "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        self.assertTrue(changed1)
        self.assertFalse(changed2)
        self.assertEqual(how2, "unchanged")
        self.assertEqual(second, first)

    def test_insert_into_empty_container(self):
        text, _, how = bridge.merge_named_entry(
            '{\n  "mcp": {}\n}\n', "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        self.assertEqual(how, "insert-empty")
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(FAKE_HERMES))

    def test_creates_the_container_when_absent(self):
        text, _, how = bridge.merge_named_entry(
            '{\n  "provider": {}\n}\n', "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        self.assertEqual(how, "append-container")
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(FAKE_HERMES))
        self.assertEqual(data["provider"], {})

    def test_creates_the_container_in_an_empty_document(self):
        text, _, how = bridge.merge_named_entry(
            "{}\n", "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        self.assertEqual(how, "append-container")
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(FAKE_HERMES))

    def test_conflicting_entry_is_replaced(self):
        stale = (
            '{\n  "mcp": {\n    "hermes": {\n      "type": "local",\n'
            '      "command": ["/old/hermes"],\n      "enabled": false\n    }\n  }\n}\n'
        )
        text, changed, how = bridge.merge_named_entry(
            stale, "mcp", "hermes", expected_entry(FAKE_HERMES)
        )
        self.assertTrue(changed)
        self.assertEqual(how, "rewrite")
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(FAKE_HERMES))

    def test_indentation_is_adapted_to_the_document(self):
        tabs = '{\n\t"mcp": {\n\t\t"x": 1\n\t}\n}\n'
        wanted = expected_entry(FAKE_HERMES)
        text, _, _ = bridge.merge_named_entry(tabs, "mcp", "hermes", wanted)
        self.assertIn('\t\t"hermes": {', text)
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(FAKE_HERMES))

    def test_non_object_root_is_rejected(self):
        with self.assertRaises(ValueError):
            bridge.merge_named_entry("[1, 2]", "mcp", "hermes", {})


class EntryEquivalenceTests(unittest.TestCase):
    def test_accepts_a_different_but_working_launcher(self):
        desired = expected_entry("C:\\elsewhere\\hermes.exe")
        existing = {
            "type": "local",
            "command": [sys.executable, "mcp", "serve"],
            "enabled": True,
        }
        self.assertTrue(bridge.entry_is_satisfied(existing, desired))

    def test_rejects_a_launcher_that_does_not_exist(self):
        desired = expected_entry(sys.executable)
        existing = {
            "type": "local",
            "command": ["/nope/hermes", "mcp", "serve"],
            "enabled": True,
        }
        self.assertFalse(bridge.entry_is_satisfied(existing, desired))

    def test_rejects_disabled_entries_and_wrong_arguments(self):
        desired = expected_entry(sys.executable)
        disabled = {
            "type": "local",
            "command": [sys.executable, "mcp", "serve"],
            "enabled": False,
        }
        wrong_args = {
            "type": "local",
            "command": [sys.executable, "serve"],
            "enabled": True,
        }
        self.assertFalse(bridge.entry_is_satisfied(disabled, desired))
        self.assertFalse(bridge.entry_is_satisfied(wrong_args, desired))
        self.assertFalse(bridge.entry_is_satisfied(None, desired))
        self.assertFalse(bridge.entry_is_satisfied({"type": "local"}, desired))


# --------------------------------------------------------------------------
# the shipped fragment must stay honest
# --------------------------------------------------------------------------


class FragmentTests(unittest.TestCase):
    def test_fragment_exists_and_renders_to_the_expected_entry(self):
        fragment = bridge.FRAGMENT_PATH
        self.assertTrue(fragment.exists(), f"missing {fragment}")
        rendered = bridge.render_fragment(fragment, {"HERMES_BIN": FAKE_HERMES})
        self.assertEqual(rendered["mcp"]["hermes"], expected_entry(FAKE_HERMES))

    def test_fragment_has_no_hardcoded_user_or_secret(self):
        raw = bridge.FRAGMENT_PATH.read_text(encoding="utf-8")
        self.assertIn("{{HERMES_BIN}}", raw)
        self.assertNotIn("apiKey", raw)


# --------------------------------------------------------------------------
# CLI behaviour (subprocess)
# --------------------------------------------------------------------------


class CheckCliTests(TempCase):
    def test_passes_on_a_well_formed_setup(self):
        self.write_config(
            json.dumps(
                {
                    "provider": {
                        "testprov": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {
                                "baseURL": "https://example.invalid/v1",
                                "apiKey": FAKE_KEY,
                            },
                            "models": {"test-model": {"name": "Test Model"}},
                        }
                    },
                    "mcp": {"hermes": expected_entry(sys.executable)},
                },
                indent=2,
            )
        )
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT: PASS", proc.stdout)
        self.assertIn("testprov/test-model", proc.stdout)

    def test_fails_with_actionable_message_when_config_is_missing(self):
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "gone.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL", proc.stdout)
        self.assertIn("bridge.py setup", proc.stdout)

    def test_fails_when_the_mcp_entry_is_absent(self):
        self.write_config('{\n  "mcp": {\n    "someoneelse": {}\n  }\n}\n')
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("mcp.hermes", proc.stdout)
        self.assertIn("bridge.py setup", proc.stdout)

    def test_fails_when_the_entry_is_disabled(self):
        self.write_config(
            json.dumps(
                {
                    "mcp": {
                        "hermes": {
                            "type": "local",
                            "command": [sys.executable, "mcp", "serve"],
                            "enabled": False,
                        }
                    }
                }
            )
        )
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("enabled", proc.stdout)

    def test_fails_when_the_config_is_corrupt(self):
        self.write_config('{ "mcp": }')
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not valid JSONC", proc.stdout)

    def test_fails_when_a_binary_is_missing_and_says_which_variable_to_set(self):
        self.write_config(GOOD_CONFIG)
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            str(self.tmp / "nope" / "hermes.exe"),
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("HERMES_BIN", proc.stdout)

    def test_fails_when_no_model_can_be_resolved(self):
        entry = expected_entry(sys.executable)
        self.write_config(json.dumps({"mcp": {"hermes": entry}}, indent=2))
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MIMO_BRIDGE_MODEL", proc.stdout)


class SetupCliTests(TempCase):
    def test_running_twice_changes_nothing_the_second_time(self):
        self.write_config(GOOD_CONFIG)
        config = self.tmp / "mimocode.jsonc"
        first = self.run_setup(config, sys.executable)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        after_first = config.read_text(encoding="utf-8")
        backups_after_first = self.backup_names()
        self.assertEqual(len(backups_after_first), 1, backups_after_first)
        self.assertIn("backup", first.stdout)
        self.assertIn(backups_after_first[0], first.stdout)

        second = self.run_setup(config, sys.executable)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("no change", second.stdout)
        self.assertEqual(config.read_text(encoding="utf-8"), after_first)
        self.assertEqual(self.backup_names(), backups_after_first)

    def test_backup_contains_the_pre_change_content(self):
        self.write_config(GOOD_CONFIG)
        config = self.tmp / "mimocode.jsonc"
        proc = self.run_setup(config, sys.executable)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        backup = next(self.tmp.glob("mimocode.jsonc.bak.*"))
        self.assertEqual(backup.read_text(encoding="utf-8"), GOOD_CONFIG)

    def test_merges_the_entry_and_keeps_comments(self):
        self.write_config(GOOD_CONFIG)
        config = self.tmp / "mimocode.jsonc"
        self.run_setup(config, sys.executable)
        text = config.read_text(encoding="utf-8")
        self.assertIn("// keep this comment", text)
        data = bridge.load_jsonc_text(text)
        self.assertEqual(data["mcp"]["hermes"], expected_entry(sys.executable))

    def test_dry_run_writes_nothing(self):
        self.write_config(GOOD_CONFIG)
        config = self.tmp / "mimocode.jsonc"
        before = config.read_text(encoding="utf-8")
        proc = self.run_setup(config, sys.executable, dry_run=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(config.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.tmp.glob("mimocode.jsonc.bak.*")), [])

    def test_reports_missing_hermes_bin_instead_of_crashing(self):
        self.write_config(GOOD_CONFIG)
        proc = run_bridge(
            "setup",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            str(self.tmp / "nope" / "hermes.exe"),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("HERMES_BIN", proc.stdout)
        self.assertEqual(list(self.tmp.glob("mimocode.jsonc.bak.*")), [])

    def test_leaves_a_working_foreign_launcher_untouched(self):
        entry = expected_entry(sys.executable)
        config = json.dumps({"mcp": {"hermes": entry}}, indent=2) + "\n"
        path = self.write_config(config)
        proc = self.run_setup(path, str(ROOT / "bridge.py"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no change", proc.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), config)
        self.assertEqual(list(self.tmp.glob("mimocode.jsonc.bak.*")), [])

    def test_replaces_a_launcher_that_no_longer_exists(self):
        stale = str(self.tmp / "gone" / "hermes.exe")
        config = json.dumps({"mcp": {"hermes": expected_entry(stale)}}, indent=2) + "\n"
        path = self.write_config(config)
        proc = self.run_setup(path, sys.executable)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            bridge.load_jsonc_text(path.read_text(encoding="utf-8"))["mcp"]["hermes"],
            expected_entry(sys.executable),
        )
        self.assertEqual(len(list(self.tmp.glob("mimocode.jsonc.bak.*"))), 1)


class DoctorCliTests(TempCase):
    def test_doctor_writes_nothing(self):
        self.write_config(GOOD_CONFIG)

        def snapshot():
            def fingerprint(path: Path) -> tuple[str, int, int]:
                stat = path.stat()
                relative = path.relative_to(self.tmp).as_posix()
                return relative, stat.st_size, stat.st_mtime_ns

            return sorted(fingerprint(p) for p in self.tmp.rglob("*"))

        before = snapshot()
        proc = run_bridge(
            "doctor",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--hermes-home",
            str(self.tmp / "hermes-home"),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("read-only", proc.stdout)
        self.assertEqual(snapshot(), before)

    def test_doctor_never_prints_the_api_key(self):
        self.write_config(GOOD_CONFIG)
        proc = run_bridge(
            "doctor",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--hermes-home",
            str(self.tmp / "hermes-home"),
        )
        self.assertNotIn(FAKE_KEY, proc.stdout)
        self.assertNotIn(FAKE_KEY, proc.stderr)

    def test_check_never_prints_the_api_key(self):
        self.write_config(GOOD_CONFIG)
        proc = run_bridge(
            "check",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--hermes-bin",
            sys.executable,
            "--mimo-bin",
            sys.executable,
            "--no-mcp-probe",
        )
        self.assertNotIn(FAKE_KEY, proc.stdout)


class AskCliTests(TempCase):
    def test_to_mimo_without_a_binary_explains_what_to_set(self):
        self.write_config(GOOD_CONFIG)
        proc = run_bridge(
            "ask",
            "--to",
            "mimo",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--mimo-bin",
            str(self.tmp / "nope" / "mimo"),
            "ping",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("MIMO_BIN", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_to_mimo_without_a_model_explains_what_to_set(self):
        self.write_config('{\n  "mcp": {}\n}\n')
        proc = run_bridge(
            "ask",
            "--to",
            "mimo",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--mimo-bin",
            sys.executable,
            "ping",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("MIMO_BRIDGE_MODEL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_to_mimo_with_an_unset_credential_env_var_names_it(self):
        self.write_config(
            '{\n  "provider": {\n    "p": {\n'
            '      "options": { "apiKey": "{env:BRIDGE_TEST_MISSING_KEY}" },\n'
            '      "models": { "m": {} }\n    }\n  },\n  "mcp": {}\n}\n'
        )
        proc = run_bridge(
            "ask",
            "--to",
            "mimo",
            "--config",
            str(self.tmp / "mimocode.jsonc"),
            "--mimo-bin",
            sys.executable,
            "ping",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BRIDGE_TEST_MISSING_KEY", proc.stderr)

    def test_to_hermes_without_a_binary_explains_what_to_set(self):
        proc = run_bridge(
            "ask",
            "--to",
            "hermes",
            "--hermes-bin",
            str(self.tmp / "nope" / "hermes.exe"),
            "ping",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("HERMES_BIN", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_empty_message_is_rejected(self):
        proc = run_bridge("ask", "--to", "hermes", "--hermes-bin", sys.executable)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("nothing to send", proc.stderr)


class RestoreCliTests(TempCase):
    """`setup --restore` -- the roll-back half of the backup guarantee."""

    def _config_with_one_backup(self):
        path = self.write_config('{\n  "v": 2\n}\n')
        older = self.tmp / "mimocode.jsonc.bak.20260101_000000"
        older.write_text('{\n  "v": 1\n}\n', encoding="utf-8")
        return path, older

    def test_restores_the_newest_backup_by_default(self):
        path, _ = self._config_with_one_backup()
        proc = run_bridge("setup", "--restore", "--config", str(path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        restored = bridge.load_jsonc_text(path.read_text(encoding="utf-8"))
        self.assertEqual(restored, {"v": 1})
        self.assertIn("restored", proc.stdout)

    def test_accepts_an_explicit_backup_path(self):
        path = self.write_config('{\n  "v": 9\n}\n')
        chosen = self.tmp / "handmade.jsonc"
        chosen.write_text('{\n  "v": 7\n}\n', encoding="utf-8")
        proc = run_bridge("setup", "--restore", str(chosen), "--config", str(path))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        restored = bridge.load_jsonc_text(path.read_text(encoding="utf-8"))
        self.assertEqual(restored, {"v": 7})

    def test_safety_copy_does_not_shadow_the_backups(self):
        """Restoring twice must be stable, not a toggle.

        The restore makes its own safety copy; if that copy were named `*.bak.*`
        the next `--restore` would pick *it* and flip the config back.
        """
        path, _ = self._config_with_one_backup()
        first = run_bridge("setup", "--restore", "--config", str(path))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        restored = path.read_text(encoding="utf-8")

        second = run_bridge("setup", "--restore", "--config", str(path))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("no change", second.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), restored)

    def test_refuses_a_corrupt_backup_instead_of_wrecking_a_good_config(self):
        path = self.write_config('{\n  "v": 2\n}\n')
        bad = self.tmp / "mimocode.jsonc.bak.20260101_000000"
        bad.write_text('{ "v": }\n', encoding="utf-8")
        proc = run_bridge("setup", "--restore", "--config", str(path))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid JSONC", proc.stdout)
        restored = bridge.load_jsonc_text(path.read_text(encoding="utf-8"))
        self.assertEqual(restored, {"v": 2})

    def test_fails_clearly_when_there_is_no_backup(self):
        path = self.write_config('{\n  "v": 2\n}\n')
        proc = run_bridge("setup", "--restore", "--config", str(path))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no backup", proc.stdout)

    def test_fails_clearly_when_the_named_backup_is_missing(self):
        path = self.write_config('{\n  "v": 2\n}\n')
        proc = run_bridge(
            "setup",
            "--restore",
            str(self.tmp / "nope.jsonc"),
            "--config",
            str(path),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("backup not found", proc.stdout)


class AskSessionTests(TempCase):
    """The argv we hand each CLI. Our logic is real; only the OS boundary is stubbed."""

    def setUp(self):
        super().setUp()
        self.calls: list[list[str]] = []
        original = bridge.run_capture

        def fake_run_capture(cmd, **_ignored):
            """Record the argv instead of spawning anything.

            Accepts and ignores `run_capture`'s timeout/cwd: these cases are about
            the command line we build, not about how it runs.
            """
            self.calls.append(list(cmd))
            return 0, "pong", ""

        bridge.run_capture = fake_run_capture
        self.addCleanup(setattr, bridge, "run_capture", original)

    def parse(self, *argv: str):
        return bridge.build_parser().parse_args(["ask", *argv])

    def test_mimo_starts_a_fresh_session_by_default(self):
        config = json.dumps({"provider": {"p": {"models": {"m": {}}}}}, indent=2)
        args = self.parse(
            "--to",
            "mimo",
            "--config",
            str(self.write_config(config)),
            "--mimo-bin",
            sys.executable,
            "ping",
        )
        self.assertEqual(bridge.cmd_ask(args), 0)
        cmd = self.calls[-1]
        self.assertNotIn("--continue", cmd)
        self.assertNotIn("--session", cmd)

    def test_mimo_forwards_an_explicit_session_id(self):
        config = json.dumps({"provider": {"p": {"models": {"m": {}}}}}, indent=2)
        args = self.parse(
            "--to",
            "mimo",
            "--config",
            str(self.write_config(config)),
            "--mimo-bin",
            sys.executable,
            "--session",
            "ses_test",
            "ping",
        )
        self.assertEqual(bridge.cmd_ask(args), 0)
        cmd = self.calls[-1]
        self.assertNotIn("--continue", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "ses_test")

    def test_hermes_maps_session_onto_resume(self):
        args = self.parse(
            "--to",
            "hermes",
            "--hermes-bin",
            sys.executable,
            "--session",
            "ses_test",
            "ping",
        )
        self.assertEqual(bridge.cmd_ask(args), 0)
        cmd = self.calls[-1]
        self.assertEqual(cmd[cmd.index("--resume") + 1], "ses_test")
        self.assertIn("-z", cmd)

    def test_hermes_has_no_session_flag_by_default(self):
        args = self.parse("--to", "hermes", "--hermes-bin", sys.executable, "ping")
        self.assertEqual(bridge.cmd_ask(args), 0)
        self.assertNotIn("--resume", self.calls[-1])

    def test_the_continue_shorthand_is_gone(self):
        """`-c` resumed "the last session", which may be a desktop window's."""
        stderr = contextlib.redirect_stderr(io.StringIO())
        with stderr, self.assertRaises(SystemExit) as ctx:
            self.parse("--to", "mimo", "-c", "ping")
        self.assertEqual(ctx.exception.code, 2)


class RenderCliTests(TempCase):
    def test_render_expands_the_shipped_fragment(self):
        proc = run_bridge("render", "--set", f"HERMES_BIN={FAKE_HERMES}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rendered = json.loads(proc.stdout)
        self.assertEqual(rendered["mcp"]["hermes"], expected_entry(FAKE_HERMES))

    def test_render_reports_an_unknown_placeholder(self):
        fragment = self.tmp / "frag.jsonc"
        fragment.write_text('{ "x": "{{NOPE}}" }\n', encoding="utf-8")
        proc = run_bridge("render", "--fragment", str(fragment))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("NOPE", proc.stderr)

    def test_render_rejects_a_malformed_set(self):
        proc = run_bridge("render", "--set", "JUSTAKEY")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("KEY=value", proc.stderr)


class HelperTests(unittest.TestCase):
    def test_strip_ansi_removes_colour_and_carriage_returns(self):
        cleaned = bridge.strip_ansi("\x1b[0m> build\r\nhello\x1b[0m")
        self.assertEqual(cleaned, "> build\nhello")

    def test_backup_name_is_unique_and_keeps_the_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.jsonc"
            path.write_text("hi", encoding="utf-8")
            first = bridge.backup_file(path)
            second = bridge.backup_file(path)
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("config.jsonc.bak."))
            self.assertEqual(second.read_text(encoding="utf-8"), "hi")

    def test_detect_model_returns_provider_slash_model(self):
        config = {"provider": {"p": {"models": {"m": {}}}}}
        model, _ = bridge.detect_model(Path("cfg"), config)
        self.assertEqual(model, "p/m")

    def test_detect_model_handles_a_config_without_providers(self):
        model, note = bridge.detect_model(Path("cfg"), {})
        self.assertIsNone(model)
        self.assertIn("provider", note)

    def test_missing_credential_hint_reports_an_unset_env_reference(self):
        config = {"provider": {"p": {"options": {"apiKey": "{env:NOPE_NOT_SET}"}}}}
        hint = bridge.missing_credential_hint(config)
        self.assertIsNotNone(hint)
        self.assertIn("NOPE_NOT_SET", hint)

    def test_resolution_prefers_explicit_flag_then_env(self):
        # relative paths are passed through untouched (MSYS translation only
        # kicks in for absolute POSIX-looking paths that do not exist)
        resolved = bridge.resolve_mimo_config("explicit.jsonc")
        self.assertEqual(str(resolved), "explicit.jsonc")
        self.assertEqual(str(bridge.resolve_hermes_home("h")), "h")

    def test_relative_paths_are_never_rewritten(self):
        rewritten = bridge.to_native_path("sub/dir/config.jsonc").as_posix()
        self.assertEqual(rewritten, "sub/dir/config.jsonc")

    def test_posix_path_untouched_when_not_under_msys(self):
        with unittest.mock.patch.dict(os.environ, {"MSYSTEM": ""}, clear=False):
            self.assertEqual(
                str(bridge.to_native_path("/tmp/does-not-exist.jsonc")),
                str(Path("/tmp/does-not-exist.jsonc")),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
