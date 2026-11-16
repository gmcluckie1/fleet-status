"""Snapshot-level behaviour: isolation, freshness, pace, the schema contract and the secret filter."""
import copy
import json
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (CODEX_RPC, FIX, MUSE, NO_USAGE, NOW, OAUTH_USAGE, RULES, SCHEMA, FakeHttp, FakeRunner,  # noqa: E402
                     build, codex_log, cost_body, fc, make_config, openai_body, pool, src)

FIXTURE = json.loads((FIX / "fleet-status-v1.json").read_text())


def valid(test, snap):
    test.assertEqual(fc.validate(snap, SCHEMA), [])


def with_source(fn, test_body):
    """Replace the muse source with ``fn`` for the duration of ``test_body``."""
    sources = [("muse", fn)] + [(sid, f) for sid, f in fc.SOURCES if sid != "muse"]
    with patch.object(fc, "SOURCES", sources):
        return test_body()


class IsolationTests(unittest.TestCase):
    def test_unknown_error_is_isolated_with_a_fixed_code(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(overrides={"muse": "not json at all"}), only=("muse",))
        self.assertEqual(src(snap, "muse")["error_code"], "parse_error")

    def test_an_unexpected_exception_in_a_source_is_contained(self):
        def boom(ctx, cfg):
            raise RuntimeError("secret detail that must not leak")
        with tempfile.TemporaryDirectory() as t:
            snap = with_source(boom, lambda: build(t, only=("muse", "agy")))
        self.assertEqual((src(snap, "muse")["ok"], src(snap, "muse")["error_code"]), (False, "unavailable"))
        self.assertNotIn("secret detail", json.dumps(snap))
        self.assertTrue(src(snap, "agy")["ok"])

    def test_no_carryover_from_a_previous_run(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            (state / "latest.json").write_text(json.dumps({"pools": [{"id": "muse", "used": 1}]}))
            snap = build(t, FakeRunner(fail={"muse": "timeout"}), only=("muse",), state_dir=state)
        self.assertEqual((pool(snap, "muse")["state"], pool(snap, "muse")["used"]), ("unknown", None))

    def test_real_runner_timeout_and_missing_binary(self):
        t0 = time.monotonic()
        with self.assertRaises(fc.CmdError) as cm:
            fc.run_cmd(["sleep", "5"], timeout=0.3)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 3)
        with self.assertRaises(fc.CmdError) as cm:
            fc.run_cmd(["definitely-not-a-binary-xyz"], timeout=1)
        self.assertEqual(cm.exception.code, "not_configured")
        with self.assertRaises(fc.CmdError) as cm:
            fc.run_cmd(["sh", "-c", "exit 127"], timeout=1)
        self.assertEqual(cm.exception.code, "not_configured")
        with self.assertRaises(fc.CmdError) as cm:
            fc.run_cmd(["false"], timeout=1)
        self.assertEqual(cm.exception.code, "unavailable")

    def test_the_source_wall_clock_timeout(self):
        with self.assertRaises(fc.CmdError) as cm:
            fc.with_timeout(lambda: time.sleep(2), 0.2)
        self.assertEqual(cm.exception.code, "timeout")

    def test_a_hung_source_is_reported_as_a_timeout_and_does_not_block_the_rest(self):
        def hang(ctx, cfg):
            time.sleep(30)
        config = make_config(("muse", "agy"), muse={"timeout_seconds": 1})
        with tempfile.TemporaryDirectory() as t:
            t0 = time.monotonic()
            snap = with_source(hang, lambda: build(t, config=config))
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 10)
        self.assertEqual((src(snap, "muse")["ok"], src(snap, "muse")["error_code"]), (False, "timeout"))
        self.assertTrue(src(snap, "agy")["ok"])
        self.assertEqual(pool(snap, "muse")["state"], "unknown")

    def test_a_transient_failure_is_retried_once(self):
        calls = []

        def flaky(ctx):
            calls.append(1)
            if len(calls) == 1:
                raise fc.CmdError("timeout")
            return {"observed_at": "x"}
        self.assertEqual(fc.run_source(flaky, None, 1), ({"observed_at": "x"}, None))
        self.assertEqual(len(calls), 2)

    def test_a_persistent_failure_retries_once_then_reports(self):
        calls = []

        def down(ctx):
            calls.append(1)
            raise fc.CmdError("unavailable")
        self.assertEqual(fc.run_source(down, None, 1), (None, "unavailable"))
        self.assertEqual(len(calls), 2)

    def test_parse_auth_and_not_configured_failures_are_not_retried(self):
        for code in ("parse", "auth", "stale", "not_configured"):
            calls = []

            def bad(ctx, code=code):
                calls.append(1)
                raise fc.CmdError(code)
            self.assertEqual(fc.run_source(bad, None, 1), (None, code))
            self.assertEqual(len(calls), 1, code)


class FreshnessTests(unittest.TestCase):
    def test_missing_producer_timestamps_keep_unknown_pools_and_a_null_source_time(self):
        muse_no_ts = copy.deepcopy(MUSE)
        del muse_no_ts["allowance"]["asOfMs"]
        with tempfile.TemporaryDirectory() as t:
            sessions = Path(t) / "sessions" / "2026" / "11" / "01"
            sessions.mkdir(parents=True)
            (sessions / "rollout-a.jsonl").write_text(json.dumps({"payload": {"rate_limits": {
                "primary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 1793774214},
                "credits": {"has_credits": True, "unlimited": False, "balance": "12345"}}}}) + "\n")
            snap = build(t, FakeRunner(overrides={"muse": json.dumps(muse_no_ts)}), only=("muse", "codex"))
        valid(self, snap)
        for sid in ("muse", "codex"):
            self.assertEqual((src(snap, sid)["ok"], src(snap, sid)["observed_at"], src(snap, sid)["error_code"]), (True, None, None), sid)
        for pid in ("muse", "codex", "codex-credits"):
            p = pool(snap, pid)
            self.assertEqual((p["state"], p["observed_at"], p["advice"], p["used"], p["limit"], p["remaining"],
                              p["pace_pct_per_hr"], p["runs_out_at"]),
                             ("unknown", None, RULES["advice"]["missing_timestamp"], None, None, None, None, None), pid)
            for w in p["windows"]:
                self.assertIsNone(w["used_pct"])
                self.assertIsNotNone(w["resets_at"])

    def test_the_limits_are_the_shipped_six_hours_with_no_exceptions(self):
        self.assertEqual(RULES["thresholds"]["freshness_exceptions"], [])
        self.assertEqual(fc.FRESHNESS_EXCEPTIONS, ())
        self.assertEqual(RULES["thresholds"]["max_age_seconds"], 21600)

    def test_every_usage_source_at_ages_21600_and_21601_and_prior_windows_never_restore_stale(self):
        daily_billing = {"usageItems": [{"date": f"2026-11-0{d}", "product": "actions", "unitType": "minutes", "quantity": 50} for d in (1, 2, 3)]}
        for age in (21600, 21601):
            obs_dt = NOW - fc.timedelta(seconds=age)
            obs_str = fc.fmt_utc(obs_dt)
            muse_doc = copy.deepcopy(MUSE)
            muse_doc["allowance"]["asOfMs"] = int(obs_dt.timestamp() * 1000)

            def http(method, url, headers, body, timeout):
                if "api.openai.com" in url:
                    return 200, openai_body(42.0)
                if "oauth/usage" in url:
                    return 200, OAUTH_USAGE
                if "cost_report" in url:
                    return 200, cost_body(1200.0)
                return 400, "credit balance is too low"

            with tempfile.TemporaryDirectory() as t:
                state = Path(t) / "state"
                state.mkdir()
                runner = FakeRunner(overrides={"muse": json.dumps(muse_doc), "billing_daily": json.dumps(daily_billing)})
                seed = fc.UsageState(state / fc.USAGE_STATE_FILE)
                future = fc.fmt_utc(NOW + fc.timedelta(days=3))
                seed.put("pool_windows", {pid: {"weekly": {"label": "weekly", "used_pct": 95, "resets_at": future},
                                                "5h": {"label": "5h", "used_pct": 95, "resets_at": future},
                                                "monthly": {"label": "monthly", "used_pct": 95, "resets_at": "2026-12-01T00:00:00Z"}}
                                          for pid in ("muse", "codex", "claude-subscription", "grok", "claude-cloud",
                                                      "github-actions", "anthropic-api", "openai-api")})
                seed.put("api_probe", {"checked_at": obs_str, "credit": "exhausted"})

                query_timed = {"codex", "agy", "colab", "claude_sub", "grok", "claude_cloud", "github_billing", "openai_costs", "anthropic_costs"}
                wrapped = []
                for sid, fn in fc.SOURCES:
                    if sid in query_timed:  # these read at query time: pretend the read happened `age` seconds ago
                        def _wrap(ctx, cfg, _fn=fn):
                            past = fc.Ctx(obs_dt, ctx.rules, ctx.config, ctx.runner, ctx.env, ctx.codex_rpc, ctx.grok_rpc,
                                          ctx.http, ctx.state, ctx.state_dir)
                            return _fn(past, cfg)
                        wrapped.append((sid, _wrap))
                    else:
                        wrapped.append((sid, fn))
                with patch.object(fc, "SOURCES", wrapped):
                    snap = build(t, runner, state_dir=state, codex_rpc=lambda argv, timeout: CODEX_RPC, http=http)
                valid(self, snap)
                expected = ("muse", "codex", "codex-credits", "codex-reset-credits",
                            "agy-account-a-gemini-models", "agy-account-a-claude-and-gpt-models", "agy-account-a-gemini-4-argon",
                            "agy-account-b-gemini-models", "agy-account-b-claude-and-gpt-models", "agy-account-b-gemini-4-argon",
                            "colab-account-a-units", "colab-account-b-units", "claude-subscription", "claude-fable", "grok",
                            "claude-cloud", "github-actions", "anthropic-api", "openai-api")
                for pid in expected:
                    p = pool(snap, pid)
                    self.assertIsNotNone(p, (age, pid))
                    self.assertEqual(p["observed_at"], obs_str, (age, pid))
                    if age == 21600:
                        self.assertNotEqual(p["state"], "unknown", (age, pid))
                        self.assertIsNotNone(p["remaining"], (age, pid))
                        for w in p["windows"]:
                            self.assertIsNotNone(w["used_pct"], (age, pid, w))
                    else:
                        self.assertEqual(p["state"], "unknown", (age, pid))
                        self.assertEqual(p["advice"], RULES["advice"]["stale"], (age, pid))
                        self.assertEqual((p["used"], p["limit"], p["remaining"], p["pace_pct_per_hr"], p["runs_out_at"]),
                                         (None, None, None, None, None), (age, pid))
                        for w in p["windows"]:
                            self.assertIsNone(w["used_pct"], (age, pid, w))
                            self.assertIsNotNone(w["resets_at"], (age, pid, w))

            # The session-log fallback, the held Muse weekly and a probe-only anthropic-api pool at the same ages.
            with tempfile.TemporaryDirectory() as t:
                state = Path(t) / "state"
                state.mkdir()
                fc.UsageState(state / fc.USAGE_STATE_FILE).put("muse_weekly", {"used_pct": 44, "resets_at": fc.fmt_utc(1793577600), "seen_at": obs_str})
                fc.UsageState(state / fc.USAGE_STATE_FILE).put("api_probe", {"checked_at": obs_str, "credit": "available"})
                codex_log(Path(t) / "sessions", limits_ts=obs_str, extra_old=False)
                snap = build(t, FakeRunner(overrides={"muse": NO_USAGE}), only=("muse", "codex", "api_probe"), state_dir=state)
                valid(self, snap)
                for pid in ("muse", "codex", "codex-credits", "anthropic-api"):
                    p = pool(snap, pid)
                    self.assertEqual(p["observed_at"], obs_str, (age, pid))
                    if age == 21600:
                        self.assertNotEqual(p["state"], "unknown", (age, pid))
                    else:
                        self.assertEqual((p["state"], p["advice"], p["used"], p["limit"], p["remaining"]),
                                         ("unknown", RULES["advice"]["stale"], None, None, None), (age, pid))

    def test_two_ticks_dispatch_every_source_and_reuse_caches_without_extra_requests(self):
        counts = Counter()
        wrapped = []
        for sid, fn in fc.SOURCES:
            def _count(ctx, cfg, _sid=sid, _fn=fn):
                counts[_sid] += 1
                return _fn(ctx, cfg)
            wrapped.append((sid, _count))
        http, runner = FakeHttp(), FakeRunner()
        with tempfile.TemporaryDirectory() as t, patch.object(fc, "SOURCES", wrapped):
            state = Path(t) / "state"
            state.mkdir()
            snap1 = build(t, runner, http=http, state_dir=state)
            snap2 = build(t, runner, http=http, state_dir=state, now=NOW + fc.timedelta(minutes=5))
        enabled = [sid for sid, _ in fc.enabled_sources(make_config())]
        self.assertEqual(sorted(counts), sorted(enabled))
        self.assertTrue(all(v == 2 for v in counts.values()), dict(counts))
        self.assertEqual((http.count("oauth/usage"), http.count("count_tokens")), (1, 1))
        self.assertEqual(len([c for c in runner.calls if "agy" in c]), 4)  # two accounts, two ticks, no cross-tick cache
        self.assertEqual(src(snap2, "claude_cloud")["observed_at"], fc.fmt_utc(NOW))
        self.assertEqual(pool(snap2, "claude-cloud")["observed_at"], fc.fmt_utc(NOW))
        self.assertEqual(src(snap2, "api_probe")["observed_at"], fc.fmt_utc(NOW))
        for snap in (snap1, snap2):
            valid(self, snap)


class RulesTests(unittest.TestCase):
    def test_state_thresholds(self):
        far = fc.fmt_utc(NOW.timestamp() + 20 * 86400)
        soon = fc.fmt_utc(NOW.timestamp() + 3600)

        def state(used, resets):
            return fc.pool_state(used, resets, NOW, RULES)
        self.assertEqual(state(95, far), "low")
        self.assertEqual(state(100 - RULES["thresholds"]["low_remaining_pct"], far), "low")
        self.assertEqual(state(75, far), "watch")
        self.assertEqual(state(10, far), "healthy")
        self.assertEqual(state(10, soon), "spend_first")  # lots left and about to reset
        self.assertEqual(state(95, soon), "low")          # nothing left: never spend_first
        self.assertEqual(state(None, far), "unknown")

    def test_advice_comes_from_the_rules(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex", "grok"))
            custom = {**RULES, "advice": {**RULES["advice"], "low": "Custom low advice"}}
            changed = build(t, only=("codex", "grok"), rules=custom)
        self.assertEqual(pool(snap, "codex")["advice"], RULES["advice"]["low"])
        self.assertIsNone(pool(snap, "grok")["advice"])
        self.assertEqual(pool(changed, "codex")["advice"], "Custom low advice")

    def test_thresholds_come_from_the_rules(self):
        custom = {**RULES, "thresholds": {**RULES["thresholds"], "codex_credits_low_below": 5000}}
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("codex",), codex_rpc=lambda argv, timeout: CODEX_RPC, rules=custom)
        self.assertEqual(pool(snap, "codex-credits")["state"], "low")  # 1234 credits is below 5000 now

    def test_resets_are_sorted_and_future_only(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t)
        ats = [r["at"] for r in snap["resets"]]
        self.assertEqual(ats, sorted(ats))
        self.assertTrue(ats and all(fc.parse_utc(a) > NOW for a in ats))
        self.assertIn("muse", [r["pool_id"] for r in snap["resets"]])
        self.assertTrue(all(r["kind"] == "reset" for r in snap["resets"]))

    def test_rules_json_has_the_required_keys_and_a_label_for_every_fixed_pool(self):
        for key in ("version", "thresholds", "advice", "pools"):
            self.assertIn(key, RULES)
        for state in ("low", "watch", "spend_first", "healthy", "unmetered", "unknown", "stale", "missing_timestamp"):
            self.assertIn(state, RULES["advice"])
        for pids in fc.FALLBACK_POOLS.values():
            for pid, _kind, _unit in pids:
                self.assertIn(pid, RULES["pools"])
        for pid in ("codex-credits", "codex-reset-credits"):
            self.assertIn(pid, RULES["pools"])

    def test_no_pool_is_ever_unmetered(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t)
        self.assertNotIn("unmetered", [p["state"] for p in snap["pools"]])


class PaceAndMergeTests(unittest.TestCase):
    """Ported from github.com/dhitalkamal/clalt pacemath_test.go and limits_test.go (MIT, Copyright (c) 2026 Kamal Dhital)."""

    def test_pace_fill_clalt_fixtures(self):
        reset = fc.parse_utc("2026-04-06T15:00:00Z")
        win = fc.timedelta(hours=5)
        start = reset - win  # 10:00Z
        cases = [
            ("halfway through 5h at 50% hits 100% at reset", 50.0, reset, start + fc.timedelta(minutes=150), win, True, 20.0, reset),
            ("fast burn hits 100% before reset", 60.0, reset, start + fc.timedelta(hours=1), win, True, 60.0, start + fc.timedelta(hours=1, minutes=40)),
            ("slow burn hits 100% after reset", 10.0, reset, start + fc.timedelta(minutes=150), win, True, 4.0, start + fc.timedelta(hours=25)),
            ("too early in window (<5% elapsed) is suppressed", 10.0, reset, start + fc.timedelta(minutes=10), win, False, 0.0, None),
            ("zero percent has no projection", 0.0, reset, start + fc.timedelta(hours=2), win, False, 0.0, None),
            ("missing reset timestamp", 30.0, None, start + fc.timedelta(hours=2), win, False, 0.0, None),
            ("already at 100% returns observed as fullAt", 100.0, reset, start + fc.timedelta(hours=2), win, True, 50.0, start + fc.timedelta(hours=2)),
            ("observed after reset is rejected (elapsed > length)", 40.0, reset, reset + fc.timedelta(minutes=10), win, False, 0.0, None),
            ("7d window math uses 7d length", 50.0, reset, reset - fc.timedelta(hours=84), fc.timedelta(days=7), True, 50.0 / 84.0, reset),
        ]
        for name, pct, r_at, obs, length, want_ok, want_rate, want_full in cases:
            with self.subTest(name=name):
                rate, full_at, ok = fc.pace_fill(pct, r_at, obs, length)
                self.assertEqual(ok, want_ok)
                if want_ok:
                    self.assertAlmostEqual(rate, want_rate, delta=0.05)
                    self.assertLessEqual(abs((full_at - want_full).total_seconds()), 1.0)
                else:
                    self.assertIsNone(full_at)

    def test_merge_window_clalt_fixtures(self):
        base = fc.parse_utc("2026-04-06T12:00:00Z")

        def w(pct, offset):
            return {"label": "5h", "used_pct": pct, "resets_at": fc.fmt_utc(base + offset)}

        h = fc.timedelta
        cases = [
            ("first observation kept", None, w(30, h(hours=2)), w(30, h(hours=2))),
            ("higher pct within same window replaces", w(30, h(hours=2)), w(45, h(hours=2)), w(45, h(hours=2))),
            ("lower pct within same window ignored (out-of-order read)", w(45, h(hours=2)), w(30, h(hours=2)), w(45, h(hours=2))),
            ("reset timestamp jitter within 60s treated as same window", w(45, h(hours=2)), w(30, h(hours=2, seconds=30)), w(45, h(hours=2))),
            ("new window (resetsAt jumps forward) accepts lower pct", w(80, h(hours=1)), w(5, h(hours=5)), w(5, h(hours=5))),
            ("expired old window dropped, new obs kept", w(90, -h(minutes=1)), w(10, h(hours=5)), w(10, h(hours=5))),
            ("expired old window dropped, nil obs yields nil", w(90, -h(minutes=1)), None, None),
            ("expired obs ignored, valid old kept", w(40, h(hours=2)), w(99, -h(seconds=10)), w(40, h(hours=2))),
            ("obs with earlier resetsAt beyond tolerance ignored", w(20, h(hours=5)), w(95, h(hours=1)), w(20, h(hours=5))),
        ]
        for name, old_w, obs_w, want in cases:
            with self.subTest(name=name):
                self.assertEqual(fc.merge_window(old_w, obs_w, base), want)

    def test_pool_pace_projection_and_monotonic_state_across_runs(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            state.mkdir()
            # 1h elapsed in a 5h window (resets in 4h) at 60% -> 60%/hr, runs out 40 minutes after NOW (before the reset)
            resets_5h = int((NOW + fc.timedelta(hours=4)).timestamp())
            resets_wk = int((NOW + fc.timedelta(days=5)).timestamp())
            rpc_fast = copy.deepcopy(CODEX_RPC)
            rpc_fast["rateLimits"]["primary"] = {"usedPercent": 60, "windowDurationMins": 300, "resetsAt": resets_5h}
            rpc_fast["rateLimits"]["secondary"] = {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": resets_wk}
            rpc_fast["rateLimits"]["rateLimitReachedType"] = None
            snap1 = build(t, only=("codex",), state_dir=state, codex_rpc=lambda argv, timeout: rpc_fast)
            p1 = pool(snap1, "codex")
            self.assertEqual(p1["pace_pct_per_hr"], 60)
            self.assertEqual(p1["runs_out_at"], fc.fmt_utc(NOW + fc.timedelta(minutes=40)))
            valid(self, snap1)

            # An out-of-order lower reading (35%) with 20 s of reset jitter in the same window stays monotonic at 60%.
            stale_low = copy.deepcopy(rpc_fast)
            stale_low["rateLimits"]["primary"] = {"usedPercent": 35, "windowDurationMins": 300, "resetsAt": resets_5h + 20}
            p2 = pool(build(t, only=("codex",), state_dir=state, codex_rpc=lambda argv, timeout: stale_low), "codex")
            self.assertEqual((p2["windows"][0]["used_pct"], p2["pace_pct_per_hr"]), (60, 60))
            self.assertEqual(p2["runs_out_at"], fc.fmt_utc(NOW + fc.timedelta(minutes=40)))

            # A slow burn (10% after 2.5h of a 5h window, 4%/hr) resets before 100%, so runs_out_at is None.
            new_window = copy.deepcopy(rpc_fast)
            new_window["rateLimits"]["primary"] = {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": resets_5h + 3600}
            snap3 = build(t, only=("codex",), state_dir=state, now=NOW + fc.timedelta(minutes=150),
                          codex_rpc=lambda argv, timeout: new_window)
            p3 = pool(snap3, "codex")
            self.assertEqual((p3["windows"][0]["used_pct"], p3["pace_pct_per_hr"], p3["runs_out_at"]), (10, 4, None))
            valid(self, snap3)


class AskTests(unittest.TestCase):
    def ask(self, n, kind="at_desk", since="2026-11-01T00:00:00Z", title=None, detail="detail"):
        return {"id": f"ask-{n}", "title": title or f"Ask {n}", "detail": detail, "kind": kind, "since": since, "link_ref": None}

    def snapshot_with(self, asks):
        def source(ctx, cfg):
            return {"observed_at": fc.fmt_utc(ctx.now), "pools": [], "asks": asks}
        with tempfile.TemporaryDirectory() as t:
            return with_source(source, lambda: build(t, only=("muse",)))

    def test_sorted_by_kind_then_since_then_id_whatever_the_input_order(self):
        asks = [self.ask(3, "deferred"), self.ask(2, "at_desk", "2026-11-02T00:00:00Z"), self.ask(1, "confirm"),
                self.ask(5, "at_desk", "2026-11-01T00:00:00Z"), self.ask(4, "at_desk", "2026-11-01T00:00:00Z")]
        for order in (asks, list(reversed(asks))):
            snap = self.snapshot_with(order)
            self.assertEqual([a["id"] for a in snap["asks"]], ["ask-1", "ask-4", "ask-5", "ask-2", "ask-3"])
            valid(self, snap)

    def test_more_than_the_cap_are_counted_not_silently_dropped(self):
        snap = self.snapshot_with([self.ask(n, "deferred") for n in range(25)] + [self.ask(99, "confirm")])
        self.assertEqual(len(snap["asks"]), fc.CAPS["asks"])
        self.assertEqual(snap["asks"][0]["id"], "ask-99")  # the most urgent survives the cut
        self.assertEqual((snap["truncated"], snap["omitted"]), (True, 6))
        valid(self, snap)

    def test_duplicates_blank_titles_and_secrets_are_dropped_and_counted(self):
        token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        asks = [self.ask(1), self.ask(1, title="same id again"), self.ask(2, title="   "), self.ask(3, title="oops " + token),
                self.ask(4, detail="see /Users/someone/x")]
        snap = self.snapshot_with(asks)
        self.assertEqual([a["id"] for a in snap["asks"]], ["ask-1"])
        self.assertEqual((snap["truncated"], snap["omitted"]), (True, 4))
        self.assertNotIn(token, json.dumps(snap))
        valid(self, snap)

    def test_the_shipped_asks_are_the_shared_login_and_the_rejected_admin_key_only(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, FakeRunner(), http=FakeHttp(costs=((401, "{}"),)))
        self.assertEqual({a["id"] for a in snap["asks"]}, {"anthropic-credential-rejected"})


class ContractTests(unittest.TestCase):
    def mutated(self, fn):
        snap = copy.deepcopy(FIXTURE)
        fn(snap)
        return fc.validate(snap, SCHEMA)

    def test_the_reference_fixture_validates(self):
        self.assertEqual(fc.validate(FIXTURE, SCHEMA), [])

    def test_a_full_snapshot_validates(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t)
        valid(self, snap)
        self.assertEqual(snap["schema"], "fleet-status/v1")
        self.assertEqual((snap["work"], snap["landed"], snap["landed_count_today"], snap["closed_wp_today"]), ([], [], 0, 0))
        self.assertNotIn("lanes", snap)
        self.assertNotIn("coordinator", snap)

    def test_the_schema_file_is_self_consistent(self):
        self.assertEqual(SCHEMA["title"], "fleet-status/v1")
        for name in ("pools", "asks", "resets", "sources"):
            self.assertEqual(SCHEMA["properties"][name]["maxItems"], 20)
        self.assertEqual(fc.CAPS["pools"], SCHEMA["properties"]["pools"]["maxItems"])
        self.assertEqual(fc.CAPS["asks"], SCHEMA["properties"]["asks"]["maxItems"])
        self.assertEqual(fc.CAPS["resets"], SCHEMA["properties"]["resets"]["maxItems"])
        self.assertTrue(set(SCHEMA["required"]) <= set(SCHEMA["properties"]))

    def test_the_pool_cap_is_enforced_and_counted(self):
        accounts = [{"label": f"a{n}"} for n in range(10)]
        config = make_config(("agy",), agy={"accounts": accounts})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, config=config)
        self.assertEqual(len(snap["pools"]), 20)  # 10 accounts x 3 groups = 30 pools, 10 beyond the cap
        self.assertEqual(snap["truncated"], True)
        self.assertGreaterEqual(snap["omitted"], 10)
        self.assertEqual({r["pool_id"] for r in snap["resets"]} - {p["id"] for p in snap["pools"]}, set())  # no reset for a dropped pool
        valid(self, snap)

    def test_unknown_top_level_keys_enums_and_caps_are_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("muse",))
        snap["extra"] = 1
        snap["pools"][0]["state"] = "great"
        snap["asks"] = [{"id": "a", "title": "t", "detail": "d", "kind": "confirm", "since": None, "link_ref": None}] * 21
        errors = " ".join(fc.validate(snap, SCHEMA))
        for word in ("extra", "state", "asks"):
            self.assertIn(word, errors)

    def test_validator_honours_ref_and_oneof_branches(self):
        def used_string(s):
            s["pools"][0]["used"] = "12"
        self.assertTrue(self.mutated(used_string))

        def used_null(s):
            s["pools"][0]["used"] = None
        self.assertEqual(self.mutated(used_null), [])

        def bad_ref(s):
            s["work"][0]["ref"] = "pr:abc"
        self.assertTrue(self.mutated(bad_ref))

    def test_validator_honours_minlength_format_and_bounds(self):
        def empty_detail(s):
            s["asks"][0]["detail"] = ""
        self.assertTrue(self.mutated(empty_detail))
        for bad in ("2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-11-01T24:00:00Z", "2026-11-01T12:00:00+00:00"):
            def stamp(s, bad=bad):
                s["generated_at"] = bad
            self.assertTrue(self.mutated(stamp), bad)

        def huge(s):
            s["landed_count_today"] = 1_000_000_001
        self.assertTrue(self.mutated(huge))

        def boolean_count(s):
            s["omitted"] = True
        self.assertTrue(self.mutated(boolean_count))

        def trailing_newline(s):
            s["collector_id"] = "my-machine\n"
        self.assertTrue(self.mutated(trailing_newline))

        def too_many_resets(s):
            s["resets"] = [s["resets"][0]] * 21
        self.assertTrue(self.mutated(too_many_resets))

    def test_whitespace_matches_javascript_semantics(self):
        # U+FEFF is whitespace to JavaScript's \s but not to Python's: it must not slip past a text pattern.
        def bom_title(s):
            s["asks"][0]["title"] = "\ufeffConfirm it"
        self.assertTrue(self.mutated(bom_title))
        self.assertEqual(fc.clean("\ufeff  hello \ufeff"), "hello")
        self.assertEqual(fc.clean("a" * 199 + "\ufeffz", 200), "a" * 199)
        self.assertEqual(fc.clean("a" * 199 + " tail", 200), "a" * 199)  # a truncated text never ends in whitespace

    def test_unsupported_keyword_type_format_or_ref_fails_closed(self):
        with self.assertRaises(fc.SchemaError):
            fc.validate({"a": 1}, {"type": "object", "allOf": []})
        with self.assertRaises(fc.SchemaError):
            fc.validate("x", {"$ref": "#/$defs/missing", "$defs": {}})
        with self.assertRaises(fc.SchemaError):
            fc.validate("x", {"type": "string", "format": "email"})
        with self.assertRaises(fc.SchemaError):
            fc.validate(1, {"type": "number"})

    def test_oneof_requires_exactly_one_branch(self):
        branches = [{"type": "string"}, {"type": "string", "minLength": 2}]
        self.assertNotEqual(fc.validate("ab", {"oneOf": branches, "$defs": {}}), [])
        self.assertEqual(fc.validate("a", {"oneOf": branches, "$defs": {}}), [])

    def test_agrees_with_the_reference_validator_when_installed(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        checker = jsonschema.FormatChecker()

        @checker.checks("fleet-timestamp")
        def _ts(value):
            return not isinstance(value, str) or fc.valid_timestamp(value)

        @checker.checks("coordinator-timestamp")
        def _cts(value):
            return not isinstance(value, str) or fc.valid_coordinator_timestamp(value)

        reference = jsonschema.Draft202012Validator(SCHEMA, format_checker=checker)
        cases = []
        for key, val in [("generated_at", "2026-02-30T00:00:00Z"), ("generated_at", 5), ("collector_id", "UPPER"),
                         ("landed_count_today", -1), ("landed_count_today", 1.5), ("truncated", "no"), ("extra", 1),
                         ("sources", [{"id": "x", "ok": True, "observed_at": None, "error_code": "auth"}])]:
            snap = copy.deepcopy(FIXTURE)
            snap[key] = val
            cases.append(snap)
        for field, val in (("used", "1"), ("state", "great"), ("advice", ""), ("label", " x")):
            snap = copy.deepcopy(FIXTURE)
            snap["pools"][0][field] = val
            cases.append(snap)
        cases.append(copy.deepcopy(FIXTURE))
        with tempfile.TemporaryDirectory() as t:
            cases.append(build(t))
        for snap in cases:
            self.assertEqual(not fc.validate(snap, SCHEMA), reference.is_valid(snap), snap)


class SecretFilterTests(unittest.TestCase):
    # Synthetic, assembled at runtime so no scanner sees a literal credential in this file.
    SAMPLES = {
        "quoted json password": '"pass' + 'word": "hunter2hunter2"',
        "quoted json short password": '"pass' + 'word":"abc"',
        "single quoted token": "'tok" + "en': 'abcdefghijkl'",
        "escaped quoted password": 'say \\"pass' + 'word\\": \\"hunter2hunter2\\"',
        "google api key": "AI" + "za" + "A" * 35,
        "google oauth": "ya" + "29." + "a0Af" * 10,
        "google refresh": "1/" + "/0g" + "A" * 40,
        "google client secret": "GOCS" + "PX-" + "A" * 28,
        "cloudflare access": "cf" + "ast_" + "A" * 20,
        "cloudflare user token": "cf" + "ut_" + "A" * 40,
        "cf authorization header": "CF-Access-Client-Secret: " + "a" * 20,
        "env style password": "DB_PASS" + "WORD=hunter2hunter2",
        "access token env": "access_tok" + "en=abcdefghijkl",
        "quoted refresh token": '"refresh_tok' + 'en": "abcdefghijkl"',
        "bearer": "Bear" + "er " + "abcdefghijklmnop",
        "github token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "private key header": "-----BEGIN " + "PRIVATE KEY-----",
        "aws key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "jwt": "eyJhbGciOi" + ".eyJzdWIiOi" + ".abc",
        "long hex": "ab" * 24,
        "url credentials": "https://user:" + "pw@example.com/x",
        "tilde path": "see ~/secrets/x",
        "bare tilde": "cd ~/",
        "users path": "/Users/someone/github",
        "users path mid": "in /Users/someone",
        "home path": "/home/alice/x",
        "windows path": "C:\\Users\\someone",
        "windows fwd": "D:/work/x",
        "email": "someone@example.com",
    }
    # Key-based: a credential-named key followed by : or = flags the record whatever the value looks like
    # (spaces, one-letter words, quotes of either kind, escaped quotes, any case, any key spelling).
    ASSIGNMENTS = {
        "multi-space short words": '"pass' + 'word": "ab cd ef"',
        "single quoted spaces": "pass" + "word='x y'",
        "spaced equals uppercase": 'PASS' + 'WORD = "a b"',
        "unquoted spaces": "pass" + "word: ab cd ef",
        "escaped quoted spaces": '\\"pass' + 'word\\": \\"a b c\\"',
        "secret": '"sec' + 'ret": "a b"',
        "token": "tok" + "en = 'q r'",
        "api_key": "api" + "_key: a b",
        "apikey": '"api' + 'key":"a b"',
        "api-key": "API" + "-KEY=a b",
        "private_key": "private" + "_key: 'a b'",
        "client_secret": '"client' + '_secret": "a b"',
        "prefixed key": "DB_pass" + "word=a b",
        "passwd": "pass" + "wd: a b",
        "empty value": "pass" + "word:",
    }

    def test_each_sample_is_flagged(self):
        for name, text in {**self.SAMPLES, **self.ASSIGNMENTS}.items():
            self.assertTrue(fc.has_secret(text), name)
            self.assertTrue(fc.record_has_secret({"title": "ok " + text + " tail"}), name)

    def test_credential_words_without_an_assignment_are_not_flagged(self):
        for good in ("rotate the password policy", "feat: secret scanning docs", "max tokens reached", "tokens: 5",
                     "api key rotation plan", "private keys are hard", "secrets: none"):
            self.assertFalse(fc.has_secret(good), good)

    def test_ordinary_text_is_not_flagged(self):
        for good in ("Nearly exhausted: route work to another provider until the next reset", "Claude subscription",
                     "agy-account-a-gemini-models (weekly)", "Colab compute units (account-a)", "a/b and c/d", "pr:42",
                     "Unavailable: the source timed out", "Expiring unused credit: spend before 2026-11-27"):
            self.assertFalse(fc.has_secret(good), good)

    def test_every_sample_in_an_ask_never_reaches_the_body(self):
        def source(ctx, cfg):
            asks = [{"id": f"ask-{n}", "title": "x " + text, "detail": "d", "kind": "at_desk", "since": None, "link_ref": None}
                    for n, text in enumerate({**self.SAMPLES, **self.ASSIGNMENTS}.values())]
            return {"observed_at": fc.fmt_utc(ctx.now), "pools": [], "asks": asks}
        with tempfile.TemporaryDirectory() as t:
            snap = with_source(source, lambda: build(t, only=("muse",)))
        self.assertEqual(snap["asks"], [])
        self.assertEqual(snap["omitted"], len(self.SAMPLES) + len(self.ASSIGNMENTS))

    def test_the_final_body_scan_walks_parsed_strings_and_refuses(self):
        with self.assertRaises(fc.OutputRefused):
            fc.encode_body({"schema": "fleet-status/v1", "x": 'a "pass' + 'word": "hunter2hunter2"'})
        with self.assertRaises(fc.OutputRefused):
            fc.encode_body({"nested": [{"k": "/Users/someone/x"}]})

    def test_a_secret_in_a_pool_field_refuses_the_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            config = make_config(("muse",))
            config["pool_notes"] = {"muse": "token=" + "ab" * 12}
            snap = build(t, config=config)
        with self.assertRaises(fc.OutputRefused):
            fc.encode_body(snap)

    def test_the_body_size_cap(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t)
        body = fc.encode_body(snap)
        self.assertLess(len(body), fc.MAX_BODY_BYTES)
        with self.assertRaises(fc.OutputRefused):
            fc.encode_body(snap, max_bytes=100)

    def test_compact_and_pretty_encodings_carry_the_same_document(self):
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, only=("muse",))
        self.assertEqual(json.loads(fc.encode_body(snap)), json.loads(fc.encode_body(snap, pretty=True)))
        self.assertEqual(fc.encode_body(snap), fc.encode_body(snap))  # sorted keys: deterministic


if __name__ == "__main__":
    unittest.main()
