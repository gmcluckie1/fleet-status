"""Shared test plumbing: loads collect.py, fakes every CLI and HTTP call, and builds snapshots from fixtures.

Nothing here touches the network, a real CLI, an account or the user's home directory.
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
spec = importlib.util.spec_from_file_location("fleet_collect", ROOT / "collect.py")
fc = importlib.util.module_from_spec(spec)
sys.modules["fleet_collect"] = fc
spec.loader.exec_module(fc)

NOW = fc.parse_utc("2026-11-01T21:30:00Z")
RULES = json.loads((ROOT / "rules.json").read_text())
SCHEMA = json.loads((ROOT / "fleet-status-v1.schema.json").read_text())

# Sentinel credential values: they must reach the (fake) HTTP layer and nothing else.
OAUTH_TOKEN = "sentinel-oauth-token-value-0123456789"
API_KEY = "sentinel-api-key-value-0123456789"
ADMIN_KEY = "sentinel-admin-key-value-0123456789"
OPENAI_KEY = "sentinel-openai-key-value-0123456789"
SECRETS = (OAUTH_TOKEN, API_KEY, ADMIN_KEY, OPENAI_KEY)
ENV = {"FLEET_STATUS_CLAUDE_OAUTH_TOKEN": OAUTH_TOKEN, "FLEET_STATUS_ANTHROPIC_API_KEY": API_KEY,
       "FLEET_STATUS_ANTHROPIC_ADMIN_KEY": ADMIN_KEY, "FLEET_STATUS_OPENAI_ADMIN_KEY": OPENAI_KEY}


def load(name):
    return (FIX / name).read_text()


MUSE = json.loads(load("muse-usage.json"))
BILLING = json.loads(load("github-billing-summary.json"))
AGY = load("agy-usage.json")
CLAUDE = load("claude-usage.json")
CODEX_RPC = json.loads(load("codex-rate-limits.json"))
GROK = json.loads(load("grok-billing.json"))["result"]
OAUTH_USAGE = load("claude-oauth-usage.json")
COLAB_USAGE = load("colab-usage.txt")
NO_USAGE = json.dumps({"schemaVersion": 1, "allowance": None, "reason": "server_sent_no_usage", "hint": None})

# Every source, enabled, with placeholder settings. Individual tests narrow or change it with ``build``.
CONFIG = {
    "collector_id": "test-collector",
    "state_dir": None,
    "pool_notes": {},
    "sources": {
        "muse": {},
        "codex": {},
        "agy": {"accounts": [{"label": "account-a", "home": "/fixture/agy-a"}, {"label": "account-b", "home": "/fixture/agy-b"}]},
        "colab": {"accounts": [{"label": "account-a", "expect_email": "a@example.test"},
                               {"label": "account-b", "command": ["colab-b"]}]},
        "claude_sub": {},
        "grok": {},
        "claude_cloud": {},
        "github_billing": {"owner": "example-owner", "owner_type": "user", "included_minutes": 2000},
        "anthropic_costs": {"monthly_credit_usd": 200},
        "openai_costs": {"grant_usd": 128, "grant_start": "2026-11-07", "expires": "2026-11-27T23:59",
                         "timezone": "America/New_York"},
        "api_probe": {},
    },
}


def make_config(only=None, **overrides):
    """CONFIG restricted to ``only`` (ids), with per-source settings merged from ``overrides``."""
    config = copy.deepcopy(CONFIG)
    if only is not None:
        config["sources"] = {sid: cfg for sid, cfg in config["sources"].items() if sid in only}
    for sid, extra in overrides.items():
        config["sources"].setdefault(sid, {}).update(extra)
    return config


def codex_log(dirpath, limits_ts="2026-11-01T21:20:00.000Z", resets=1793774214, used=100.0, extra_old=True):
    d = Path(dirpath) / "2026" / "11" / "01"
    d.mkdir(parents=True)
    lines = []
    if extra_old:
        lines.append(json.dumps({"timestamp": "2026-11-01T10:00:00.000Z", "payload": {"rate_limits": {
            "primary": {"used_percent": 1.0, "window_minutes": 10080, "resets_at": resets}}}}))
    lines.append(json.dumps({"timestamp": "2026-11-01T21:19:00.000Z", "payload": {"type": "message", "text": "SECRET CONTENT"}}))
    lines.append(json.dumps({"timestamp": limits_ts, "payload": {"rate_limits": {
        "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": resets},
        "secondary": None, "credits": {"has_credits": True, "unlimited": False, "balance": "36779.135"}}}}))
    (d / "rollout-a.jsonl").write_text("\n".join(lines) + "\n")


def cost_body(*cents, has_more=False, next_page=None):
    """A cost_report page: one daily bucket per entry; each ``amount`` is a decimal string in USD cents."""
    data = [{"starting_at": "2026-11-0%dT00:00:00Z" % (i + 1), "ending_at": "2026-11-0%dT00:00:00Z" % (i + 2),
             "results": [{"currency": "USD", "amount": "%s" % c, "workspace_id": None, "model": None}] if c is not None else []}
            for i, c in enumerate(cents)]
    return json.dumps({"data": data, "has_more": has_more, "next_page": next_page})


def openai_body(*dollars, has_more=False, next_page=None):
    return json.dumps({"data": [{"results": [{"amount": {"value": d, "currency": "usd"}}]} for d in dollars],
                       "has_more": has_more, "next_page": next_page})


class FakeHttp:
    """Records requests; answers by URL from a queue (or a fixed reply). Never touches the network."""

    def __init__(self, usage=((200, OAUTH_USAGE),), probe=((200, "{}"),), costs=((200, None),), openai=((200, None),)):
        self.replies = {"usage": list(usage), "probe": list(probe), "openai": [(c, b if b is not None else openai_body(0.0)) for c, b in openai],
                        "costs": [(c, b if b is not None else cost_body(1200.0)) for c, b in costs]}
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers)))
        kind = ("usage" if "oauth/usage" in url else "costs" if "cost_report" in url
                else "openai" if "api.openai.com" in url else "probe")
        queue_ = self.replies[kind]
        return queue_.pop(0) if len(queue_) > 1 else queue_[0]

    def count(self, needle):
        return sum(needle in c[1] for c in self.calls)


def fake_grok(argv, timeout):
    return GROK


def no_rpc(argv, timeout):
    """The Codex app-server is unreachable: the source then exercises the session-log fallback."""
    raise fc.CmdError("unavailable")


class FakeRunner:
    """Maps argv to canned stdout, or raises a CmdError code. Records every call."""

    def __init__(self, fail=None, overrides=None):
        self.fail = fail or {}
        self.out = {"muse": json.dumps(MUSE), "billing": json.dumps(BILLING), "billing_daily": json.dumps({"usageItems": []}),
                    "agy": AGY, "claude": CLAUDE, "refresh": "", "colab_usage": COLAB_USAGE,
                    "rate_limit": json.dumps({"resources": {"core": {"limit": 5000, "remaining": 4000, "reset": 1793580000}}})}
        self.out.update(overrides or {})
        self.calls = []

    def __call__(self, argv, timeout, cwd=None):
        self.calls.append(list(argv))
        if argv[0] == "muse":
            key = "muse"
        elif "agy" in argv:
            key = "agy"
        elif argv[0] == "env" and "claude" in argv:
            key = "refresh" if "--output-format" not in argv else "claude"
        elif argv[0] in ("colab", "colab-b"):
            key = "colab_" + argv[-1]
            if key == "colab_whoami":
                return "Auth provider: oauth2\nEmail:         a@example.test\n"
        elif argv[0] == "gh" and argv[1] == "api" and argv[2] == "rate_limit":
            key = "rate_limit"
        elif argv[0] == "gh" and argv[1] == "api":
            key = "billing_daily" if "/settings/billing/usage?" in argv[2] else "billing"
        else:
            raise fc.CmdError("unavailable")
        if key in self.fail:
            raise fc.CmdError(self.fail[key])
        return self.out[key]


def build(tmp, runner=None, only=None, config=None, rules=None, now=None, **kw):
    """Build a snapshot from CONFIG (or ``config``) with every CLI and HTTP call faked."""
    sessions = Path(tmp) / "sessions"
    if not sessions.exists():
        codex_log(sessions)
    config = config if config is not None else make_config(only)
    if "codex" in config["sources"]:
        config["sources"]["codex"].setdefault("sessions_dir", str(sessions))
    kw.setdefault("codex_rpc", no_rpc)
    kw.setdefault("grok_rpc", fake_grok)
    kw.setdefault("http", FakeHttp())
    kw.setdefault("env", dict(ENV))
    return fc.build_snapshot(now or NOW, rules or RULES, config, runner or FakeRunner(), **kw)


def pool(snap, pid):
    return next((p for p in snap["pools"] if p["id"] == pid), None)


def src(snap, sid):
    return next(s for s in snap["sources"] if s["id"] == sid)
