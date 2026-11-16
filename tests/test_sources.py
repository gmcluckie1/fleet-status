"""Fixture tests for each source: what it reads, what it reports, and how it degrades. No accounts needed."""
import copy
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (ADMIN_KEY, AGY, API_KEY, CODEX_RPC, ENV, GROK, NO_USAGE, NOW, OAUTH_TOKEN, OAUTH_USAGE,  # noqa: E402
                     OPENAI_KEY, RULES, SCHEMA, BILLING, FakeHttp, FakeRunner, build, codex_log, cost_body, fc, load,
                     make_config, no_rpc, openai_body, pool, src)


def valid(test, snap):
    test.assertEqual(fc.validate(snap, SCHEMA), [])


class MuseTests(unittest.TestCase):
    def two_runs(self, t, later, after, first=None):
        state = Path(t) / "state"
        state.mkdir()
        build(t, FakeRunner(overrides={"muse": first} if first else None), only=("muse",), state_dir=state)
        return build(t, FakeRunner(overrides={"muse": after}), only=("muse",), state_dir=state, now=later)

    def test_windows(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("muse",))
        p = pool(snap, "muse")
        self.assertEqual({w["label"]: w["used_pct"] for w in p["windows"]}, {"5h": 6, "weekly": 44})
        self.assertEqual((p["used"], p["limit"], p["remaining"], p["unit"]), (44, 100, 56, "percent"))
        self.assertEqual(p["state"], "healthy")
        self.assertEqual(p["windows"][1]["resets_at"], fc.fmt_utc(1793577600))
        valid(self, snap)

    def test_null_allowance_is_an_unknown_pool_not_a_failed_source(self):
        null = json.dumps({"schemaVersion": 1, "allowance": None, "reason": "no data", "hint": None})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"muse": null}), only=("muse",))
        self.assertTrue(src(snap, "muse")["ok"])
        self.assertEqual(pool(snap, "muse")["state"], "unknown")
        valid(self, snap)

    def test_server_sent_no_usage_keeps_last_weekly_and_invents_no_window(self):
        with tempfile.TemporaryDirectory() as t:
            snap = self.two_runs(t, NOW + fc.timedelta(hours=1), NO_USAGE)
        p = pool(snap, "muse")
        self.assertTrue(src(snap, "muse")["ok"])
        self.assertEqual(p["windows"], [{"label": "weekly", "used_pct": 44, "resets_at": fc.fmt_utc(1793577600)}])
        self.assertEqual((p["used"], p["observed_at"]), (44, fc.fmt_utc(1793568529)))  # dated at the last real reading
        valid(self, snap)

    def test_held_weekly_is_dropped_once_its_reset_has_passed(self):
        with tempfile.TemporaryDirectory() as t:
            snap = self.two_runs(t, NOW + fc.timedelta(hours=5), NO_USAGE)  # the weekly window resets at 2026-11-02T00:00Z
        p = pool(snap, "muse")
        self.assertEqual((p["state"], p["used"]), ("unknown", None))  # never a healthy 0%
        valid(self, snap)

    def test_no_usage_without_history_is_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"muse": NO_USAGE}), only=("muse",))
        p = pool(snap, "muse")
        self.assertEqual((p["state"], p["used"], p["observed_at"]), ("unknown", None, fc.fmt_utc(NOW)))

    def test_custom_command_is_what_runs(self):
        runner = FakeRunner()
        cfg = make_config(("muse",), muse={"command": ["muse", "usage", "--json", "--profile", "x"]})
        with tempfile.TemporaryDirectory() as t:
            build(t, runner, config=cfg)
        self.assertEqual(runner.calls, [["muse", "usage", "--json", "--profile", "x"]])


class CodexTests(unittest.TestCase):
    def test_latest_session_event_only_and_credits(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex",))
        p = pool(snap, "codex")
        self.assertEqual(p["windows"][0]["label"], "weekly")
        self.assertEqual(p["windows"][0]["used_pct"], 100)  # the latest event, not the older 1.0 one
        self.assertEqual(p["state"], "low")
        self.assertEqual(pool(snap, "codex-credits")["remaining"], 36779)
        self.assertNotIn("SECRET CONTENT", json.dumps(snap))
        valid(self, snap)

    def test_window_already_reset_is_unknown_not_a_stale_number(self):
        with tempfile.TemporaryDirectory() as t:
            codex_log(Path(t) / "sessions", resets=int(NOW.timestamp()) - 60)
            p = pool(build(t, only=("codex",)), "codex")
        self.assertIsNone(p["windows"][0]["used_pct"])
        self.assertEqual(p["state"], "unknown")

    def test_old_event_is_stale_not_current(self):
        with tempfile.TemporaryDirectory() as t:
            codex_log(Path(t) / "sessions", limits_ts="2026-10-30T00:00:00.000Z")
            snap = build(t, only=("codex",))
        self.assertEqual((src(snap, "codex")["ok"], src(snap, "codex")["observed_at"]), (True, "2026-10-30T00:00:00Z"))
        p = pool(snap, "codex")
        self.assertEqual((p["state"], p["observed_at"], p["used"], p["limit"], p["remaining"]),
                         ("unknown", "2026-10-30T00:00:00Z", None, None, None))
        self.assertEqual(p["advice"], RULES["advice"]["stale"])
        self.assertEqual(p["windows"], [{"label": "weekly", "used_pct": None, "resets_at": fc.fmt_utc(1793774214)}])
        cr = pool(snap, "codex-credits")
        self.assertEqual((cr["state"], cr["remaining"], cr["advice"]), ("unknown", None, RULES["advice"]["stale"]))
        valid(self, snap)

    def test_a_missing_sessions_dir_is_not_configured(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex",), config=make_config(("codex",), codex={"sessions_dir": str(Path(t) / "none")}))
        self.assertEqual((src(snap, "codex")["ok"], src(snap, "codex")["error_code"]), (False, "not_configured"))
        self.assertEqual(pool(snap, "codex")["state"], "unknown")

    def test_no_sessions_degrades_to_an_unknown_pool(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "sessions").mkdir()
            snap = build(t, only=("codex",))
        self.assertEqual((src(snap, "codex")["ok"], src(snap, "codex")["error_code"]), (False, "unavailable"))
        p = pool(snap, "codex")
        self.assertEqual((p["state"], p["used"], p["observed_at"]), ("unknown", None, None))
        self.assertTrue(p["advice"].startswith("Unavailable: "))
        valid(self, snap)

    def test_rpc_pool_credits_and_reset(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex",), codex_rpc=lambda argv, timeout: CODEX_RPC)
        p = pool(snap, "codex")
        self.assertEqual(p["windows"], [{"label": "weekly", "used_pct": 100, "resets_at": fc.fmt_utc(1793774214)}])
        self.assertEqual((p["state"], src(snap, "codex")["observed_at"]), ("low", fc.fmt_utc(NOW)))
        self.assertEqual((pool(snap, "codex-credits")["remaining"], pool(snap, "codex-credits")["state"]), (1234, "healthy"))
        valid(self, snap)

    def test_credits_below_the_threshold_are_low(self):
        rpc = copy.deepcopy(CODEX_RPC)
        rpc["rateLimits"]["credits"]["balance"] = "999.9"
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex",), codex_rpc=lambda argv, timeout: rpc)
        self.assertEqual(pool(snap, "codex-credits")["state"], "low")

    def test_rpc_publishes_the_reset_credits_count(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex",), codex_rpc=lambda argv, timeout: CODEX_RPC)
        p = pool(snap, "codex-reset-credits")
        self.assertEqual((p["remaining"], p["kind"], p["unit"], p["source_id"]), (2, "credit", "credits", "codex"))

    def test_reset_credits_absent_or_zero_make_no_pool(self):
        for extra in ({}, {"rateLimitResetCredits": {"availableCount": 0, "credits": []}},
                      {"rateLimitResetCredits": {"availableCount": "x"}}):
            rpc = {k: v for k, v in CODEX_RPC.items() if k != "rateLimitResetCredits"}
            rpc.update(extra)
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, only=("codex",), codex_rpc=lambda argv, timeout, r=rpc: r)
            self.assertIsNone(pool(snap, "codex-reset-credits"))
            self.assertEqual(pool(snap, "codex-credits")["remaining"], 1234)

    def test_secondary_window_and_the_reached_flag(self):
        rpc = copy.deepcopy(CODEX_RPC)
        rl = rpc["rateLimits"]
        rl["primary"] = {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": int(NOW.timestamp()) + 3600}
        rl["secondary"] = {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": int(NOW.timestamp()) + 86400}
        rl["rateLimitReachedType"] = None
        with tempfile.TemporaryDirectory() as t:
            p = pool(build(t, only=("codex",), codex_rpc=lambda argv, timeout: rpc), "codex")
        self.assertEqual([(w["label"], w["used_pct"]) for w in p["windows"]], [("5h", 20), ("weekly", 40)])
        self.assertEqual((p["used"], p["state"]), (40, "spend_first"))
        rl["rateLimitReachedType"] = "rate_limit_reached"
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(pool(build(t, only=("codex",), codex_rpc=lambda argv, timeout: rpc), "codex")["state"], "low")

    def test_rpc_failure_or_garbage_falls_back_to_the_session_scan(self):
        for rpc in (no_rpc, lambda argv, timeout: {"nope": 1}):
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, only=("codex",), codex_rpc=rpc)
            self.assertEqual(pool(snap, "codex")["windows"][0]["used_pct"], 100)
            self.assertEqual(src(snap, "codex")["observed_at"], "2026-11-01T21:20:00Z")

    def test_the_configured_command_reaches_the_rpc(self):
        seen = []
        cfg = make_config(("codex",), codex={"command": ["codex-wrapper", "app-server"]})
        with tempfile.TemporaryDirectory() as t:
            build(t, config=cfg, codex_rpc=lambda argv, timeout: seen.append(argv) or CODEX_RPC)
        self.assertEqual(seen, [["codex-wrapper", "app-server"]])

    def test_sessions_dir_setting_then_codex_home_then_default(self):
        self.assertEqual(fc.codex_sessions_dir({"sessions_dir": "/x/s"}, {"CODEX_HOME": "/y"}), Path("/x/s"))
        self.assertEqual(fc.codex_sessions_dir({}, {"CODEX_HOME": "/y"}), Path("/y/sessions"))
        with patch.object(fc.Path, "home", return_value=Path("/h")):
            self.assertEqual(fc.codex_sessions_dir({}, {}), Path("/h/.codex/sessions"))

    def test_session_scan_walks_only_the_newest_day_dirs(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for day in ("2026/10/01", "2026/10/02", "2026/11/01", "2026/11/02"):
                d = root / day
                d.mkdir(parents=True)
                (d / "rollout.jsonl").write_text("{}\n")
            (root / "2026" / "stray").mkdir()
            files = fc._newest_session_files(root, want=2, max_days=2)
        self.assertEqual(sorted(f.parent.name for f in files), ["01", "02"])
        self.assertEqual({f.parent.parent.name for f in files}, {"11"})

    def test_real_rpc_missing_binary_is_unavailable(self):
        with self.assertRaises(fc.CmdError) as cm:
            fc.codex_rate_limits(["definitely-not-a-binary-xyz", "app-server"], 1)
        self.assertEqual(cm.exception.code, "unavailable")

    def test_real_rpc_speaks_json_rpc_and_drops_the_api_key(self):
        """A stub app-server answers like the real one; OPENAI_API_KEY must not reach it."""
        with tempfile.TemporaryDirectory() as t:
            stub = Path(t) / "codex"
            stub.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                            "assert sys.argv[1:] == ['app-server']\n"
                            "seen = []\n"
                            "for line in sys.stdin:\n"
                            "    m = json.loads(line); seen.append(m.get('method'))\n"
                            "    if m.get('method') == 'initialize':\n"
                            "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
                            "    if m.get('method') == 'account/rateLimits/read':\n"
                            "        ok = seen == ['initialize', 'initialized', 'account/rateLimits/read'] and 'OPENAI_API_KEY' not in os.environ\n"
                            "        print(json.dumps({'id': 2, 'result': {'ok': ok}}), flush=True)\n")
            stub.chmod(0o755)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-value-000000"}):
                result = fc.codex_rate_limits([str(stub), "app-server"], 5)
        self.assertEqual(result, {"ok": True})


class AgyTests(unittest.TestCase):
    def test_pool_per_group_with_windows_and_reset(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("agy",))
        valid(self, snap)
        gem = pool(snap, "agy-account-a-gemini-models")
        self.assertEqual(gem["label"], "agy-account-a-gemini-models (weekly)")
        self.assertEqual([(w["label"], w["used_pct"], w["resets_at"]) for w in gem["windows"]],
                         [("weekly", 0, "2026-11-04T00:01:19Z"), ("5h", 0, "2026-11-02T15:19:22Z")])
        self.assertEqual(gem["state"], "healthy")
        third = pool(snap, "agy-account-a-claude-and-gpt-models")
        self.assertEqual([(w["label"], w["used_pct"]) for w in third["windows"]], [("weekly", 100)])  # disabled 5h skipped
        self.assertEqual(third["state"], "low")
        argon = pool(snap, "agy-account-a-gemini-4-argon")
        self.assertEqual((argon["windows"][0]["used_pct"], argon["state"], argon["windows"][0]["resets_at"]),
                         (100, "low", "2026-11-09T00:44:56Z"))
        self.assertTrue(src(snap, "agy")["ok"])

    def test_each_account_runs_under_its_own_home(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            build(t, runner, only=("agy",))
        calls = [c for c in runner.calls if "agy" in c]
        self.assertEqual(calls, [["env", "HOME=/fixture/agy-a", "agy", "-p", "/usage", "--output-format", "json"],
                                 ["env", "HOME=/fixture/agy-b", "agy", "-p", "/usage", "--output-format", "json"]])

    def test_account_without_home_runs_the_plain_command_and_command_can_be_overridden(self):
        runner = FakeRunner()
        cfg = make_config(("agy",), agy={"accounts": [{"label": "solo"}, {"label": "wrapped", "command": ["agy", "--json"]}]})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, runner, config=cfg)
        self.assertEqual(runner.calls, [["agy", "-p", "/usage", "--output-format", "json"], ["agy", "--json"]])
        self.assertIsNotNone(pool(snap, "agy-solo-gemini-models"))
        self.assertIsNotNone(pool(snap, "agy-wrapped-gemini-models"))

    def test_variants_headline_binding_and_account_isolation(self):
        base = json.loads(AGY)
        for five_h_rem, want_five_h_used, want_state in ((0.90, 10, "healthy"), (0.05, 95, "low")):
            with self.subTest(five_h_rem=five_h_rem):
                doc_a = copy.deepcopy(base)
                a_3p = doc_a["command"]["data"]["groups"][1]["buckets"]  # weekly 45 remaining plus an enabled 5h bucket
                a_3p[0]["remaining_fraction"] = 0.45
                a_3p[1].pop("disabled", None)
                a_3p[1]["remaining_fraction"] = five_h_rem
                a_3p[1]["reset_time"] = "2026-11-02T16:00:00Z"
                doc_b = copy.deepcopy(base)
                b_gem = doc_b["command"]["data"]["groups"][0]["buckets"]
                b_gem[0]["remaining_fraction"] = 0.80
                b_gem[1]["remaining_fraction"] = 0.70
                b_3p = doc_b["command"]["data"]["groups"][1]["buckets"]  # weekly disabled: the 5h bucket headlines
                b_3p[0]["disabled"] = True
                b_3p[1].pop("disabled", None)
                b_3p[1]["remaining_fraction"] = 0.75
                b_3p[1]["reset_time"] = "2026-11-02T18:00:00Z"
                docs = {"HOME=/fixture/agy-a": json.dumps(doc_a), "HOME=/fixture/agy-b": json.dumps(doc_b)}

                def per_home(argv, timeout, cwd=None, docs=docs):
                    return docs[argv[1]]

                with tempfile.TemporaryDirectory() as t:
                    snap = build(t, per_home, only=("agy",))
                valid(self, snap)
                a_claude = pool(snap, "agy-account-a-claude-and-gpt-models")
                self.assertEqual((a_claude["used"], a_claude["remaining"], a_claude["state"]), (55, 45, want_state))
                self.assertEqual(a_claude["windows"], [
                    {"label": "weekly", "used_pct": 55, "resets_at": "2026-11-04T00:10:49Z"},
                    {"label": "5h", "used_pct": want_five_h_used, "resets_at": "2026-11-02T16:00:00Z"}])
                b_claude = pool(snap, "agy-account-b-claude-and-gpt-models")
                self.assertEqual(b_claude["label"], "agy-account-b-claude-and-gpt-models (5h)")
                self.assertEqual((b_claude["used"], b_claude["remaining"]), (25, 75))
                self.assertEqual(pool(snap, "agy-account-b-gemini-models")["used"], 20)

    def test_a_failing_account_leaves_an_unknown_pool_and_the_others_continue(self):
        for failure in ("timeout", "auth", "unavailable"):
            def run(argv, timeout, cwd=None, failure=failure):
                if argv[1] == "HOME=/fixture/agy-b":
                    raise fc.CmdError(failure)
                return AGY
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, run, only=("agy",))
            self.assertEqual(pool(snap, "agy-account-a-gemini-models")["state"], "healthy")
            bad = pool(snap, "agy-account-b-unavailable")
            self.assertEqual(bad["state"], "unknown")
            self.assertTrue(bad["advice"].startswith("Unavailable: "))
            self.assertTrue(src(snap, "agy")["ok"])
            valid(self, snap)

    def test_garbage_marks_every_account_unavailable(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"agy": json.dumps({"status": "SUCCESS"})}), only=("agy",))
        for label in ("account-a", "account-b"):
            self.assertEqual(pool(snap, f"agy-{label}-unavailable")["state"], "unknown")

    def test_shared_login_asks_after_three_identical_collections(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            for n in range(1, 4):  # the same /usage is served to both accounts
                snap = build(t, only=("agy",), state_dir=state)
                self.assertEqual("shared-login-agy" in [a["id"] for a in snap["asks"]], n == 3, n)
            valid(self, snap)
            ask = next(a for a in snap["asks"] if a["id"] == "shared-login-agy")
            self.assertEqual(ask["kind"], "confirm")
            self.assertIn("account-a, account-b", ask["detail"])
            other = re.sub(r'("remaining_fraction":\s*)[0-9.]+', r"\g<1>0.123", AGY)
            self.assertNotEqual(other, AGY)

            def per_home(argv, timeout, cwd=None):
                return other if argv[1] == "HOME=/fixture/agy-b" else AGY
            result = build(t, per_home, only=("agy",), state_dir=state)  # different readings reset the counter
        self.assertNotIn("shared-login-agy", [a["id"] for a in result["asks"]])

    def test_labels_with_dashes_do_not_confuse_the_shared_login_check(self):
        cfg = make_config(("agy",), agy={"accounts": [{"label": "a"}, {"label": "a-b"}]})
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            for _ in range(3):
                snap = build(t, config=copy.deepcopy(cfg), state_dir=state)
        self.assertIn("shared-login-agy", [a["id"] for a in snap["asks"]])


class ColabTests(unittest.TestCase):
    def test_each_account_is_read_through_its_own_command(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, runner, only=("colab",))
        valid(self, snap)
        self.assertEqual(pool(snap, "colab-account-a-units")["remaining"], 594)
        self.assertEqual(pool(snap, "colab-account-b-units")["state"], "healthy")
        self.assertEqual(pool(snap, "colab-account-a-units")["label"], "Colab compute units (account-a)")
        self.assertEqual({c[0] for c in runner.calls}, {"colab", "colab-b"})

    def test_whoami_is_only_asked_when_an_email_is_expected(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            build(t, runner, only=("colab",))
        self.assertEqual([c for c in runner.calls if c[-1] == "whoami"], [["colab", "whoami"]])

    def test_a_mismatched_account_is_unknown_with_no_balance(self):
        cfg = make_config(("colab",), colab={"accounts": [{"label": "account-a", "expect_email": "someone-else@example.test"}]})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, config=cfg)
        p = pool(snap, "colab-account-a-units")
        self.assertEqual((p["state"], p["remaining"]), ("unknown", None))  # the schema has no 'unavailable' state
        self.assertIn("account mismatch", p["advice"])
        self.assertNotIn("someone-else", json.dumps(snap))  # the address is compared in memory and never published
        self.assertNotIn("a@example.test", json.dumps(snap))

    def test_zero_balance_is_low_and_unparseable_output_is_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            zero = build(t, FakeRunner(overrides={"colab_usage": "Current balance: 0.00 compute units\n"}), only=("colab",))
            junk = build(t, FakeRunner(overrides={"colab_usage": "nothing useful"}), only=("colab",))
        self.assertEqual(pool(zero, "colab-account-b-units")["state"], "low")
        self.assertEqual(pool(junk, "colab-account-b-units")["state"], "unknown")
        self.assertTrue(src(junk, "colab")["ok"])

    def test_no_accounts_key_means_one_default_account(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, runner, config={"collector_id": "x", "state_dir": None, "pool_notes": {}, "sources": {"colab": {}}})
        self.assertEqual(pool(snap, "colab-default-units")["remaining"], 594)
        self.assertEqual(runner.calls, [["colab", "usage"]])


class ClaudeSubscriptionTests(unittest.TestCase):
    def test_weekly_percent_and_utc_reset(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("claude_sub",))
        p = pool(snap, "claude-subscription")
        # Nov 8 04:00 America/New_York (EST, UTC-5) = 09:00Z
        self.assertEqual(p["windows"], [{"label": "weekly", "used_pct": 17, "resets_at": "2026-11-08T09:00:00Z"}])
        self.assertEqual((p["used"], p["remaining"], p["state"], p["source_id"]), (17, 83, "healthy", "claude_sub"))
        valid(self, snap)

    def test_reset_time_formats_and_year_rollover(self):
        zone = "America/New_York"
        dec = fc.parse_utc("2026-12-30T12:00:00Z")
        self.assertEqual(fc.claude_reset_utc("Jan 3 at 3:59am", zone, dec), "2027-01-03T08:59:00Z")
        self.assertEqual(fc.claude_reset_utc("Dec 31 at 11pm", zone, dec), "2027-01-01T04:00:00Z")
        self.assertEqual(fc.claude_reset_utc("Nov 8 at 12am", zone, NOW), "2026-11-08T05:00:00Z")
        self.assertEqual(fc.claude_reset_utc("Nov 8 at 12pm", zone, NOW), "2026-11-08T17:00:00Z")
        # DST: a March date is EDT (UTC-4)
        self.assertEqual(fc.claude_reset_utc("Mar 20 at 4am", zone, fc.parse_utc("2027-03-10T00:00:00Z")), "2027-03-20T08:00:00Z")
        # a bare time is its next occurrence (21:30Z = 16:30 EST, so 9pm is today and 9am is tomorrow)
        self.assertEqual(fc.claude_reset_utc("9pm", zone, NOW), "2026-11-02T02:00:00Z")
        self.assertEqual(fc.claude_reset_utc("9am", zone, NOW), "2026-11-02T14:00:00Z")
        self.assertIsNone(fc.claude_reset_utc("whenever", zone, NOW))
        self.assertIsNone(fc.claude_reset_utc("4am", "Not/AZone", NOW))

    def test_session_line_becomes_a_5h_window_and_model_lines_are_skipped(self):
        text = ("Current session: 3% used · resets 9pm (America/New_York)\n"
                "Current week (all models): 61.6% used · resets Nov 8 at 3:59am (America/New_York)\n"
                "Current week (Opus): 90% used · resets Nov 8 at 4am (America/New_York)\n")
        w = fc.parse_claude_usage(text, NOW)
        self.assertEqual([(x["label"], x["used_pct"]) for x in w], [("weekly", 62), ("5h", 3)])
        self.assertEqual(w[1]["resets_at"], "2026-11-02T02:00:00Z")

    def test_output_without_usage_lines_is_a_parse_error_and_an_unknown_pool(self):
        bad = json.dumps([{"type": "result", "num_turns": 0, "result": "Usage is not available"}])
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"claude": bad}), only=("claude_sub",))
        self.assertEqual((src(snap, "claude_sub")["ok"], src(snap, "claude_sub")["error_code"]), (False, "parse_error"))
        self.assertEqual(pool(snap, "claude-subscription")["state"], "unknown")
        self.assertEqual(pool(snap, "claude-fable")["state"], "unknown")

    def test_a_model_turn_or_missing_num_turns_is_a_parse_error(self):
        text = "Current week (all models): 17% used · resets Nov 8 at 4am (America/New_York)"
        for extra in ({"num_turns": 1}, {}, {"num_turns": False}):
            payload = json.dumps([{"type": "result", "result": text, **extra}])
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, FakeRunner(overrides={"claude": payload}), only=("claude_sub",))
            self.assertEqual(src(snap, "claude_sub")["error_code"], "parse_error")

    def test_the_command_drops_auth_env_and_makes_no_model_turn(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            build(t, runner, only=("claude_sub",))
        argv = runner.calls[0]
        self.assertEqual(argv[0], "env")
        self.assertEqual(argv[-4:], ["claude", "-p", "/usage", "--output-format", "json"][-4:])
        unset = [argv[i + 1] for i, a in enumerate(argv) if a == "-u"]
        self.assertEqual(unset, list(fc.CLAUDE_AUTH_ENV))
        self.assertIn("ANTHROPIC_API_KEY", unset)
        self.assertIn("/usage", argv)

    def test_fable_pool_from_the_usage_line(self):
        text = ("Current week (all models): 30% used · resets Nov 22 at 4am (America/New_York)\n"
                "Current week (Fable): 2% used · resets Nov 22 at 4am (America/New_York)\n")
        raw = json.dumps([{"type": "result", "num_turns": 0, "result": text}])
        cfg = make_config(("claude_sub",))
        cfg["pool_notes"] = {"claude-fable": "Resets are counted in my notes"}
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"claude": raw}), config=cfg)
        p = pool(snap, "claude-fable")
        self.assertEqual(p["windows"], [{"label": "weekly", "used_pct": 2, "resets_at": "2026-11-22T09:00:00Z"}])
        self.assertEqual((p["used"], p["remaining"], p["state"], p["kind"], p["unit"]), (2, 98, "healthy", "subscription", "percent"))
        self.assertEqual(p["advice"], "Resets are counted in my notes")  # a pool note rides in the advice
        self.assertEqual(pool(snap, "claude-subscription")["used"], 30)  # Fable never leaks into the all-models pool
        valid(self, snap)

    def test_fable_absent_from_usage_is_unavailable_and_fable_false_omits_it(self):
        text = "Current week (all models): 30% used · resets Nov 22 at 4am (America/New_York)\n"
        raw = json.dumps([{"type": "result", "num_turns": 0, "result": text}])
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"claude": raw}), only=("claude_sub",))
            quiet = build(t, FakeRunner(overrides={"claude": raw}), config=make_config(("claude_sub",), claude_sub={"fable": False}))
        p = pool(snap, "claude-fable")
        self.assertEqual((p["state"], p["used"], p["windows"]), ("unknown", None, []))
        self.assertIn("Unavailable: claude /usage shows no Fable weekly limit", p["advice"])
        self.assertIsNone(pool(quiet, "claude-fable"))
        self.assertIsNotNone(pool(quiet, "claude-subscription"))
        valid(self, snap)

    def test_a_failed_source_leaves_unknown_pools_and_respects_fable_false(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(fail={"claude": "timeout"}), only=("claude_sub",))
            quiet = build(t, FakeRunner(fail={"claude": "timeout"}), config=make_config(("claude_sub",), claude_sub={"fable": False}))
        self.assertEqual(sorted(p["id"] for p in snap["pools"]), ["claude-fable", "claude-subscription"])
        self.assertEqual([p["id"] for p in quiet["pools"]], ["claude-subscription"])


class GrokTests(unittest.TestCase):
    def grok(self, **changes):
        billing = copy.deepcopy(GROK)
        billing["config"].update(changes)
        return lambda argv, timeout: billing

    def test_weekly_percent_and_reset(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("grok",))
        p = pool(snap, "grok")
        self.assertEqual((p["state"], p["used"], p["remaining"]), ("healthy", 64, 36))  # 64.5 rounds half to even
        self.assertEqual(p["windows"], [{"label": "weekly", "used_pct": 64, "resets_at": "2026-11-08T11:05:11Z"}])
        valid(self, snap)

    def test_exhausted_is_low_but_prepaid_or_on_demand_headroom_is_only_watch(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(pool(build(t, only=("grok",), grok_rpc=self.grok(creditUsagePercent=100.0)), "grok")["state"], "low")
            prepaid = self.grok(creditUsagePercent=100.0, prepaidBalance={"val": 500})
            self.assertEqual(pool(build(t, only=("grok",), grok_rpc=prepaid), "grok")["state"], "watch")
            on_demand = self.grok(creditUsagePercent=100.0, onDemandCap={"val": 900}, onDemandUsed={"val": 10})
            self.assertEqual(pool(build(t, only=("grok",), grok_rpc=on_demand), "grok")["state"], "watch")

    def test_unknown_period_or_shape_is_a_parse_error_and_an_unknown_pool(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("grok",), grok_rpc=self.grok(
                currentPeriod={"type": "USAGE_PERIOD_TYPE_DAILY", "end": "2026-11-02T00:00:00Z"}))
            self.assertEqual((src(snap, "grok")["ok"], src(snap, "grok")["error_code"]), (False, "parse_error"))
            self.assertEqual(pool(snap, "grok")["state"], "unknown")
            snap = build(t, only=("grok",), grok_rpc=lambda argv, timeout: {"unexpected": 1})
            self.assertEqual(src(snap, "grok")["error_code"], "parse_error")

    def test_fresh_period_without_a_usage_field_is_zero_percent_only_once_started(self):
        real = json.loads(load("grok-billing-fresh-period.json"))["result"]
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("grok",), grok_rpc=lambda argv, timeout: real, now=fc.parse_utc("2026-11-08T12:00:00Z"))
            early = build(t, only=("grok",), grok_rpc=lambda argv, timeout: real, now=fc.parse_utc("2026-11-08T10:00:00Z"))
        p = pool(snap, "grok")
        self.assertEqual((p["state"], p["used"], p["windows"][0]["resets_at"]), ("healthy", 0, "2026-11-15T11:05:11Z"))
        self.assertEqual(src(early, "grok")["error_code"], "parse_error")  # a period that has not started is not trusted

    def test_the_configured_command_reaches_the_rpc(self):
        seen = []
        cfg = make_config(("grok",), grok={"command": ["/opt/tools/grok", "agent", "--no-leader", "stdio"]})
        with tempfile.TemporaryDirectory() as t:
            build(t, config=cfg, grok_rpc=lambda argv, timeout: seen.append(argv) or GROK)
        self.assertEqual(seen, [["/opt/tools/grok", "agent", "--no-leader", "stdio"]])

    def test_real_rpc_speaks_json_rpc_and_drops_the_api_key(self):
        """A stub answers like `agent --no-leader stdio`; XAI_API_KEY must not reach it and the updater is off."""
        with tempfile.TemporaryDirectory() as t:
            stub = Path(t) / "grok"
            stub.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                            "assert sys.argv[1:] == ['agent', '--no-leader', 'stdio']\n"
                            "seen = []\n"
                            "for line in sys.stdin:\n"
                            "    m = json.loads(line); seen.append(m.get('method'))\n"
                            "    if m.get('method') == 'initialize':\n"
                            "        assert m['params'] == {'protocolVersion': 1, 'clientCapabilities': {}}\n"
                            "        print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}), flush=True)\n"
                            "    if m.get('method') == '_x.ai/billing':\n"
                            "        ok = seen == ['initialize', '_x.ai/billing'] and 'XAI_API_KEY' not in os.environ"
                            " and os.environ.get('GROK_DISABLE_AUTOUPDATER') == '1'\n"
                            "        print(json.dumps({'jsonrpc': '2.0', 'id': 2, 'result': {'ok': ok}}), flush=True)\n")
            stub.chmod(0o755)
            with patch.dict(os.environ, {"XAI_API_KEY": "not-a-real-value-000000"}):
                result = fc.grok_billing([str(stub), "agent", "--no-leader", "stdio"], 5)
        self.assertEqual(result, {"ok": True})

    def test_real_rpc_missing_binary_is_not_configured_and_a_silent_child_times_out(self):
        with self.assertRaises(fc.CmdError) as cm:
            fc.grok_billing(["definitely-not-a-binary-xyz"], 1)
        self.assertEqual(cm.exception.code, "not_configured")
        with self.assertRaises(fc.CmdError) as cm:  # a child that never answers: timeout, and the child is killed
            fc.stdio_rpc(["/bin/sleep", "30"], dict(os.environ), [], 2, 0.3)
        self.assertEqual(cm.exception.code, "timeout")

    def test_real_rpc_waits_for_a_background_refresh_and_retries(self):
        """When auth is expired, grok returns -32603 first and refreshes on stdin EOF before exiting."""
        expired = json.dumps(json.loads(load("grok-billing-expired-error.json")))
        good = json.dumps(json.loads(load("grok-billing.json")))
        with tempfile.TemporaryDirectory() as t:
            marker = Path(t) / "auth-refreshed"
            stub = Path(t) / "grok"
            stub.write_text("#!/usr/bin/env python3\nimport json, pathlib, sys, time\n"
                            f"marker = pathlib.Path({str(marker)!r})\n"
                            f"err = {expired!r}\n"
                            f"good = {good!r}\n"
                            "for line in sys.stdin:\n"
                            "    m = json.loads(line)\n"
                            "    if m.get('method') == 'initialize':\n"
                            "        print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}), flush=True)\n"
                            "    elif m.get('method') == '_x.ai/billing':\n"
                            "        print(good if marker.exists() else err, flush=True)\n"
                            "if not marker.exists():\n"
                            "    time.sleep(0.05)\n"
                            "    marker.write_text('ok')\n")
            stub.chmod(0o755)
            result = fc.grok_billing([str(stub), "agent", "--no-leader", "stdio"], 5)
        self.assertEqual(result, GROK)


class ClaudeCloudTests(unittest.TestCase):
    def test_picks_the_bucket_by_limit_dollars_not_codename(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("claude_cloud",))
            renamed = OAUTH_USAGE.replace("example_codename_bucket", "some_entirely_new_codename")
            again = build(t, only=("claude_cloud",), http=FakeHttp(usage=((200, renamed),)))
        p = pool(snap, "claude-cloud")
        self.assertEqual((p["kind"], p["unit"], p["used"], p["limit"], p["remaining"]), ("credit", "usd_cents", 20480, 25000, 4520))
        self.assertEqual(p["windows"], [{"label": "monthly", "used_pct": 82, "resets_at": "2026-11-05T07:59:00Z"}])
        self.assertEqual(p["state"], "watch")
        self.assertEqual(pool(again, "claude-cloud")["used"], 20480)
        valid(self, snap)

    def test_request_shape(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            build(t, only=("claude_cloud",), http=http)
        method, url, headers = http.calls[0]
        self.assertEqual((method, url), ("GET", "https://api.anthropic.com/api/oauth/usage"))
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["Authorization"], "Bearer " + OAUTH_TOKEN)

    def test_no_active_credit_bucket_is_ok_with_no_pool(self):
        no_credit = load("claude-oauth-usage-no-credit.json")
        http = FakeHttp(usage=((200, no_credit),))
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("claude_cloud",), http=http)
            bad = build(t, only=("claude_cloud",), http=FakeHttp(usage=((200, json.dumps({"unexpected": 1})),)))
        self.assertEqual((src(snap, "claude_cloud")["ok"], src(snap, "claude_cloud")["error_code"]), (True, None))
        self.assertEqual(snap["pools"], [])
        self.assertEqual(src(bad, "claude_cloud")["error_code"], "parse_error")
        self.assertEqual(pool(bad, "claude-cloud")["state"], "unknown")

    def test_polled_at_most_every_15_minutes(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            build(t, only=("claude_cloud",), http=http, state_dir=state)
            soon = build(t, only=("claude_cloud",), http=http, state_dir=state, now=NOW + fc.timedelta(minutes=14))
            self.assertEqual(http.count("oauth/usage"), 1)
            self.assertEqual(pool(soon, "claude-cloud")["observed_at"], fc.fmt_utc(NOW))  # the cached reading, honestly dated
            later = build(t, only=("claude_cloud",), http=http, state_dir=state, now=NOW + fc.timedelta(minutes=16))
            self.assertEqual(http.count("oauth/usage"), 2)
            self.assertEqual(pool(later, "claude-cloud")["observed_at"], fc.fmt_utc(NOW + fc.timedelta(minutes=16)))

    def test_a_401_refreshes_through_the_cli_once_and_retries_once(self):
        http, runner = FakeHttp(usage=((401, "{}"), (200, OAUTH_USAGE))), FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, runner, only=("claude_cloud",), http=http)
        self.assertTrue(src(snap, "claude_cloud")["ok"])
        self.assertEqual(http.count("oauth/usage"), 2)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0], "env")
        self.assertEqual(runner.calls[0][-3:], ["claude", "-p", "/usage"])

    def test_a_401_twice_is_unauthorized_not_a_loop(self):
        http = FakeHttp(usage=((401, "{}"),))
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("claude_cloud",), http=http)
        self.assertEqual(src(snap, "claude_cloud")["error_code"], "unauthorized")
        self.assertEqual(http.count("oauth/usage"), 2)  # the first try and the single retry
        self.assertEqual(pool(snap, "claude-cloud")["state"], "unknown")

    def test_a_missing_claude_cli_does_not_stop_the_retry(self):
        class NoCli(FakeRunner):
            def __call__(self, argv, timeout, cwd=None):
                raise fc.CmdError("not_configured")
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, NoCli(), only=("claude_cloud",), http=FakeHttp(usage=((401, "{}"), (200, OAUTH_USAGE))))
        self.assertTrue(src(snap, "claude_cloud")["ok"])

    def test_the_credential_may_be_the_json_the_cli_stores(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": OAUTH_TOKEN, "refreshToken": "r-" + OAUTH_TOKEN}})
        http = FakeHttp()
        cfg = make_config(("claude_cloud",), claude_cloud={"credential": {"command": ["print-credential"]}})
        runner = FakeRunner()
        original = runner.__call__

        def run(argv, timeout, cwd=None):
            return blob if argv == ["print-credential"] else original(argv, timeout, cwd)
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, run, config=cfg, http=http)
        self.assertTrue(src(snap, "claude_cloud")["ok"])
        self.assertEqual(http.calls[0][2]["Authorization"], "Bearer " + OAUTH_TOKEN)
        self.assertNotIn("r-" + OAUTH_TOKEN, json.dumps(snap))

    def test_malformed_credential_json_is_a_parse_error(self):
        cfg = make_config(("claude_cloud",), claude_cloud={"credential": {"command": ["print-credential"]}})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, lambda argv, timeout, cwd=None: "{" + OAUTH_TOKEN, config=cfg)
        self.assertEqual(src(snap, "claude_cloud")["error_code"], "parse_error")
        self.assertNotIn(OAUTH_TOKEN, json.dumps(snap))

    def test_no_credential_is_not_configured_and_an_unknown_pool(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("claude_cloud",), env={}, http=http)
        self.assertEqual((src(snap, "claude_cloud")["ok"], src(snap, "claude_cloud")["error_code"]), (False, "not_configured"))
        self.assertEqual(pool(snap, "claude-cloud")["state"], "unknown")
        self.assertEqual(http.calls, [])  # nothing is sent without a credential


class GitHubBillingTests(unittest.TestCase):
    def daily(self, quantity, unit="minutes"):
        return {"usageItems": [{"date": f"2026-11-0{day}", "product": "actions", "unitType": unit, "quantity": quantity}
                               for day in (5, 6, 7)]}

    def test_minutes_against_the_included_allowance(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("github_billing",))
        p = pool(snap, "github-actions")
        self.assertEqual((p["unit"], p["used"], p["kind"], p["limit"], p["remaining"]), ("minutes", 3, "minutes", 2000, 1997))
        self.assertEqual(p["windows"], [{"label": "monthly", "used_pct": 0, "resets_at": "2026-12-01T00:00:00Z"}])
        valid(self, snap)

    def test_user_and_org_use_different_endpoints(self):
        for kind, prefix in (("user", "/users/example-owner/"), ("org", "/organizations/example-owner/")):
            runner = FakeRunner()
            cfg = make_config(("github_billing",), github_billing={"owner_type": kind})
            with tempfile.TemporaryDirectory() as t:
                build(t, runner, config=cfg)
            self.assertEqual(runner.calls[1][:3], ["gh", "api", prefix + "settings/billing/usage/summary"], kind)

    def test_states_at_the_watch_and_low_boundaries(self):
        now = fc.parse_utc("2026-11-08T12:00:00Z")
        for minutes, expected in ((900, "healthy"), (1879, "healthy"), (1880, "watch"), (1950, "watch"), (2000, "low")):
            with self.subTest(minutes=minutes), tempfile.TemporaryDirectory() as t:
                billing = {**BILLING, "usageItems": [{**BILLING["usageItems"][0], "grossQuantity": minutes}]}
                runner = FakeRunner(overrides={"billing": json.dumps(billing), "billing_daily": json.dumps(self.daily(46))})
                p = pool(build(t, runner, only=("github_billing",), now=now), "github-actions")
                self.assertEqual((p["limit"], p["remaining"], p["state"]), (2000, 2000 - minutes, expected))
                self.assertEqual(p["windows"][0]["used_pct"], round(minutes / 20))

    def test_recent_burn_forecasts_the_run_out_and_paid_overage_is_watch(self):
        now = fc.parse_utc("2026-11-08T12:00:00Z")
        billing = {"usageItems": [{**BILLING["usageItems"][0], "grossQuantity": 1000, "netAmount": 1}]}
        with tempfile.TemporaryDirectory() as t:
            runner = FakeRunner(overrides={"billing": json.dumps(billing), "billing_daily": json.dumps(self.daily(100))})
            p = pool(build(t, runner, only=("github_billing",), now=now), "github-actions")
            self.assertEqual(p["state"], "watch")  # paid overage
            self.assertEqual(p["runs_out_at"], fc.fmt_utc(now + fc.timedelta(days=10)))  # 1000 left at 100 a day
            slow = FakeRunner(overrides={"billing": json.dumps(billing), "billing_daily": json.dumps(self.daily(1))})
            self.assertIsNone(pool(build(t, slow, only=("github_billing",), now=now), "github-actions")["runs_out_at"])
            broken = FakeRunner(fail={"billing_daily": "unavailable"}, overrides={"billing": json.dumps(billing)})
            self.assertIsNone(pool(build(t, broken, only=("github_billing",), now=now), "github-actions")["runs_out_at"])

    def test_a_spent_rate_limit_is_rate_limited_and_burns_no_calls(self):
        spent = json.dumps({"resources": {"core": {"limit": 5000, "remaining": 0, "reset": 1793580000}}})
        runner = FakeRunner(overrides={"rate_limit": spent})
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            snap = build(t, runner, only=("github_billing",), state_dir=state)
            hit = fc.UsageState(state / fc.USAGE_STATE_FILE).get("gh_rate_limit")
        self.assertEqual((src(snap, "github_billing")["ok"], src(snap, "github_billing")["error_code"]), (False, "rate_limited"))
        self.assertEqual(hit["reset_at"], fc.fmt_utc(1793580000))
        self.assertEqual(runner.calls, [["gh", "api", "rate_limit"]])  # one free check, then nothing
        self.assertEqual(pool(snap, "github-actions")["state"], "unknown")
        valid(self, snap)

    def test_run_cmd_maps_rate_limit_text_to_rate_limited_and_other_403_to_auth(self):
        def fake(stderr):
            class P:
                returncode, stdout = 1, ""
            P.stderr = stderr
            return patch.object(fc.subprocess, "run", return_value=P)
        for text, code in (("gh: API rate limit exceeded for user ID 1. (HTTP 403)", "rate_limited"),
                           ("gh: You have exceeded a secondary rate limit (HTTP 403)", "rate_limited"),
                           ("gh: Resource not accessible (HTTP 403)", "auth"), ("boom", "unavailable")):
            with fake(text), self.assertRaises(fc.CmdError) as cm:
                fc.run_cmd(["gh", "api", "x"], 5)
            self.assertEqual(cm.exception.code, code, text)

    def test_bad_settings_are_not_configured(self):
        for bad in ({"owner": "../etc"}, {"owner": ""}, {"owner": None}, {"owner_type": "team"},
                    {"included_minutes": 0}, {"included_minutes": "2000"}, {"included_minutes": True}):
            runner = FakeRunner()
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, runner, config=make_config(("github_billing",), github_billing=bad))
            self.assertEqual(src(snap, "github_billing")["error_code"], "not_configured", bad)
            self.assertEqual(runner.calls, [], bad)  # nothing is run for a configuration that cannot work
        cfg = make_config(("github_billing",))
        del cfg["sources"]["github_billing"]["included_minutes"]
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(src(build(t, config=cfg), "github_billing")["error_code"], "not_configured")


class AnthropicCostsTests(unittest.TestCase):
    def costs(self, http=None, runner=None, config=None):
        with tempfile.TemporaryDirectory() as t:
            return build(t, runner, only=("anthropic_costs",), http=http or FakeHttp(), config=config)

    def test_a_get_with_the_admin_key_in_a_header_only(self):
        http = FakeHttp(costs=((200, cost_body(1.0, None, 2.5, has_more=True, next_page="p2")), (200, cost_body(100.0))))
        runner = FakeRunner()
        snap = self.costs(http, runner)
        calls = [c for c in http.calls if "cost_report" in c[1]]
        self.assertEqual(len(calls), 2)
        method, url, headers = calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith("https://api.anthropic.com/v1/organizations/cost_report?starting_at=2026-11-01T00:00:00Z"))
        self.assertIn("bucket_width=1d", url)
        self.assertIn("page=p2", calls[1][1])
        self.assertEqual((headers["x-api-key"], headers["anthropic-version"]), (ADMIN_KEY, "2023-06-01"))
        self.assertEqual(pool(snap, "anthropic-api")["used"], 104)  # 1.0 + 2.5 + 100.0 cents, rounded
        self.assertEqual(runner.calls, [])  # an env credential runs no command, and the key is in no argv
        self.assertNotIn(ADMIN_KEY, json.dumps(snap))

    def test_pool_is_month_to_date_cents_against_the_monthly_credit(self):
        snap = self.costs()
        p = pool(snap, "anthropic-api")
        self.assertEqual((p["kind"], p["unit"], p["used"], p["limit"], p["remaining"], p["source_id"]),
                         ("credit", "usd_cents", 1200, 20000, 18800, "anthropic_costs"))
        self.assertEqual(p["windows"], [{"label": "monthly", "used_pct": 6, "resets_at": "2026-12-01T00:00:00Z"}])
        self.assertEqual(p["state"], "healthy")
        valid(self, snap)

    def test_over_80_percent_advises_no_new_api_work_and_exactly_80_does_not(self):
        p = pool(self.costs(FakeHttp(costs=((200, cost_body(16001.0)),))), "anthropic-api")
        self.assertEqual((p["used"], p["remaining"], p["windows"][0]["used_pct"]), (16001, 3999, 80))
        self.assertTrue(p["advice"].startswith("Anthropic API nearly spent: no new API work until the next credit"))
        p = pool(self.costs(FakeHttp(costs=((200, cost_body(16000.0)),))), "anthropic-api")
        self.assertNotIn("nearly spent", p["advice"] or "")

    def test_spend_over_the_credit_clamps_remaining_at_zero(self):
        p = pool(self.costs(FakeHttp(costs=((200, cost_body(25000.5)),))), "anthropic-api")
        self.assertEqual((p["used"], p["limit"], p["remaining"], p["state"]), (25000, 20000, 0, "low"))
        self.assertEqual(p["windows"][0]["used_pct"], 100)

    def test_the_credit_amount_and_the_note_come_from_the_config(self):
        cfg = make_config(("anthropic_costs",), anthropic_costs={"monthly_credit_usd": 190})
        cfg["pool_notes"] = {"anthropic-api": "A budget I set myself"}
        p = pool(self.costs(config=cfg), "anthropic-api")
        self.assertEqual((p["limit"], p["remaining"], p["advice"]), (19000, 17800, "A budget I set myself"))

    def test_a_missing_or_invalid_credit_amount_is_an_unavailable_pool(self):
        for bad in (None, 0, -5, "lots"):
            cfg = make_config(("anthropic_costs",))
            if bad is None:
                del cfg["sources"]["anthropic_costs"]["monthly_credit_usd"]
            else:
                cfg["sources"]["anthropic_costs"]["monthly_credit_usd"] = bad
            http = FakeHttp()
            p = pool(self.costs(http, config=cfg), "anthropic-api")
            self.assertEqual(p["state"], "unknown", bad)
            self.assertIn("monthly_credit_usd", p["advice"])
            self.assertEqual(http.calls, [])

    def test_a_missing_key_is_an_unavailable_pool_not_a_failed_source(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("anthropic_costs",), env={})
        p = pool(snap, "anthropic-api")
        self.assertTrue(src(snap, "anthropic_costs")["ok"])
        self.assertEqual((p["state"], p["used"], p["limit"], p["remaining"]), ("unknown", None, None, None))
        self.assertEqual(p["advice"], "Unavailable: Anthropic admin key not configured")
        self.assertEqual(snap["asks"], [])  # not a rejection: no credential ask
        valid(self, snap)

    def test_a_401_is_unavailable_and_raises_a_credential_ask(self):
        snap = self.costs(FakeHttp(costs=((401, '{"error":"invalid x-api-key"}'),)))
        p = pool(snap, "anthropic-api")
        self.assertEqual((p["state"], p["remaining"]), ("unknown", None))
        self.assertEqual(p["advice"], "Unavailable: Anthropic admin key rejected (401/403)")
        ask = next(a for a in snap["asks"] if a["id"] == "anthropic-credential-rejected")
        self.assertEqual(ask["kind"], "at_desk")
        self.assertNotIn(ADMIN_KEY, json.dumps(snap))
        valid(self, snap)

    def test_other_failures_are_unavailable_without_an_ask(self):
        for status, reason in ((429, "rate limited"), (500, "unavailable")):
            snap = self.costs(FakeHttp(costs=((status, "{}"),)))
            p = pool(snap, "anthropic-api")
            self.assertEqual((p["state"], p["remaining"]), ("unknown", None))
            self.assertTrue(p["advice"].startswith("Unavailable: ") and reason in p["advice"])
            self.assertEqual(snap["asks"], [])
        self.assertIn("not understood", pool(self.costs(FakeHttp(costs=((200, "not json"),))), "anthropic-api")["advice"])


class OpenAICostsTests(unittest.TestCase):
    def costs(self, http=None, now=None, config=None, env=None):
        with tempfile.TemporaryDirectory() as t:
            return build(t, only=("openai_costs",), http=http or FakeHttp(), now=now, config=config, **({} if env is None else {"env": env}))

    def test_a_missing_key_is_an_unavailable_pool_not_a_crash(self):
        snap = self.costs(env={})
        p = pool(snap, "openai-api")
        self.assertTrue(src(snap, "openai_costs")["ok"])
        self.assertEqual((p["state"], p["used"], p["limit"], p["remaining"]), ("unknown", None, None, None))
        self.assertEqual(p["advice"], "Unavailable: OpenAI admin key not configured")
        valid(self, snap)

    def test_remaining_is_the_grant_minus_spend_since_the_grant_start(self):
        calls = []
        pages = {"p2": openai_body(90.0, 0.25), None: openai_body(12.5, has_more=True, next_page="p2")}

        def http(method, url, headers, body, timeout):
            calls.append((method, url, headers))
            return 200, pages["p2" if "page=p2" in url else None]
        snap = self.costs(http)
        p = pool(snap, "openai-api")
        self.assertEqual((p["used"], p["limit"], p["remaining"], p["unit"], p["kind"]), (10275, 12800, 2525, "usd_cents", "credit"))
        self.assertEqual(p["windows"], [{"label": "monthly", "used_pct": 80, "resets_at": "2026-11-28T04:59:00Z"}])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[0] == "GET" for c in calls))
        self.assertEqual(calls[0][2]["Authorization"], "Bearer " + OPENAI_KEY)
        # grant_start 2026-11-07 00:00 America/New_York (EST) = 05:00Z
        self.assertIn("start_time=" + str(int(fc.parse_utc("2026-11-07T05:00:00Z").timestamp())), calls[0][1])
        self.assertNotIn(OPENAI_KEY, json.dumps(snap))
        valid(self, snap)

    def test_spend_over_the_grant_clamps_remaining_at_zero(self):
        p = pool(self.costs(lambda *a: (200, openai_body(150.0))), "openai-api")
        self.assertEqual((p["used"], p["limit"], p["remaining"], p["state"]), (15000, 12800, 0, "low"))
        self.assertEqual(p["windows"][0]["used_pct"], 100)

    def test_the_expiring_unused_advice_threshold(self):
        def advice(day, spent):
            return pool(self.costs(lambda *a: (200, openai_body(spent)), now=fc.parse_utc(day)), "openai-api")["advice"]
        expiring = "Expiring unused credit: spend before 2026-11-27"
        self.assertEqual(advice("2026-11-20T12:00:00Z", 20.0), expiring)      # 7 days left, 84% unused
        self.assertEqual(advice("2026-11-14T12:00:00Z", 20.0), expiring)      # just under 14 days left
        self.assertNotEqual(advice("2026-11-12T12:00:00Z", 20.0), expiring)   # 15 days left: too early
        self.assertNotEqual(advice("2026-11-20T12:00:00Z", 100.0), expiring)  # 78% used: not unused
        self.assertNotEqual(advice("2026-11-20T12:00:00Z", 64.0), expiring)   # exactly 50% used: not over 50% unused

    def test_the_expiry_is_optional(self):
        cfg = make_config(("openai_costs",))
        del cfg["sources"]["openai_costs"]["expires"]
        p = pool(self.costs(lambda *a: (200, openai_body(20.0)), config=cfg), "openai-api")
        self.assertEqual((p["remaining"], p["windows"][0]["resets_at"], p["advice"]), (10800, None, None))

    def test_an_expired_grant_is_an_unavailable_pool(self):
        p = pool(self.costs(now=fc.parse_utc("2026-12-01T00:00:00Z")), "openai-api")
        self.assertEqual(p["state"], "unknown")
        self.assertIn("grant expired 2026-11-27", p["advice"])

    def test_bad_grant_settings_are_an_unavailable_pool_and_no_request(self):
        for change in ({"grant_usd": 0}, {"grant_usd": "x"}, {"grant_start": "yesterday"}, {"timezone": "Not/AZone"}):
            http = FakeHttp()
            p = pool(self.costs(http, config=make_config(("openai_costs",), openai_costs=change)), "openai-api")
            self.assertEqual(p["state"], "unknown", change)
            self.assertIn("missing or invalid", p["advice"])
            self.assertEqual(http.calls, [], change)

    def test_http_errors_and_garbage_become_unavailable_pools_with_a_reason(self):
        for status, reason in ((401, "rejected"), (429, "rate limited"), (500, "unavailable")):
            p = pool(self.costs(lambda *a, _s=status: (_s, "{}")), "openai-api")
            self.assertEqual((p["state"], p["remaining"]), ("unknown", None))
            self.assertTrue(p["advice"].startswith("Unavailable: ") and reason in p["advice"])
        self.assertIn("not understood", pool(self.costs(lambda *a: (200, "not json")), "openai-api")["advice"])


class ApiProbeTests(unittest.TestCase):
    def test_exhausted_forces_low_over_healthy_costs(self):
        http = FakeHttp(probe=((400, '{"error":{"message":"Your credit balance is too low to access the Anthropic API."}}'),))
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("anthropic_costs", "api_probe"), http=http)
        p = pool(snap, "anthropic-api")
        self.assertEqual((p["state"], p["source_id"], p["used"]), ("low", "anthropic_costs", 1200))  # cost figures kept
        self.assertEqual(p["advice"], RULES["advice"]["low"])

    def test_alone_it_makes_a_state_only_pool(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("api_probe",))
            low = build(t, only=("api_probe",), http=FakeHttp(probe=((400, "credit balance is too low"),)))
        p = pool(snap, "anthropic-api")
        self.assertEqual((p["state"], p["source_id"], p["used"], p["limit"]), ("healthy", "api_probe", None, None))
        self.assertEqual(pool(low, "anthropic-api")["state"], "low")
        valid(self, low)

    def test_the_request_is_the_free_count_tokens_endpoint_with_the_configured_model(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            build(t, only=("api_probe",), http=http)
            build(t, config=make_config(("api_probe",), api_probe={"model": "claude-test-model"}), http=http)
        method, url, headers = http.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.anthropic.com/v1/messages/count_tokens"))
        self.assertEqual(headers["x-api-key"], API_KEY)

    def test_it_runs_once_a_day(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            build(t, only=("anthropic_costs", "api_probe"), http=http, state_dir=state)
            for later in (fc.timedelta(hours=1), fc.timedelta(hours=23)):
                snap = build(t, only=("anthropic_costs", "api_probe"), http=http, state_dir=state, now=NOW + later)
                self.assertEqual(src(snap, "api_probe")["observed_at"], fc.fmt_utc(NOW))
                self.assertEqual(pool(snap, "anthropic-api")["observed_at"], fc.fmt_utc(NOW + later))  # costs are live each tick
            self.assertEqual(http.count("count_tokens"), 1)
            repolled = build(t, only=("anthropic_costs", "api_probe"), http=http, state_dir=state, now=NOW + fc.timedelta(hours=25))
            self.assertEqual(http.count("count_tokens"), 2)
            self.assertEqual(src(repolled, "api_probe")["observed_at"], fc.fmt_utc(NOW + fc.timedelta(hours=25)))

    def test_missing_cache_timestamps_cannot_authorize_reuse(self):
        http = FakeHttp()
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            for bad_ts in (None, "", "not-a-timestamp"):
                (state / fc.USAGE_STATE_FILE).write_text(json.dumps({
                    "claude_cloud": {"fetched_at": bad_ts, "credit": {"used_cents": 100, "limit_cents": 1000, "resets_at": "2026-11-05T07:59:00Z"}},
                    "api_probe": {"checked_at": bad_ts, "credit": "exhausted"}}))
                http.calls.clear()
                snap = build(t, only=("claude_cloud", "api_probe"), http=http, state_dir=state)
                self.assertEqual((http.count("oauth/usage"), http.count("count_tokens")), (1, 1), bad_ts)
                self.assertEqual(src(snap, "claude_cloud")["observed_at"], fc.fmt_utc(NOW))
                self.assertEqual(src(snap, "api_probe")["observed_at"], fc.fmt_utc(NOW))
                self.assertEqual(pool(snap, "anthropic-api")["state"], "healthy")  # the fresh 200, not the cached exhausted

    def test_eligibility_matrix_against_healthy_low_unavailable_and_absent_costs(self):
        for credit in ("available", "exhausted"):
            for age in (21600, 21601):
                probe_ts = fc.fmt_utc(NOW - fc.timedelta(seconds=age))
                for case in ("healthy", "low", "unavailable", "absent"):
                    with self.subTest(credit=credit, age=age, case=case), tempfile.TemporaryDirectory() as t:
                        state = Path(t) / "state"
                        state.mkdir()
                        (state / fc.USAGE_STATE_FILE).write_text(json.dumps({"api_probe": {"checked_at": probe_ts, "credit": credit}}))
                        http = FakeHttp(costs=((200, cost_body(19000.0 if case == "low" else 1200.0)),))
                        only = ("api_probe",) if case == "absent" else ("anthropic_costs", "api_probe")
                        env = {k: v for k, v in ENV.items() if not (case == "unavailable" and k.endswith("ADMIN_KEY"))}
                        snap = build(t, only=only, http=http, state_dir=state, env=env)
                        self.assertEqual(http.count("count_tokens"), 0)
                        self.assertEqual(src(snap, "api_probe")["observed_at"], probe_ts)
                        p = pool(snap, "anthropic-api")
                        valid(self, snap)
                        if case == "healthy":
                            want = "low" if (age == 21600 and credit == "exhausted") else "healthy"
                            self.assertEqual((p["state"], p["source_id"], p["used"], p["limit"], p["remaining"]),
                                             (want, "anthropic_costs", 1200, 20000, 18800))
                        elif case == "low":  # an available probe never upgrades a low pool
                            self.assertEqual((p["state"], p["used"], p["remaining"]), ("low", 19000, 1000))
                        elif case == "unavailable":  # the probe cannot invent figures or a state for an unavailable pool
                            self.assertEqual((p["state"], p["used"], p["remaining"]), ("unknown", None, None))
                            self.assertTrue(p["advice"].startswith("Unavailable: "))
                        elif age == 21600:  # the probe alone
                            want = "low" if credit == "exhausted" else "healthy"
                            self.assertEqual((p["state"], p["advice"], p["observed_at"], p["source_id"], p["used"], p["windows"]),
                                             (want, RULES["advice"][want], probe_ts, "api_probe", None, []))
                        else:
                            self.assertEqual((p["state"], p["advice"], p["observed_at"], p["source_id"], p["used"], p["windows"]),
                                             ("unknown", RULES["advice"]["stale"], probe_ts, "api_probe", None, []))

    def test_other_failures_are_not_reported_as_exhausted(self):
        for reply, code in (((400, "invalid request"), "unavailable"), ((401, "{}"), "unauthorized"), ((500, "oops"), "unavailable")):
            with tempfile.TemporaryDirectory() as t:
                snap = build(t, only=("anthropic_costs", "api_probe"), http=FakeHttp(probe=(reply,)))
            self.assertEqual(src(snap, "api_probe")["error_code"], code)
            self.assertEqual(pool(snap, "anthropic-api")["state"], "healthy")  # the cost report alone


if __name__ == "__main__":
    unittest.main()
