"""The local API spend ledger, and the collector source that reads it. No network."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_ledger as al  # noqa: E402
from helpers import NOW, ROOT, SCHEMA, build, fc, make_config, pool, src  # noqa: E402

LEDGER_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
API_RESPONSE = {"id": "msg_01", "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000, "cache_read_input_tokens": 2_000_000,
                          "cache_creation_input_tokens": 100_000}}


class LedgerTests(unittest.TestCase):
    def test_cost_from_published_prices(self):
        # sonnet-5-5: $2 in, $10 out per MTok: 2.00 + 5.00 + cache read 2M x $0.20 = 0.40 + write 0.1M x $2.50 = 0.25
        self.assertEqual(al.cost_units("claude-sonnet-5-5", 1_000_000, 500_000, 2_000_000, 100_000, 0), 765 * al.UNITS_PER_CENT)
        self.assertEqual(al.cost_units("claude-opus-5-5", 1_000_000, 0, 0, 0, 0), 400 * al.UNITS_PER_CENT)
        self.assertEqual(al.cost_units("claude-haiku-5-5", 0, 1_000_000, 0, 0, 1_000_000), 70 * al.UNITS_PER_CENT)

    def test_dated_model_ids_price_as_their_base_and_unknown_models_are_refused(self):
        self.assertEqual(al.canonical_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")
        self.assertEqual(al.canonical_model("claude-opus-5-5[1m]"), "claude-opus-5-5")
        with self.assertRaises(al.LedgerError):
            al.canonical_model("claude-mystery-9")

    def test_record_appends_events_and_rebuilds_the_month_summary(self):
        with tempfile.TemporaryDirectory() as t:
            s1 = al.record(t, API_RESPONSE, LEDGER_NOW)
            self.assertEqual((s1["month"], s1["spent_cents"], s1["basis"]), ("2026-11", 765, "local_events"))
            self.assertNotIn("cap_cents", s1)
            second = dict(API_RESPONSE, id="msg_02", usage={"input_tokens": 1, "output_tokens": 1})
            s2 = al.record(t, second, LEDGER_NOW)
            self.assertEqual(s2["spent_cents"], 766)  # rounds up: never under-reports
            self.assertEqual(len((Path(t) / al.EVENTS_FILE).read_text().splitlines()), 2)
            self.assertEqual(json.loads((Path(t) / al.SUMMARY_FILE).read_text()), s2)
            self.assertEqual(oct((Path(t) / al.SUMMARY_FILE).stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct((Path(t) / al.EVENTS_FILE).stat().st_mode & 0o777), "0o600")

    def test_a_duplicate_id_is_recorded_once_and_the_events_file_is_append_only(self):
        with tempfile.TemporaryDirectory() as t:
            al.record(t, API_RESPONSE, LEDGER_NOW)
            before = (Path(t) / al.EVENTS_FILE).read_text()
            al.record(t, API_RESPONSE, LEDGER_NOW)
            self.assertEqual((Path(t) / al.EVENTS_FILE).read_text(), before)
            al.record(t, dict(API_RESPONSE, id="msg_9"), LEDGER_NOW)
            self.assertTrue((Path(t) / al.EVENTS_FILE).read_text().startswith(before))

    def test_the_summary_is_month_scoped(self):
        with tempfile.TemporaryDirectory() as t:
            al.record(t, API_RESPONSE, datetime(2026, 10, 30, tzinfo=timezone.utc))
            s = al.record(t, dict(API_RESPONSE, id="msg_2", usage={"input_tokens": 1_000_000}), LEDGER_NOW)
            self.assertEqual(s["spent_cents"], 200)  # only November's $2.00
            december = al.summarise(al.read_events(Path(t) / al.EVENTS_FILE), datetime(2026, 12, 1, tzinfo=timezone.utc))
            self.assertEqual(december["spent_cents"], 0)

    def test_a_claude_cli_result_with_model_usage_is_priced_per_model(self):
        doc = {"session_id": "s1", "modelUsage": {
            "claude-haiku-4-5-20251001": {"inputTokens": 1_000_000, "outputTokens": 0, "cacheReadInputTokens": 0,
                                           "cacheCreationInputTokens": 0, "costUSD": 99},
            "claude-opus-5-5": {"inputTokens": 0, "outputTokens": 1_000_000}}}
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(al.record(t, doc, LEDGER_NOW)["spent_cents"], 100 + 2000)

    def test_unpriced_or_malformed_input_records_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            for bad in (dict(API_RESPONSE, model="claude-mystery-9"), {"usage": {}}, [], {"model": "x", "usage": {"input_tokens": -1}}):
                with self.assertRaises(al.LedgerError):
                    al.record(t, bad, LEDGER_NOW)
            self.assertFalse((Path(t) / al.EVENTS_FILE).exists())

    def test_the_cli_tee_passes_stdin_through_and_never_stores_content(self):
        with tempfile.TemporaryDirectory() as t:
            text = json.dumps(dict(API_RESPONSE, content=[{"type": "text", "text": "SECRET-BODY"}])) + "\n"
            out = io.StringIO()
            self.assertEqual(al.main(["record", "--state-dir", t, "--tee"], io.StringIO(text), out, LEDGER_NOW), 0)
            self.assertEqual(out.getvalue(), text)
            self.assertNotIn("SECRET-BODY", (Path(t) / al.EVENTS_FILE).read_text())
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(al.main(["record", "--state-dir", t], io.StringIO("not json"), io.StringIO(), LEDGER_NOW), 2)
            self.assertIn("not recorded", err.getvalue())

    def test_the_script_runs_isolated_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            proc = subprocess.run([sys.executable, "-I", str(ROOT / "api_ledger.py"), "record", "--state-dir", t, "--tee"],
                                  input=json.dumps(API_RESPONSE), capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["id"], "msg_01")
            self.assertEqual(json.loads((Path(t) / al.SUMMARY_FILE).read_text())["spent_cents"], 765)

    def test_the_default_state_dir_is_the_collectors(self):
        self.assertEqual(al.DEFAULT_STATE_DIR, fc.DEFAULT_STATE_DIR.replace("~", str(Path.home()), 1))


class LedgerSourceTests(unittest.TestCase):
    def build(self, t, state, **settings):
        config = make_config(("anthropic_ledger",), anthropic_ledger={"monthly_credit_usd": 200, **settings})
        return build(t, config=config, state_dir=state)

    def test_the_collector_reads_what_the_ledger_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            al.record(state, API_RESPONSE, NOW)
            snap = self.build(t, state)
        p = pool(snap, "anthropic-api-ledger")
        self.assertEqual((p["kind"], p["unit"], p["used"], p["limit"], p["remaining"], p["source_id"]),
                         ("credit", "usd_cents", 765, 20000, 19235, "anthropic_ledger"))
        self.assertEqual(p["windows"], [{"label": "monthly", "used_pct": 4, "resets_at": "2026-12-01T00:00:00Z"}])
        self.assertEqual((p["state"], p["observed_at"]), ("healthy", fc.fmt_utc(NOW)))
        self.assertTrue(src(snap, "anthropic_ledger")["ok"])
        self.assertEqual(fc.validate(snap, SCHEMA), [])

    def test_a_summary_for_an_earlier_month_means_nothing_spent_this_month(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            al.record(state, API_RESPONSE, datetime(2026, 10, 30, tzinfo=timezone.utc))
            p = pool(self.build(t, state), "anthropic-api-ledger")
        self.assertEqual((p["used"], p["remaining"], p["state"]), (0, 20000, "healthy"))

    def test_no_ledger_yet_is_not_configured_and_an_unknown_pool(self):
        with tempfile.TemporaryDirectory() as t:
            snap = self.build(t, Path(t) / "state")
        self.assertEqual((src(snap, "anthropic_ledger")["ok"], src(snap, "anthropic_ledger")["error_code"]), (False, "not_configured"))
        self.assertEqual(pool(snap, "anthropic-api-ledger")["state"], "unknown")

    def test_a_damaged_summary_is_a_parse_error(self):
        for text in ("not json", "[]", json.dumps({"month": "2026-11", "spent_cents": -1}),
                     json.dumps({"month": "2026-11", "spent_cents": "5"}), json.dumps({"month": "2026-11", "spent_cents": True})):
            with tempfile.TemporaryDirectory() as t:
                state = Path(t) / "state"
                state.mkdir()
                (state / al.SUMMARY_FILE).write_text(text)
                snap = self.build(t, state)
            self.assertEqual(src(snap, "anthropic_ledger")["error_code"], "parse_error", text)
            self.assertEqual(pool(snap, "anthropic-api-ledger")["state"], "unknown")

    def test_ledger_dir_overrides_the_state_dir(self):
        with tempfile.TemporaryDirectory() as t:
            elsewhere = Path(t) / "ledger"
            al.record(elsewhere, API_RESPONSE, NOW)
            p = pool(self.build(t, Path(t) / "state", ledger_dir=str(elsewhere)), "anthropic-api-ledger")
        self.assertEqual(p["used"], 765)

    def test_without_any_directory_it_is_not_configured(self):
        config = make_config(("anthropic_ledger",), anthropic_ledger={"monthly_credit_usd": 200})
        with tempfile.TemporaryDirectory() as t:
            snap = build(t, config=config)
        self.assertEqual(src(snap, "anthropic_ledger")["error_code"], "not_configured")

    def test_a_missing_credit_amount_is_an_unavailable_pool(self):
        for bad in (None, 0, "x"):
            config = make_config(("anthropic_ledger",), anthropic_ledger={"monthly_credit_usd": bad})
            with tempfile.TemporaryDirectory() as t:
                state = Path(t) / "state"
                al.record(state, API_RESPONSE, NOW)
                p = pool(build(t, config=config, state_dir=state), "anthropic-api-ledger")
            self.assertEqual(p["state"], "unknown", bad)
            self.assertIn("monthly_credit_usd", p["advice"])

    def test_over_80_percent_advises_no_new_api_work_and_the_pool_does_not_collide_with_the_cost_report(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "state"
            al.record(state, dict(API_RESPONSE, usage={"input_tokens": 100_000_000}), NOW)  # 100M tokens at $2 = $200
            config = make_config(("anthropic_ledger", "anthropic_costs"))
            config["sources"]["anthropic_ledger"] = {"monthly_credit_usd": 200}
            snap = build(t, config=config, state_dir=state)
        ledger, costs = pool(snap, "anthropic-api-ledger"), pool(snap, "anthropic-api")
        self.assertEqual((ledger["state"], ledger["remaining"]), ("low", 0))
        self.assertEqual(ledger["advice"], fc.ANTHROPIC_NEARLY_SPENT)
        self.assertEqual(costs["state"], "healthy")  # an independent pool from the cost report
        ids = [p["id"] for p in snap["pools"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(fc.validate(snap, SCHEMA), [])


if __name__ == "__main__":
    unittest.main()
