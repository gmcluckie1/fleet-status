"""Config file, pluggable sources, credentials and the command line. No accounts needed."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (CONFIG, ENV, NOW, OAUTH_TOKEN, OPENAI_KEY, RULES, ROOT, SCHEMA, SECRETS, FakeHttp, FakeRunner, build, codex_log,  # noqa: E402
                     fake_grok, fc, make_config, no_rpc, pool, src)

EXAMPLE = ROOT / "fleet-status.example.json"


def write_config(directory, config):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(config))
    return str(path)


def run_main(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    kw.setdefault("runner", FakeRunner())
    kw.setdefault("now", NOW)
    kw.setdefault("env", dict(ENV))
    kw.setdefault("codex_rpc", no_rpc)
    kw.setdefault("grok_rpc", fake_grok)
    kw.setdefault("http", FakeHttp())
    with contextlib.redirect_stderr(err):
        code = fc.main(argv, stdout=out, **kw)
    return code, out.getvalue(), err.getvalue()


class LoadConfigTests(unittest.TestCase):
    def load(self, text_or_obj):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "c.json"
            path.write_text(text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj))
            return fc.load_config(path)

    def test_the_example_config_loads_and_names_only_known_sources(self):
        config = fc.load_config(EXAMPLE)
        self.assertEqual(config["collector_id"], "my-machine")
        raw = json.loads(EXAMPLE.read_text())
        self.assertEqual({k for k in raw["sources"] if not k.startswith("_")}, set(fc.SOURCE_IDS) - set())
        self.assertFalse(config["sources"]["claude_cloud"]["enabled"])
        self.assertNotIn("claude_cloud", [sid for sid, _ in fc.enabled_sources(config)])

    def test_the_example_uses_placeholder_labels_and_no_real_looking_paths_or_addresses(self):
        text = EXAMPLE.read_text()
        self.assertFalse(re.search(r"/Users/|/home/[A-Za-z]", text))
        for address in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text):
            self.assertTrue(address.endswith("@example.com"), address)

    def test_defaults(self):
        config = self.load({"sources": {"muse": {}}})
        self.assertEqual((config["collector_id"], config["state_dir"], config["pool_notes"]), ("fleet-status", None, {}))

    def test_underscore_keys_are_comments(self):
        config = self.load({"_note": "x", "sources": {"_note": "y", "muse": {"_why": "z"}}})
        self.assertEqual(list(config["sources"]), ["muse"])

    def test_structural_mistakes_fail_closed(self):
        bad = ({"nope": 1, "sources": {}}, {"sources": {"musse": {}}}, {"sources": {"muse": []}}, {"sources": []},
               {"sources": {"muse": {"enabled": "false"}}}, {"collector_id": "Upper Case", "sources": {}},
               {"collector_id": "x" * 41, "sources": {}}, {"state_dir": "", "sources": {}}, {"state_dir": 5, "sources": {}},
               {"pool_notes": {"a": 1}, "sources": {}}, {"pool_notes": [], "sources": {}}, {}, [], "text", 5)
        for config in bad:
            with self.subTest(config=config), self.assertRaises(fc.ConfigError):
                self.load(config)
        for text in ("{not json", ""):
            with self.assertRaises(fc.ConfigError):
                self.load(text)
        with self.assertRaises(fc.ConfigError):
            fc.load_config("/no/such/config.json")

    def test_error_messages_never_echo_config_values(self):
        secret = "Hunter2-" + "X" * 12
        for config in ({"sources": {"muse": {"command": [secret]}}, "collector_id": secret},
                       {"sources": {"muse": {"enabled": secret}}}, {"pool_notes": {"a": secret}, "sources": {"muse": []}}):
            with self.assertRaises(fc.ConfigError) as cm:
                self.load(config)
            self.assertNotIn(secret, str(cm.exception))
        with tempfile.TemporaryDirectory() as t:
            Path(t, "c.json").write_text("{" + secret)
            with self.assertRaises(fc.ConfigError) as cm:
                fc.load_config(Path(t) / "c.json")
        self.assertNotIn(secret, str(cm.exception))


class PluggableSourceTests(unittest.TestCase):
    def test_only_the_listed_sources_run(self):
        runner = FakeRunner()
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, runner, only=("muse",), http=http)
        self.assertEqual([s["id"] for s in snap["sources"]], ["muse"])
        self.assertEqual([c[0] for c in runner.calls], ["muse"])
        self.assertEqual(http.calls, [])
        self.assertEqual([p["id"] for p in snap["pools"]], ["muse"])

    def test_enabled_false_skips_a_source_entirely(self):
        runner = FakeRunner()
        cfg = make_config(("muse", "grok"), grok={"enabled": False})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, runner, config=cfg)
        self.assertEqual([s["id"] for s in snap["sources"]], ["muse"])  # not a failure: simply absent
        self.assertIsNone(pool(snap, "grok"))

    def test_sources_report_in_a_fixed_order_whatever_the_config_order(self):
        cfg = {"collector_id": "x", "state_dir": None, "pool_notes": {},
               "sources": {"grok": {}, "muse": {}, "codex": {}}}
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, config=cfg)
        self.assertEqual([s["id"] for s in snap["sources"]], ["muse", "codex", "grok"])

    def test_every_source_degrades_to_unknown_instead_of_failing(self):
        """Nothing is installed, no credential is set and every network call fails: the feed is still valid."""
        class Dead(FakeRunner):
            def __call__(self, argv, timeout, cwd=None):
                raise fc.CmdError("unavailable")

        def dead_http(*args):
            raise fc.CmdError("unavailable")

        def dead_grok(argv, timeout):
            raise fc.CmdError("unavailable")
        config = make_config()
        config["sources"]["anthropic_ledger"] = {"monthly_credit_usd": 200}
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "sessions").mkdir()
            snap = build(t, Dead(), config=config, http=dead_http, grok_rpc=dead_grok, env={})
        self.assertEqual(fc.validate(snap, SCHEMA), [])
        self.assertEqual([s["id"] for s in snap["sources"]], [sid for sid in fc.SOURCE_IDS])
        self.assertTrue(snap["pools"])
        self.assertEqual({p["state"] for p in snap["pools"]}, {"unknown"})
        for p in snap["pools"]:
            self.assertEqual((p["used"], p["limit"], p["remaining"]), (None, None, None), p["id"])
            self.assertTrue(p["advice"].startswith("Unavailable: "), p["id"])
        for sid in fc.SOURCE_IDS:
            if sid != "api_probe":  # the probe has no pool of its own: it only refines anthropic-api
                self.assertTrue(any(p["source_id"] == sid or p["id"].startswith(sid.split("_")[0]) for p in snap["pools"]), sid)
        self.assertEqual({p["id"] for p in snap["pools"]} >= {"muse", "codex", "grok", "claude-subscription", "github-actions",
                                                              "agy-account-a-unavailable", "colab-account-b-units"}, True)

    def test_every_failure_code_maps_into_the_schema_enum_and_leaves_a_valid_feed(self):
        enum = SCHEMA["properties"]["sources"]["items"]["properties"]["error_code"]["oneOf"][0]["enum"]
        for code in ("timeout", "unavailable", "auth", "parse", "not_configured", "rate_limited", "something-else"):
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, FakeRunner(fail={"muse": code, "claude": code, "billing": code}), only=("muse", "claude_sub", "github_billing"))
            self.assertEqual(fc.validate(snap, SCHEMA), [], code)
            for s in snap["sources"]:
                self.assertTrue(s["error_code"] is None or s["error_code"] in enum, (code, s))
            self.assertFalse(src(snap, "muse")["ok"])
            self.assertEqual(pool(snap, "muse")["state"], "unknown")

    def test_one_failed_source_leaves_the_others_untouched(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(fail={"muse": "timeout"}), only=("muse", "agy"))
        self.assertEqual((src(snap, "muse")["ok"], src(snap, "muse")["error_code"], src(snap, "muse")["observed_at"]),
                         (False, "timeout", None))
        self.assertEqual(pool(snap, "muse")["state"], "unknown")
        self.assertEqual(pool(snap, "agy-account-a-gemini-models")["state"], "healthy")

    def test_a_missing_command_is_not_configured_and_is_not_retried(self):
        calls = []

        def missing(argv, timeout, cwd=None):
            calls.append(argv)
            raise fc.CmdError("not_configured")
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, missing, only=("muse",))
        self.assertEqual(src(snap, "muse")["error_code"], "not_configured")
        self.assertEqual(len(calls), 1)

    def test_invalid_command_settings_are_not_configured_and_run_nothing(self):
        for bad in ("muse usage --json", [], [""], [1], None):
            runner = FakeRunner()
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, runner, config=make_config(("muse",), muse={"command": bad}))
            self.assertEqual(src(snap, "muse")["error_code"], "not_configured", bad)
            self.assertEqual(runner.calls, [], bad)

    def test_bad_account_lists_are_not_configured(self):
        bad_lists = ([], "a", [{"label": "UPPER"}], [{"label": "a"}, {"label": "a"}], [{"nolabel": 1}], ["a"], [{"label": "x" * 21}])
        for sid in ("agy", "colab"):
            for accounts in bad_lists:
                runner = FakeRunner()
                with tempfile.TemporaryDirectory() as t:
                    snap = build(t, runner, config=make_config((sid,), **{sid: {"accounts": accounts}}))
                self.assertEqual(src(snap, sid)["error_code"], "not_configured", (sid, accounts))
                self.assertEqual(runner.calls, [])
                self.assertTrue(all(p["state"] == "unknown" for p in snap["pools"]))
                self.assertEqual(fc.validate(snap, SCHEMA), [])

    def test_source_timeouts(self):
        base = fc.source_timeouts(make_config())
        self.assertEqual(base["agy"], 5 + 15 * 2)
        self.assertEqual(base["colab"], 5 + 30 * 2)
        self.assertEqual(base["muse"], fc.SOURCE_TIMEOUTS["muse"])
        one = fc.source_timeouts(make_config(agy={"accounts": [{"label": "x"}]}))
        self.assertEqual(one["agy"], 20)
        custom = fc.source_timeouts(make_config(muse={"timeout_seconds": 99}))
        self.assertEqual(custom["muse"], 99.0)
        for bad in (0, -1, 301, "9", True, None):
            self.assertEqual(fc.source_timeouts(make_config(muse={"timeout_seconds": bad}))["muse"], fc.SOURCE_TIMEOUTS["muse"], bad)
        self.assertEqual(set(base), set(fc.SOURCE_IDS))

    def test_every_source_has_a_default_timeout(self):
        for sid in fc.SOURCE_IDS:
            self.assertIn(sid, fc.source_timeouts({"sources": {}}))


class CredentialTests(unittest.TestCase):
    def test_default_environment_variable_per_source(self):
        self.assertEqual(fc.CREDENTIAL_ENV, {"claude_cloud": "FLEET_STATUS_CLAUDE_OAUTH_TOKEN", "anthropic_costs": "FLEET_STATUS_ANTHROPIC_ADMIN_KEY",
                                             "openai_costs": "FLEET_STATUS_OPENAI_ADMIN_KEY", "api_probe": "FLEET_STATUS_ANTHROPIC_API_KEY"})

    def test_the_documented_environment_variables_appear_in_the_readme(self):
        readme = (ROOT / "README.md").read_text()
        for name in fc.CREDENTIAL_ENV.values():
            self.assertIn(name, readme)

    def ctx(self, runner=None, env=None):
        return fc.Ctx(NOW, RULES, CONFIG, runner or FakeRunner(), {} if env is None else env)

    def test_environment_variable_whitespace_is_trimmed(self):
        ctx = self.ctx(env={"FLEET_STATUS_OPENAI_ADMIN_KEY": "  value\n"})
        self.assertEqual(fc.read_credential(ctx, "openai_costs", {}), "value")

    def test_credential_env_renames_the_variable(self):
        ctx = self.ctx(env={"MY_KEY": "abc", "FLEET_STATUS_OPENAI_ADMIN_KEY": "default"})
        self.assertEqual(fc.read_credential(ctx, "openai_costs", {"credential": {"env": "MY_KEY"}}), "abc")

    def test_a_command_wins_over_the_environment_and_its_output_is_trimmed(self):
        seen = []

        def runner(argv, timeout, cwd=None):
            seen.append((argv, timeout))
            return "  from-command \n"
        ctx = self.ctx(runner, {"FLEET_STATUS_OPENAI_ADMIN_KEY": "from-env"})
        cfg = {"credential": {"command": ["~/bin/get-secret", "name"]}}
        self.assertEqual(fc.read_credential(ctx, "openai_costs", cfg), "from-command")
        self.assertEqual(seen[0][0][1:], ["name"])
        self.assertEqual(seen[0][0][0], os.path.expanduser("~/bin/get-secret"))

    def test_missing_empty_or_failing_credentials_are_not_configured(self):
        def failing(argv, timeout, cwd=None):
            raise fc.CmdError("unavailable")

        def empty(argv, timeout, cwd=None):
            return "  \n"
        cases = ((self.ctx(), {}), (self.ctx(env={"FLEET_STATUS_OPENAI_ADMIN_KEY": ""}), {}),
                 (self.ctx(failing), {"credential": {"command": ["x"]}}), (self.ctx(empty), {"credential": {"command": ["x"]}}),
                 (self.ctx(), {"credential": {"command": "x"}}), (self.ctx(), {"credential": {"command": []}}),
                 (self.ctx(), {"credential": "x"}), (self.ctx(), {"credential": {"env": "bad name"}}),
                 (self.ctx(), {"credential": {"env": 5}}))
        for ctx, cfg in cases:
            with self.assertRaises(fc.CmdError) as cm:
                fc.read_credential(ctx, "openai_costs", cfg)
            self.assertEqual(cm.exception.code, "not_configured", cfg)

    def test_a_credential_command_timeout_is_a_timeout(self):
        def slow(argv, timeout, cwd=None):
            raise fc.CmdError("timeout")
        with self.assertRaises(fc.CmdError) as cm:
            fc.read_credential(self.ctx(slow), "openai_costs", {"credential": {"command": ["x"]}})
        self.assertEqual(cm.exception.code, "timeout")

    def test_a_secret_never_becomes_part_of_an_argv(self):
        class Spy(FakeRunner):
            def __call__(self, argv, timeout, cwd=None):
                if argv[0] == "get-secret":
                    return ENV["FLEET_STATUS_ANTHROPIC_ADMIN_KEY"]
                return super().__call__(argv, timeout, cwd)
        runner, http = Spy(), FakeHttp()
        cfg = make_config(("anthropic_costs", "openai_costs", "claude_cloud", "api_probe"),
                          anthropic_costs={"credential": {"command": ["get-secret", "anthropic"]}})
        with tempfile.TemporaryDirectory() as t:
            build(t, runner, config=cfg, http=http)
        self.assertTrue(any("x-api-key" in c[2] for c in http.calls))  # the secret really was used
        for argv in runner.calls:
            for secret in SECRETS:
                self.assertFalse(any(secret in part for part in argv), argv)

    def test_secrets_reach_the_http_layer_and_nothing_else(self):
        """Used in-process only: not on stdout or stderr, not in the output file, the state files or the log."""
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            sessions = Path(t) / "sessions"
            codex_log(sessions)
            config = make_config()
            config["sources"]["codex"]["sessions_dir"] = str(sessions)
            path = write_config(t, config)
            state, out_file = Path(t) / "state", Path(t) / "feed.json"
            code, out, err = run_main(["--config", path, "--state-dir", str(state), "--output", str(out_file)], http=http)
            self.assertEqual(code, 0)
            self.assertTrue(any(OAUTH_TOKEN in str(c[2]) for c in http.calls), "the token must actually be used")
            blobs = [out, err, out_file.read_text()] + [p.read_text() for p in state.iterdir()]
            self.assertTrue((state / fc.USAGE_STATE_FILE).exists())
            for blob in blobs:
                for secret in SECRETS:
                    self.assertNotIn(secret, blob)
            for private in (state / fc.USAGE_STATE_FILE, state / "collector.log", out_file):
                self.assertEqual(oct(private.stat().st_mode & 0o777), "0o600", private.name)

    def test_commands_the_collector_starts_do_not_inherit_the_default_credential_variables(self):
        probe = [sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.startswith('FLEET_STATUS_')))"]
        with patch.dict(os.environ, {"FLEET_STATUS_OPENAI_ADMIN_KEY": OPENAI_KEY, "FLEET_STATUS_CONFIG": "x", "KEEP_ME": "1"}):
            self.assertEqual(fc.run_cmd(probe, 10).strip(), "[]")
            self.assertEqual(fc.child_env(("KEEP_ME",)).get("KEEP_ME"), None)
            self.assertEqual(fc.child_env().get("KEEP_ME"), "1")
            self.assertNotIn("FLEET_STATUS_OPENAI_ADMIN_KEY", fc.child_env())

    def test_no_credential_means_no_request(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("claude_cloud", "anthropic_costs", "openai_costs", "api_probe"), http=http, env={})
        self.assertEqual(http.calls, [])
        self.assertEqual({s["error_code"] for s in snap["sources"] if not s["ok"]}, {"not_configured"})


class CommandLineTests(unittest.TestCase):
    def config_file(self, t, **kw):
        sessions = Path(t) / "sessions"
        if not sessions.exists():
            codex_log(sessions)
        config = make_config(("muse", "codex"), codex={"sessions_dir": str(sessions)}, **kw)
        return write_config(t, config)

    def test_no_config_is_a_usage_error(self):
        code, out, err = run_main([], env={})
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--config", err)

    def test_the_config_can_come_from_the_environment(self):
        with tempfile.TemporaryDirectory() as t:
            path = self.config_file(t)
            code, out, err = run_main(["--state-dir", str(Path(t) / "s")], env={"FLEET_STATUS_CONFIG": path})
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["schema"], "fleet-status/v1")

    def test_a_bad_or_empty_config_is_a_usage_error_with_nothing_printed(self):
        with tempfile.TemporaryDirectory() as t:
            for config in ({"sources": {"nope": {}}}, {"sources": {}}, {"sources": {"muse": {"enabled": False}}}):
                code, out, err = run_main(["--config", write_config(t, config), "--state-dir", str(Path(t) / "s")])
                self.assertEqual((code, out), (2, ""), config)
                self.assertTrue(err.startswith("error: "), err)
            code, out, err = run_main(["--config", str(Path(t) / "missing.json")])
            self.assertEqual((code, out), (2, ""))

    def test_prints_one_valid_json_document_to_stdout(self):
        with tempfile.TemporaryDirectory() as t:
            code, out, err = run_main(["--config", self.config_file(t), "--state-dir", str(Path(t) / "s")])
        self.assertEqual((code, err), (0, ""))
        snap = json.loads(out)
        self.assertEqual(fc.validate(snap, SCHEMA), [])
        self.assertEqual(snap["collector_id"], "test-collector")
        self.assertNotIn("\n ", out)  # compact by default
        self.assertTrue(out.endswith("\n") and out.count("\n") == 1)

    def test_pretty_and_output_file(self):
        with tempfile.TemporaryDirectory() as t:
            path = self.config_file(t)
            code, out, err = run_main(["--config", path, "--state-dir", str(Path(t) / "s"), "--pretty"])
            self.assertIn('\n  "asks"', out)
            target = Path(t) / "feed.json"
            code, out, err = run_main(["--config", path, "--state-dir", str(Path(t) / "s"), "--output", str(target)])
            self.assertEqual((code, out), (0, ""))
            self.assertEqual(fc.validate(json.loads(target.read_text()), SCHEMA), [])
            self.assertEqual([p.name for p in Path(t).iterdir() if p.name.startswith(".")], [])  # no temp file left behind

    def test_state_dir_comes_from_the_flag_then_the_config(self):
        with tempfile.TemporaryDirectory() as t:
            from_config = Path(t) / "from-config"
            config = write_config(t, {**make_config(("muse",)), "state_dir": str(from_config)})
            run_main(["--config", config])
            self.assertTrue((from_config / "collector.log").exists())
            flag = Path(t) / "flag"
            run_main(["--config", config, "--state-dir", str(flag)])
            self.assertTrue((flag / "collector.log").exists())
            self.assertEqual(oct(flag.stat().st_mode & 0o777), "0o700")

    def test_an_existing_state_dir_keeps_its_permissions_and_a_new_one_is_private(self):
        with tempfile.TemporaryDirectory() as t:
            existing = Path(t) / "existing"
            existing.mkdir(mode=0o755)
            existing.chmod(0o755)
            run_main(["--config", self.config_file(t), "--state-dir", str(existing)])
            self.assertEqual(oct(existing.stat().st_mode & 0o777), "0o755")
            fresh = Path(t) / "a" / "b" / "fresh"
            run_main(["--config", self.config_file(t), "--state-dir", str(fresh)])
            self.assertEqual(oct(fresh.stat().st_mode & 0o777), "0o700")

    def test_unwritable_locations_are_usage_errors_not_tracebacks(self):
        with tempfile.TemporaryDirectory() as t:
            blocker = Path(t) / "file"
            blocker.write_text("x")
            code, out, err = run_main(["--config", self.config_file(t), "--state-dir", str(blocker / "state")])
            self.assertEqual((code, out), (2, ""))
            code, out, err = run_main(["--config", self.config_file(t), "--state-dir", str(Path(t) / "s"),
                                       "--output", str(blocker / "feed.json")])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("cannot write", err)

    def test_the_output_directory_is_created(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "new" / "dir" / "feed.json"
            code, out, err = run_main(["--config", self.config_file(t), "--state-dir", str(Path(t) / "s"), "--output", str(target)])
            self.assertEqual(code, 0, err)
            self.assertEqual(fc.validate(json.loads(target.read_text()), SCHEMA), [])

    def test_the_log_has_only_codes_and_counts(self):
        runner = FakeRunner(fail={"muse": "timeout"})
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "s"
            run_main(["--config", self.config_file(t), "--state-dir", str(state)], runner=runner)
            log = (state / "collector.log").read_text()
        self.assertIn("source failed id=muse code=timeout", log)
        self.assertRegex(log, r"snapshot sources=2 sources_failed=1 pools=3 asks=0")
        for line in log.splitlines():
            self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (source failed id=\w+ code=\w+|snapshot [\w= ]+)$")

    def test_a_secret_looking_note_refuses_the_whole_snapshot(self):
        config = make_config(("muse",))
        config["pool_notes"] = {"muse": "see /Users/someone/notes"}
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "feed.json"
            code, out, err = run_main(["--config", write_config(t, config), "--state-dir", str(Path(t) / "s"), "--output", str(target)])
            self.assertEqual((code, out), (3, ""))
            self.assertFalse(target.exists())
            self.assertNotIn("someone", err)
            self.assertIn("refused reason=secret", (Path(t) / "s" / "collector.log").read_text())

    def test_it_refuses_when_the_schema_uses_an_unsupported_keyword(self):
        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "s.json"
            bad.write_text(json.dumps({"type": "object", "allOf": []}))
            code, out, err = run_main(["--config", self.config_file(t), "--state-dir", str(Path(t) / "s"), "--schema", str(bad)])
        self.assertEqual((code, out), (3, ""))

    def test_a_snapshot_that_fails_the_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            strict = Path(t) / "s.json"
            strict.write_text(json.dumps({**SCHEMA, "properties": {**SCHEMA["properties"], "pools": {"type": "array", "maxItems": 0}}}))
            code, out, err = run_main(["--config", self.config_file(t), "--state-dir", str(Path(t) / "s"), "--schema", str(strict)])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("schema", err)

    def test_unreadable_rules_are_a_usage_error(self):
        with tempfile.TemporaryDirectory() as t:
            code, out, err = run_main(["--config", self.config_file(t), "--rules", str(Path(t) / "nope.json")])
        self.assertEqual((code, out), (2, ""))


class RealProcessTests(unittest.TestCase):
    """The script as a user runs it: isolated python, real subprocesses, an empty PATH, no accounts, no network."""

    def run_script(self, *args, env=None):
        with tempfile.TemporaryDirectory() as empty:
            full_env = {"PATH": empty, "HOME": empty, **(env or {})}
            return subprocess.run([sys.executable, "-I", str(ROOT / "collect.py"), *args], capture_output=True, text=True,
                                  env=full_env, timeout=120, cwd=empty)

    def test_help_runs_isolated(self):
        proc = self.run_script("--help")
        self.assertEqual(proc.returncode, 0)
        for flag in ("--config", "--state-dir", "--output", "--pretty"):
            self.assertIn(flag, proc.stdout)

    def test_the_example_config_with_nothing_installed_degrades_to_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            proc = self.run_script("--config", str(EXAMPLE), "--state-dir", str(Path(t) / "state"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = json.loads(proc.stdout)
        self.assertEqual(fc.validate(snap, SCHEMA), [])
        enabled = [sid for sid, _ in fc.enabled_sources(fc.load_config(EXAMPLE))]
        self.assertEqual([s["id"] for s in snap["sources"]], enabled)
        self.assertEqual({p["state"] for p in snap["pools"]}, {"unknown"})
        self.assertEqual({s["error_code"] for s in snap["sources"] if not s["ok"]}, {"not_configured"})
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
