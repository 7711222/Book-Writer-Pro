"""Hardening tests for the subscription-CLI backends and their configuration:

  * subprocess timeouts (BOOKWRITER_CLI_TIMEOUT) in the default runners
  * argv prompt-length limits and the {promptfile} placeholder (llm_cli)
  * quote-stripping for Windows CLI command overrides (provider.cli_command)
  * every UI-catalog model id is priced (the cost meter must not report $0)
  * settings.json is restricted to the current user on save (runtime_config)

Offline except two tiny real subprocess runs against sys.executable.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bookwriter import llm_claude_cli, llm_cli, provider
from bookwriter import runtime_config as rcmod
from bookwriter.config import MODEL_PRICES, StageModel
from bookwriter.costs import CostLedger, Usage
from bookwriter.llm_cli import GenericCliLLM


class _EnvGuard(unittest.TestCase):
    """Save/restore the env vars these tests poke at."""

    _VARS = [
        "BOOKWRITER_CLI_TIMEOUT", "BOOKWRITER_CODEX_CMD",
        "BOOKWRITER_GROK_CMD", "BOOKWRITER_CLI_CMD",
    ]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        self._saved_overrides = {k: rcmod._overrides.get(k) for k in self._VARS}
        for k in self._VARS:
            os.environ.pop(k, None)
            rcmod._overrides.pop(k, None)  # runtime overrides shadow os.environ

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved_overrides.items():
            if v is None:
                rcmod._overrides.pop(k, None)
            else:
                rcmod._overrides[k] = v


def _model():
    return StageModel("claude-opus-4-8", effort="medium")


# --------------------------------------------------------------------------- #
# Finding: CLI subprocess runners need a timeout (hang-proofing)
# --------------------------------------------------------------------------- #

class TestCliTimeout(_EnvGuard):
    def test_default_timeout_is_600(self):
        self.assertEqual(llm_claude_cli._cli_timeout(), 600.0)

    def test_env_override_and_garbage_fallback(self):
        os.environ["BOOKWRITER_CLI_TIMEOUT"] = "30"
        self.assertEqual(llm_claude_cli._cli_timeout(), 30.0)
        os.environ["BOOKWRITER_CLI_TIMEOUT"] = "not-a-number"
        self.assertEqual(llm_claude_cli._cli_timeout(), 600.0)

    def test_both_backends_share_the_timeout_protected_runner(self):
        # llm_cli must not regress to an un-timed subprocess.run of its own.
        self.assertIs(llm_cli._subprocess_runner, llm_claude_cli._subprocess_runner)

    def test_timeout_expired_becomes_actionable_error(self):
        exc = subprocess.TimeoutExpired(cmd=["grok", "--prompt"], timeout=600)
        with mock.patch.object(llm_claude_cli.subprocess, "run", side_effect=exc) as run:
            with self.assertRaises(RuntimeError) as ctx:
                llm_claude_cli._subprocess_runner(["grok", "--prompt", "hi"], "")
        msg = str(ctx.exception)
        self.assertIn("grok", msg)
        self.assertIn("BOOKWRITER_CLI_TIMEOUT", msg)
        # The timeout was actually passed to subprocess.run.
        self.assertEqual(run.call_args.kwargs.get("timeout"), 600.0)

    def test_real_quick_run_still_works(self):
        code, out, err = llm_claude_cli._subprocess_runner(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], "hi")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "HI")

    def test_real_hung_cli_is_killed(self):
        os.environ["BOOKWRITER_CLI_TIMEOUT"] = "1"
        with self.assertRaises(RuntimeError) as ctx:
            llm_claude_cli._subprocess_runner(
                [sys.executable, "-c", "import time; time.sleep(30)"], "")
        self.assertIn("BOOKWRITER_CLI_TIMEOUT", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Finding: argv prompts overflow the Windows command line — {promptfile} + cap
# --------------------------------------------------------------------------- #

class TestPromptFileAndArgvLimit(unittest.TestCase):
    def _capture_runner(self, record):
        def run(args, stdin):
            record["args"] = args
            record["stdin"] = stdin
            for a in args:
                if a.endswith(".txt") and os.path.exists(a):
                    record["path"] = a
                    with open(a, encoding="utf-8") as f:
                        record["filecontent"] = f.read()
            return 0, "ok", ""
        return run

    def test_promptfile_placeholder_feeds_prompt_via_temp_file(self):
        record = {}
        llm = GenericCliLLM(command=["mycli", "--prompt-file", "{promptfile}"],
                            provider="cli", runner=self._capture_runner(record))
        out = llm.complete_text(stage="write", model=_model(), system="sys",
                                user="write ch1", max_tokens=10, ledger=CostLedger())
        self.assertEqual(out, "ok")
        # Placeholder replaced with a real path; nothing left on stdin/argv.
        self.assertNotIn("{promptfile}", record["args"])
        self.assertEqual(record["stdin"], "")
        self.assertIn("write ch1", record["filecontent"])
        self.assertIn("sys", record["filecontent"])
        # Temp file cleaned up after the run.
        self.assertFalse(os.path.exists(record["path"]))

    def test_promptfile_cleaned_up_even_when_cli_fails(self):
        record = {}

        def failing(args, stdin):
            self._capture_runner(record)(args, stdin)
            return 1, "", "boom"

        llm = GenericCliLLM(command=["mycli", "{promptfile}"], provider="cli",
                            runner=failing)
        with self.assertRaises(RuntimeError):
            llm.complete_text(stage="write", model=_model(), system="s",
                              user="u", max_tokens=10, ledger=CostLedger())
        self.assertFalse(os.path.exists(record["path"]))

    def test_oversized_argv_prompt_raises_actionable_error(self):
        record = {}
        llm = GenericCliLLM(command=["grok", "--prompt"], provider="grok-cli",
                            stdin=False, runner=self._capture_runner(record))
        with mock.patch.object(llm_cli, "_ARGV_PROMPT_LIMIT", 50):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete_text(stage="write", model=_model(), system="s" * 60,
                                  user="u", max_tokens=10, ledger=CostLedger())
        msg = str(ctx.exception)
        self.assertIn("BOOKWRITER_GROK_CMD", msg)
        self.assertIn("{promptfile}", msg)
        self.assertEqual(record, {})  # the CLI was never launched

    def test_small_argv_prompt_still_passed_as_argument(self):
        record = {}
        llm = GenericCliLLM(command=["grok", "--prompt"], provider="grok-cli",
                            stdin=False, runner=self._capture_runner(record))
        llm.complete_text(stage="write", model=_model(), system="s",
                          user="short", max_tokens=10, ledger=CostLedger())
        self.assertIn("short", record["args"][-1])
        self.assertEqual(record["stdin"], "")


# --------------------------------------------------------------------------- #
# Finding: quoted Windows paths in BOOKWRITER_*_CMD kept their literal quotes
# --------------------------------------------------------------------------- #

class TestCliCommandQuoting(_EnvGuard):
    def test_strip_outer_quotes_helper(self):
        f = provider._strip_outer_quotes
        self.assertEqual(f('"a b"'), "a b")
        self.assertEqual(f("'a b'"), "a b")
        self.assertEqual(f("plain"), "plain")
        self.assertEqual(f('"unbalanced'), '"unbalanced')
        self.assertEqual(f("\"mismatch'"), "\"mismatch'")
        self.assertEqual(f('"'), '"')
        self.assertEqual(f(""), "")

    def test_quoted_space_path_resolves_to_clean_argv(self):
        os.environ["BOOKWRITER_GROK_CMD"] = '"C:\\Program Files\\Grok\\grok.exe" --prompt'
        cmd = provider.cli_command("grok-cli")
        self.assertEqual(cmd, ["C:\\Program Files\\Grok\\grok.exe", "--prompt"])
        # No token retains a literal quote character (shutil.which would fail).
        self.assertFalse(any('"' in t for t in cmd))

    def test_cli_cmd_env_mapping(self):
        self.assertEqual(provider.cli_cmd_env("grok-cli"), "BOOKWRITER_GROK_CMD")
        self.assertEqual(provider.cli_cmd_env("codex"), "BOOKWRITER_CODEX_CMD")
        self.assertEqual(provider.cli_cmd_env("cli"), "BOOKWRITER_CLI_CMD")
        self.assertEqual(provider.cli_cmd_env("something-else"), "BOOKWRITER_CLI_CMD")


# --------------------------------------------------------------------------- #
# Finding: unpriced catalog models silently metered at $0 / retired model row
# --------------------------------------------------------------------------- #

class TestCatalogPricing(unittest.TestCase):
    def test_every_api_catalog_model_is_priced(self):
        for pid in ("anthropic", "openai", "openrouter", "grok"):
            for mid, label in provider.MODEL_CATALOG[pid]:
                price = MODEL_PRICES.get(mid)
                self.assertIsNotNone(price, f"{pid}: {mid} ({label}) has no price")
                self.assertGreater(price.input, 0.0, mid)
                self.assertGreater(price.output, 0.0, mid)

    def test_retired_claude_37_replaced_with_current_model(self):
        ids = [m for m, _ in provider.MODEL_CATALOG["openrouter"]]
        self.assertNotIn("anthropic/claude-3.7-sonnet", ids)
        self.assertIn("anthropic/claude-sonnet-4.6", ids)

    def test_openrouter_grok4_usage_costs_real_money(self):
        u = Usage(model="x-ai/grok-4", stage="write",
                  input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(u.cost(), 18.0, places=6)


# --------------------------------------------------------------------------- #
# Finding: settings.json (plaintext keys) left world-readable on Windows
# --------------------------------------------------------------------------- #

class TestSettingsFilePermissions(unittest.TestCase):
    def setUp(self):
        self._old_path = rcmod._path
        self._old_overrides = dict(rcmod._overrides)
        rcmod._overrides.clear()

    def tearDown(self):
        rcmod._path = self._old_path
        rcmod._overrides.clear()
        rcmod._overrides.update(self._old_overrides)

    def test_save_round_trip_survives_permission_tightening(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            rcmod.bind_file(path)
            rcmod.set_values({"BOOKWRITER_CLI_TIMEOUT": "120"})
            self.assertTrue(os.path.exists(path))
            # A fresh load still reads the value back (no ACL lockout).
            rcmod._overrides.clear()
            rcmod.load()
            self.assertEqual(rcmod.getenv("BOOKWRITER_CLI_TIMEOUT"), "120")
            # Saving again over the restricted file also works.
            rcmod.set_values({"BOOKWRITER_CLI_TIMEOUT": "240"})
            rcmod._overrides.clear()
            rcmod.load()
            self.assertEqual(rcmod.getenv("BOOKWRITER_CLI_TIMEOUT"), "240")

    @unittest.skipUnless(os.name == "nt", "Windows ACL path")
    def test_windows_save_tightens_acl_with_icacls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            rcmod.bind_file(path)
            old_sid = rcmod._win_user_sid
            rcmod._win_user_sid = "S-1-5-21-0-0-0-1000"  # avoid a live whoami
            try:
                with mock.patch.object(rcmod.subprocess, "run") as run:
                    rcmod.set_values({"BOOKWRITER_CLI_TIMEOUT": "120"})
            finally:
                rcmod._win_user_sid = old_sid
            icacls_calls = [c.args[0] for c in run.call_args_list
                            if c.args and c.args[0] and c.args[0][0] == "icacls"]
            self.assertTrue(icacls_calls, "icacls was not invoked on save()")
            # Inheritance stripped; access granted only to the user's SID.
            final = [argv for argv in icacls_calls if argv[1] == path]
            self.assertTrue(final, f"no icacls call for {path}: {icacls_calls}")
            self.assertIn("/inheritance:r", final[0])
            self.assertIn("/grant:r", final[0])
            self.assertIn("*S-1-5-21-0-0-0-1000:F", final[0])

    @unittest.skipUnless(os.name != "nt", "POSIX chmod path")
    def test_posix_save_chmods_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            rcmod.bind_file(path)
            rcmod.set_values({"BOOKWRITER_CLI_TIMEOUT": "120"})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_cli_timeout_is_a_settings_panel_option(self):
        self.assertIn("BOOKWRITER_CLI_TIMEOUT", rcmod.OPTION_KEYS)
        self.assertIn("BOOKWRITER_IMAGE_ALLOW_PRIVATE", rcmod.OPTION_KEYS)


if __name__ == "__main__":
    unittest.main()
