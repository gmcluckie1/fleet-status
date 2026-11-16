#!/usr/bin/env python3
# Auth environment list adapted from clalt, Copyright (c) 2026 Kamal Dhital, MIT; see NOTICE.
"""fleet-status: one JSON feed of usage across AI subscriptions and API accounts.

Reads the sources you enable in a config file (Claude, Fable, Codex, agy, Muse, Grok, Colab, GitHub Actions
minutes, Anthropic and OpenAI API spend) and builds one ``fleet-status/v1`` snapshot that validates against
``fleet-status-v1.schema.json``. Every source is optional and isolated: it runs in its own thread under its
own wall-clock limit (one retry on timeout or unavailable), and a source that fails, times out or is not
configured degrades to a pool in state ``unknown`` instead of failing the run. Thresholds, pool labels and
advice text live in ``rules.json``.

Portions of this file (``pace_fill``, ``merge_window``, ``forecast_pool_pace``) are ported from
https://github.com/dhitalkamal/clalt (``internal/dashboard/pacemath.go``, ``internal/feed/limits.go``,
``internal/office/forecast.go``):
  MIT License - Copyright (c) 2026 Kamal Dhital. See NOTICE in the repository root.

Deterministic sources only: no LLM turn, no browser, no scraping. Credentials come only from documented
environment variables or a user-supplied command and are used in memory, never printed, logged or stored.

Python 3.9+, standard library only. Run isolated: ``python3 -I collect.py --config fleet-status.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_TIMEOUT = 10.0
# Per-source wall-clock limits (seconds); ``timeout_seconds`` in a source's config overrides one. agy and Colab
# read each configured account in turn, so their limit is ACCOUNT_BASE plus ACCOUNT_SECONDS per account.
SOURCE_TIMEOUTS = {"muse": 10.0, "codex": 15.0, "claude_sub": 20.0, "grok": 43.0, "claude_cloud": 45.0,
                   "github_billing": 15.0, "anthropic_costs": 15.0, "openai_costs": 15.0, "anthropic_ledger": 5.0,
                   "api_probe": 15.0}
ACCOUNT_BASE, ACCOUNT_SECONDS = 5.0, {"agy": 15.0, "colab": 30.0}
RETRY_CODES = {"timeout", "unavailable"}
MAX_BODY_BYTES = 64 * 1024
GH_MIN_CORE = 25  # below this many REST calls left, GitHub-backed sources report rate_limited instead of burning calls
USAGE_STATE_FILE = "usage-state.json"
LEDGER_SUMMARY_FILE = "api-ledger.json"
DEFAULT_STATE_DIR = "~/.local/state/fleet-status"
SHARED_LOGIN_RUNS = 3  # consecutive identical-looking collections before the shared-login ask
CLAUDE_CLOUD_MIN_AGE = 15 * 60  # the OAuth usage endpoint is polled at most this often (cached in the state dir)
API_PROBE_MIN_AGE = 24 * 3600   # the free count_tokens credit probe runs once a day
MAX_POOL_AGE_SECONDS = 6 * 3600  # 21600 s (6 h) eligibility limit across all usage pools
FRESHNESS_EXCEPTIONS: tuple = ()  # pool or source ids exempt from the freshness limit (rules.json can extend)
STALE_ADVICE_FALLBACK = "Stale reading (>6h old): treat pool state and figures as unknown until refreshed"
MISSING_TS_ADVICE_FALLBACK = "Missing producer timestamp: treat pool state and figures as unknown"
USER_AGENT = "fleet-status-collector/1"
CAPS = {"asks": 20, "pools": 20, "resets": 20}
PACE_MIN_ELAPSED = 0.05
PACE_MAX_READ_AGE = timedelta(minutes=30)
WINDOW_RESET_TOLERANCE = timedelta(seconds=60)
WINDOW_LENGTHS = {"5h": timedelta(hours=5), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}
UTC = timezone.utc
# Internal source error codes -> the schema enum; anything unlisted is reported as "unavailable".
ERROR_CODES = {"timeout": "timeout", "unavailable": "unavailable", "auth": "unauthorized", "unauthorized": "unauthorized",
               "rate_limited": "rate_limited", "parse": "parse_error", "parse_error": "parse_error",
               "not_configured": "not_configured"}
# One short phrase per schema error code, used as the advice of the unknown pool a failed source leaves behind.
FAILURE_REASONS = {"timeout": "the source timed out", "unavailable": "the source is unavailable",
                   "unauthorized": "the credential was rejected", "rate_limited": "the source is rate limited",
                   "parse_error": "the source output was not understood",
                   "not_configured": "the source is not configured or has no credential"}

# Sources, in report order. Each is enabled by listing it under ``sources`` in the config file.
SOURCE_IDS = ("muse", "codex", "agy", "colab", "claude_sub", "grok", "claude_cloud", "github_billing",
              "anthropic_costs", "openai_costs", "anthropic_ledger", "api_probe")
# Default environment variable holding each credentialed source's secret (``credential.env`` renames it).
CREDENTIAL_ENV = {"claude_cloud": "FLEET_STATUS_CLAUDE_OAUTH_TOKEN", "anthropic_costs": "FLEET_STATUS_ANTHROPIC_ADMIN_KEY",
                  "openai_costs": "FLEET_STATUS_OPENAI_ADMIN_KEY", "api_probe": "FLEET_STATUS_ANTHROPIC_API_KEY"}

# A superset of what a consumer is likely to reject: a record must never leave the machine with text that looks
# like a credential, a home path or an email address, so the whole snapshot is refused on any hit.
_KEY = r"(?:token|secret|password|passwd|api[_-]?key|client[_-]?secret|access[_-]?key|private[_-]?key)"
_Q = "[\\\\\"']*"  # optional quote or escaped-quote characters around a JSON/YAML key
SECRET_PATTERNS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9_-]{16,}",
    r"AKIA[0-9A-Z]{16}", r"xox[abprs]-[A-Za-z0-9-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}",
    r"ya29\.[A-Za-z0-9_-]{10,}", r"AIza[0-9A-Za-z_-]{30,}", r"GOCSPX-[A-Za-z0-9_-]{10,}",
    r"(?<![A-Za-z0-9])1//[A-Za-z0-9_-]{20,}",
    r"cf(?:ast|ut|at|k)_[A-Za-z0-9_-]{10,}", r"(?i)cf-access-client-(?:id|secret)\s*[:=]\s*\S{8,}",
    r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}",
    # Key-based, never value-based: a credential-named key followed by : or = drops the record whatever the
    # value looks like (spaces, short words, either quote kind), because the value cannot be reliably delimited.
    r"(?i)" + _KEY + _Q + r"\s*[:=]",
    r"[0-9a-fA-F]{40,}",
    # Home and system paths, in every spelling: absolute, ~, Windows drive.
    r"/Users/", r"/home/[A-Za-z]", r"~/", r"(?<![A-Za-z])[A-Za-z]:[\\/]",
    r"(?<![A-Za-z0-9])/(?:private|Volumes|tmp|var|etc)/\S+",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"://[^/\s:@]+:[^/\s@]+@",
)]




class CmdError(Exception):
    """A source or transport failure carrying only a fixed error code (never output or stderr)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OutputRefused(Exception):
    """The snapshot must not be emitted (secret pattern or size)."""


class ConfigError(Exception):
    """The config file cannot be used. The message never carries a config value."""


# ---------------------------------------------------------------- time helpers
def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def fmt_utc(value) -> str:
    if isinstance(value, datetime):
        value = value.timestamp()
    return datetime.fromtimestamp(int(value), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d(?::?\d\d)?)?$")


def norm_time(text) -> "str | None":
    """Normalise an ISO-ish timestamp (Z, +00, +00:00, or naive=UTC) to ``YYYY-MM-DDTHH:MM:SSZ``."""
    m = _ISO.match(str(text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=UTC)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    return fmt_utc(dt)


def next_month_start(now: datetime) -> str:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return fmt_utc(datetime(year, month, 1, tzinfo=UTC))


# ---------------------------------------------------------------- process + timeout plumbing
CLAUDE_AUTH_ENV = (
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
)
_AUTH_HINT = re.compile(r"scope|401|403|auth|login|credential|token", re.I)
# GitHub reports an exhausted quota as 403 (primary) or 403/429 (secondary); it is not an auth failure.
_RATE_HINT = re.compile(r"rate limit|rate-limit|too many requests|\b429\b", re.I)


def child_env(drop: tuple = ()) -> dict:
    """The environment for a command the collector starts: ours, minus ``FLEET_STATUS_*`` (the default credential
    variables and the config path) and any variable named in ``drop``."""
    return {k: v for k, v in os.environ.items() if not k.startswith("FLEET_STATUS_") and k not in drop}


def run_cmd(argv, timeout: float, cwd: "str | None" = None) -> str:
    """Run argv with no shell and no stdin; return stdout. Raises CmdError(code) and drops stderr."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                              stdin=subprocess.DEVNULL, check=False, env=child_env())
    except subprocess.TimeoutExpired:
        raise CmdError("timeout")
    except FileNotFoundError:  # the command is not installed: a source with nothing to read, not a failure to retry
        raise CmdError("not_configured")
    except (OSError, ValueError):
        raise CmdError("unavailable")
    if proc.returncode == 127:  # `env` or a shell wrapper could not find the command to run
        raise CmdError("not_configured")
    if proc.returncode != 0:
        err = proc.stderr or ""
        if _RATE_HINT.search(err):
            raise CmdError("rate_limited")
        raise CmdError("auth" if _AUTH_HINT.search(err) else "unavailable")
    return proc.stdout


def with_timeout(fn, seconds: float):
    """Run fn() in a daemon thread; CmdError('timeout') if it does not finish in time."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised in the caller below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise CmdError("timeout")
    if "error" in box:
        raise box["error"]
    return box["value"]


def parallel(fns, seconds: float):
    """Run callables concurrently under one wall-clock limit; returns results in order."""
    boxes = [{} for _ in fns]

    def make(fn, box):
        def target():
            try:
                box["value"] = fn()
            except BaseException as exc:
                box["error"] = exc
        return target

    threads = [threading.Thread(target=make(f, b), daemon=True) for f, b in zip(fns, boxes)]
    for t in threads:
        t.start()
    deadline = datetime.now(UTC).timestamp() + seconds
    for t in threads:
        t.join(max(0.0, deadline - datetime.now(UTC).timestamp()))
    results = []
    for t, box in zip(threads, boxes):
        if t.is_alive():
            raise CmdError("timeout")
        if "error" in box:
            raise box["error"]
        results.append(box["value"])
    return results


def load_json_line(text: str):
    """Parse stdout that is JSON, or has noise lines before a final JSON line."""
    try:
        return json.loads(text)
    except ValueError:
        for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
            if line.lstrip().startswith(("{", "[")):
                return json.loads(line)
        raise


# ---------------------------------------------------------------- secret scan, schema validation, rules
def has_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def _strings(node):
    """Every string value in a parsed JSON structure (what a consumer would scan), not its serialised text."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _strings(value)


def structure_has_secret(node) -> bool:
    return any(has_secret(text) for text in _strings(node))


def record_has_secret(record: dict) -> bool:
    return structure_has_secret(record)


class SchemaError(Exception):
    """The schema uses something this validator does not implement: refuse rather than validate loosely."""


# The JSON-Schema keywords this validator implements; anything else fails closed.
SCHEMA_KEYWORDS = {"$schema", "$id", "$ref", "$defs", "title", "description", "type", "const", "enum", "pattern",
                   "format", "minLength", "maxLength", "minimum", "maximum", "maxItems", "items", "properties",
                   "required", "additionalProperties", "oneOf"}
_SCHEMA_TYPES = {"object", "array", "string", "integer", "boolean", "null"}
_regex_cache: dict = {}


def valid_timestamp(text: str) -> bool:
    """The ``fleet-timestamp`` format: a real calendar instant written ``YYYY-MM-DDTHH:MM:SSZ``."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") == text
    except ValueError:
        return False


def valid_coordinator_timestamp(text: str) -> bool:
    """The ``coordinator-timestamp`` format: ``YYYY-MM-DDTHH:MM:SS(.mmm)?Z``."""
    return valid_timestamp(re.sub(r"\.\d{3}Z\Z", "Z", text))


_JS_SPACE = "\\t\\n\\x0b\\x0c\\r \\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"


def _js_pattern(pattern: str) -> str:
    """Translate a JS ``u``-mode pattern: ASCII ``\\d``, JS ``\\s`` (includes U+FEFF), end anchor without newline."""
    out, in_class, i = [], False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt == "d":
                out.append("0-9" if in_class else "[0-9]")
            elif nxt == "s":
                out.append(_JS_SPACE if in_class else "[" + _JS_SPACE + "]")
            else:
                out.append(ch + nxt)
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "$" and not in_class and i == len(pattern) - 1:
            out.append("\\Z")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _regex(pattern: str):
    if pattern not in _regex_cache:
        _regex_cache[pattern] = re.compile(_js_pattern(pattern))
    return _regex_cache[pattern]


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve(ref: str, root: dict) -> dict:
    prefix = "#/$defs/"
    target = root.get("$defs", {}).get(ref[len(prefix):]) if ref.startswith(prefix) else None
    if not isinstance(target, dict):
        raise SchemaError(f"unresolved $ref {ref}")
    return target


def validate(value, schema, path: str = "$", root: "dict | None" = None) -> list:
    """Validate against the JSON-Schema subset used by fleet-status-v1.schema.json.

    Implements ``$ref`` (to ``#/$defs``), ``oneOf`` (exactly one branch), ``const``, ``enum``, ``type``,
    ``minLength``/``maxLength``, ``pattern``, ``format: fleet-timestamp``, ``minimum``/``maximum``, ``maxItems``,
    ``items``, ``properties``, ``required`` and ``additionalProperties: false``. Any other keyword, type or
    format raises SchemaError (fail closed). Returns a list of error strings (no values: they may be sensitive).
    """
    root = schema if root is None else root
    for key in schema:
        if key not in SCHEMA_KEYWORDS:
            raise SchemaError(f"unsupported schema keyword {key}")
    if "$ref" in schema:
        return validate(value, _resolve(schema["$ref"], root), path, root)
    if "const" in schema and value != schema["const"]:
        return [f"{path}: expected constant"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: value not in enum"]
    if "oneOf" in schema:
        matches = sum(1 for branch in schema["oneOf"] if not validate(value, branch, path, root))
        if matches != 1:
            return [f"{path}: matches {matches} oneOf branches, expected exactly 1"]
    kind = schema.get("type")
    if kind is None:
        return []
    if kind not in _SCHEMA_TYPES:
        raise SchemaError(f"unsupported schema type {kind}")
    errors: list = []
    if kind == "null":
        return [] if value is None else [f"{path}: expected null"]
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{path}: expected boolean"]
    if kind == "integer":
        if not _is_int(value):
            return [f"{path}: expected integer"]
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
        return errors
    if kind == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength")
        if "pattern" in schema and not _regex(schema["pattern"]).search(value):
            errors.append(f"{path}: pattern mismatch")
        fmt = schema.get("format")
        if fmt is not None:
            if fmt == "fleet-timestamp":
                if not valid_timestamp(value):
                    errors.append(f"{path}: not a calendar timestamp")
            elif fmt == "coordinator-timestamp":
                if not valid_coordinator_timestamp(value):
                    errors.append(f"{path}: not a calendar timestamp")
            else:
                raise SchemaError(f"unsupported format {fmt}")
        return errors
    if kind == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]", root)
        return errors
    if not isinstance(value, dict):  # object
        return [f"{path}: expected object"]
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in value:
            errors.append(f"{path}.{key}: missing")
    if schema.get("additionalProperties") is False:
        errors += [f"{path}.{key}: unknown key" for key in value if key not in props]
    for key, sub in props.items():
        if key in value:
            errors += validate(value[key], sub, f"{path}.{key}", root)
    return errors


def pool_state(used_pct, resets_at, now: datetime, rules: dict) -> str:
    """State from the binding window's used percent and reset time (rules.json thresholds)."""
    if used_pct is None:
        return "unknown"
    th = rules["thresholds"]
    remaining = 100 - used_pct
    if remaining <= th["low_remaining_pct"]:
        return "low"
    if resets_at and remaining >= th["spend_first_min_remaining_pct"]:
        hours = (parse_utc(resets_at) - now).total_seconds() / 3600
        if 0 < hours <= th["spend_first_within_hours"]:
            return "spend_first"
    if remaining <= th["watch_remaining_pct"]:
        return "watch"
    return "healthy"


def make_pool(rules, pid, kind, unit, state, source_id, observed_at, used=None, limit=None, remaining=None,
              windows=None, pace_pct_per_hr=None, runs_out_at=None) -> dict:
    return {"id": pid, "label": rules["pools"].get(pid, pid), "kind": kind, "unit": unit, "used": used, "limit": limit,
            "remaining": remaining, "windows": windows or [], "state": state,
            "advice": rules["advice"].get(state), "observed_at": observed_at, "source_id": source_id,
            "pace_pct_per_hr": pace_pct_per_hr, "runs_out_at": runs_out_at}


def max_pool_age_seconds(rules: dict) -> int:
    th = rules.get("thresholds", {})
    val = th.get("max_age_seconds")
    return int(val) if _is_int(val) and val > 0 else MAX_POOL_AGE_SECONDS


def stale_advice(rules: dict) -> str:
    return rules.get("advice", {}).get("stale") or STALE_ADVICE_FALLBACK


def missing_timestamp_advice(rules: dict) -> str:
    return rules.get("advice", {}).get("missing_timestamp") or MISSING_TS_ADVICE_FALLBACK


def observation_is_fresh(observed_at: "str | None", now: datetime, rules: dict) -> bool:
    norm = norm_time(observed_at) if isinstance(observed_at, str) else None
    if not norm or not valid_timestamp(norm):
        return False
    age_s = (now - parse_utc(norm)).total_seconds()
    return 0 <= age_s <= max_pool_age_seconds(rules)


def suppress_pool_measurements(pool: dict, rules: dict, reason: str) -> dict:
    """Suppress measurements on a stale or undated pool while keeping identity, observed_at, and reset metadata."""
    pool["state"] = "unknown"
    pool["advice"] = stale_advice(rules) if reason == "stale" else missing_timestamp_advice(rules)
    pool["used"] = None
    pool["limit"] = None
    pool["remaining"] = None
    pool["pace_pct_per_hr"] = None
    pool["runs_out_at"] = None
    for w in pool.get("windows", []):
        w["used_pct"] = None
        if "pace_pct_per_hr" in w:
            w["pace_pct_per_hr"] = None
        if "runs_out_at" in w:
            w["runs_out_at"] = None
    return pool


def enforce_pool_freshness(pools: list, now: datetime, rules: dict) -> None:
    """Enforce the 6-hour (21600 s) freshness limit and producer-timestamp requirement across all usage pools."""
    exceptions = set(rules.get("thresholds", {}).get("freshness_exceptions") or FRESHNESS_EXCEPTIONS)
    for pool in pools:
        if pool.get("id") in exceptions or pool.get("source_id") in exceptions:
            continue
        obs = pool.get("observed_at")
        if obs is None:
            suppress_pool_measurements(pool, rules, "missing_timestamp")
        elif not observation_is_fresh(obs, now, rules) or pool.get("advice") == stale_advice(rules):
            suppress_pool_measurements(pool, rules, "stale")


def pace_fill(percent: float, resets_at: "datetime | None", observed: "datetime | None",
              length: "timedelta | float") -> "tuple[float, datetime | None, bool]":
    """Port of PaceFill from github.com/dhitalkamal/clalt internal/dashboard/pacemath.go (MIT).

    Projects when a usage window hits 100% assuming linear burn from the window start
    (``resets_at - length``) through ``observed``. Returns ``(per_hour, full_at, ok)``.
    """
    win_len = length if isinstance(length, timedelta) else timedelta(seconds=float(length))
    total_s = win_len.total_seconds()
    if resets_at is None or observed is None or percent <= 0 or total_s <= 0:
        return 0.0, None, False
    start = resets_at - win_len
    elapsed_s = (observed - start).total_seconds()
    if elapsed_s < PACE_MIN_ELAPSED * total_s or elapsed_s > total_s:
        return 0.0, None, False
    per_sec = float(percent) / elapsed_s
    per_hour = per_sec * 3600.0
    if percent >= 100:
        return per_hour, observed, True
    full_at = observed + timedelta(seconds=(100.0 - float(percent)) / per_sec)
    return per_hour, full_at, True


def _win_reset_dt(win: "dict | None") -> "datetime | None":
    if not isinstance(win, dict):
        return None
    raw = win.get("resets_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and valid_timestamp(raw):
        return parse_utc(raw)
    return None


def merge_window(old: "dict | None", obs: "dict | None", now: datetime) -> "dict | None":
    """Port of mergeWindow from github.com/dhitalkamal/clalt internal/feed/limits.go (MIT).

    Keeps rate-limit percentage monotonic within the same reset window (±60 s reset-timestamp
    jitter tolerance) and drops expired windows (``resets_at <= now``).
    """
    old_reset = _win_reset_dt(old)
    if old is not None and (old_reset is None or old_reset <= now or old.get("used_pct") is None):
        old, old_reset = None, None
    obs_reset = _win_reset_dt(obs)
    if obs is not None and (obs_reset is None or obs_reset <= now or obs.get("used_pct") is None):
        obs, obs_reset = None, None
    if obs is None:
        return dict(old) if old is not None else None
    if old is None or (obs_reset - old_reset) > WINDOW_RESET_TOLERANCE:
        return dict(obs)
    if (obs_reset - old_reset) >= -WINDOW_RESET_TOLERANCE and obs["used_pct"] > old["used_pct"]:
        return dict(obs)
    return dict(old)


def forecast_pool_pace(pool: dict, now: datetime) -> "tuple[int | None, str | None]":
    """Port of forecastOf from github.com/dhitalkamal/clalt internal/office/forecast.go (MIT).

    Populates ``pool['pace_pct_per_hr']`` and ``pool['runs_out_at']`` from active pool windows.
    """
    obs_str = pool.get("observed_at")
    observed = parse_utc(obs_str) if isinstance(obs_str, str) and valid_timestamp(obs_str) else None
    if observed is None or (now - observed) > PACE_MAX_READ_AGE or observed > now + timedelta(minutes=5):
        pool["pace_pct_per_hr"] = None
        pool["runs_out_at"] = None
        return None, None
    best_exhaust: "tuple[datetime, int] | None" = None
    best_rate: "tuple[float, int] | None" = None
    for w in pool.get("windows", []):
        used = w.get("used_pct")
        resets_str = w.get("resets_at")
        win_len = WINDOW_LENGTHS.get(w.get("label", ""))
        if used is None or not isinstance(resets_str, str) or not valid_timestamp(resets_str) or win_len is None:
            continue
        resets_at = parse_utc(resets_str)
        if resets_at <= now:
            continue
        per_hour, full_at, ok = pace_fill(float(used), resets_at, observed, win_len)
        if not ok or full_at is None:
            continue
        rate_int = max(0, min(10000, int(round(per_hour))))
        win_s = win_len.total_seconds()
        if best_rate is None or win_s < best_rate[0]:
            best_rate = (win_s, rate_int)
        if full_at < resets_at:
            clamped = now if full_at < now else full_at
            if best_exhaust is None or clamped < best_exhaust[0]:
                best_exhaust = (clamped, rate_int)
    if best_exhaust is not None:
        pool["pace_pct_per_hr"] = best_exhaust[1]
        pool["runs_out_at"] = fmt_utc(best_exhaust[0])
    elif best_rate is not None:
        pool["pace_pct_per_hr"] = best_rate[1]
        pool["runs_out_at"] = None
    else:
        pool["pace_pct_per_hr"] = None
        pool["runs_out_at"] = None
    return pool["pace_pct_per_hr"], pool["runs_out_at"]


def merge_pools_and_pace(pools: list, now: datetime, rules: dict, state: "UsageState") -> None:
    """Monotonically merge pool windows via ``merge_window`` and compute per-pool pace forecast."""
    prev_all = state.get("pool_windows")
    prev_map = prev_all if isinstance(prev_all, dict) else {}
    next_map: dict = {}
    for pool in pools:
        pid = pool["id"]
        pool_prev = prev_map.get(pid) if not pid.startswith("agy-") and isinstance(prev_map.get(pid), dict) else {}
        kept_pool: dict = {}
        for label, old_w in pool_prev.items():
            still_valid = merge_window(old_w, None, now)
            if still_valid is not None:
                kept_pool[label] = still_valid
        ineligible = (
            pool.get("observed_at") is None
            or not observation_is_fresh(pool.get("observed_at"), now, rules)
            or pool.get("advice") == stale_advice(rules)
        )
        if ineligible:
            if kept_pool:
                next_map[pid] = kept_pool
            suppress_pool_measurements(pool, rules, "missing_timestamp" if pool.get("observed_at") is None else "stale")
            continue
        changed = False
        for w in pool.get("windows", []):
            label = w.get("label")
            if not label or w.get("used_pct") is None or not w.get("resets_at"):
                continue
            merged = merge_window(kept_pool.get(label), w, now)
            if merged is not None:
                new_pct = int_pct(merged["used_pct"])
                new_reset = fmt_utc(merged["resets_at"]) if isinstance(merged["resets_at"], datetime) else merged["resets_at"]
                if new_pct != w["used_pct"]:
                    changed = True
                w["used_pct"] = new_pct
                w["resets_at"] = new_reset
                kept_pool[label] = {"label": label, "used_pct": new_pct, "resets_at": new_reset}
        if changed and pool.get("unit") == "percent" and pool.get("limit") == 100:
            known = [w for w in pool["windows"] if w.get("used_pct") is not None]
            if known:
                binding = max(known, key=lambda w: w["used_pct"])
                if pool.get("used") is not None and binding["used_pct"] > pool["used"]:
                    pool["used"] = binding["used_pct"]
                    pool["remaining"] = 100 - binding["used_pct"]
                pool["state"] = pool_state(binding["used_pct"], binding["resets_at"], now, rules)
                pool["advice"] = rules["advice"].get(pool["state"])
        if kept_pool:
            next_map[pid] = kept_pool
        if pid != "github-actions":
            forecast_pool_pace(pool, now)
    if next_map or prev_all is not None:
        state.put("pool_windows", next_map)


def pool_from_windows(rules, now, pid, kind, unit, source_id, observed_at, windows, **amounts) -> dict:
    """Pool whose state comes from the window with the highest used percent."""
    known = [w for w in windows if w["used_pct"] is not None]
    binding = max(known, key=lambda w: w["used_pct"]) if known else None
    state = pool_state(binding["used_pct"], binding["resets_at"], now, rules) if binding else "unknown"
    return make_pool(rules, pid, kind, unit, state, source_id, observed_at, windows=windows, **amounts)


def clean(text, limit: int = 200) -> str:
    """One printable line, at most ``limit`` characters, with no leading or trailing whitespace."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    return re.sub(r"[\s\ufeff]+", " ", text).strip(" \ufeff")[:limit].strip(" \ufeff")


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:limit]


def int_pct(value) -> int:
    return max(0, min(100, int(round(float(value)))))


# ---------------------------------------------------------------- config
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")  # an account label: it ends up in pool ids and labels
_COLLECTOR_ID = re.compile(r"^[a-z0-9-]{1,40}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")


def load_config(path) -> dict:
    """Read and check a config file; raises ConfigError (message free of config values).

    Structural mistakes fail closed (an unknown key or source name is almost always a typo that would otherwise
    silently disable a source). Keys starting with ``_`` are comments. Problems inside one source's settings are
    not checked here: that source reports ``not_configured`` and the others keep running.
    """
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except OSError:
        raise ConfigError("cannot read the config file") from None
    except ValueError:
        raise ConfigError("the config file is not valid JSON") from None
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    for key in raw:
        if not key.startswith("_") and key not in ("collector_id", "state_dir", "pool_notes", "sources"):
            raise ConfigError("unknown config key " + clean(key, 40))
    collector_id = raw.get("collector_id", "fleet-status")
    if not (isinstance(collector_id, str) and _COLLECTOR_ID.match(collector_id)):
        raise ConfigError("collector_id must be 1-40 characters of a-z, 0-9 and -")
    state_dir = raw.get("state_dir")
    if state_dir is not None and not (isinstance(state_dir, str) and state_dir):
        raise ConfigError("state_dir must be a non-empty string")
    notes = raw.get("pool_notes", {})
    if not (isinstance(notes, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in notes.items())):
        raise ConfigError("pool_notes must map pool ids to text")
    sources = raw.get("sources")
    if not isinstance(sources, dict):
        raise ConfigError("sources must be an object")
    checked = {}
    for sid, cfg in sources.items():
        if sid.startswith("_"):
            continue
        if sid not in SOURCE_IDS:
            raise ConfigError("unknown source " + clean(sid, 40))
        if not isinstance(cfg, dict):
            raise ConfigError(f"source {sid} must be an object")
        if not isinstance(cfg.get("enabled", True), bool):
            raise ConfigError(f"source {sid}: enabled must be true or false")
        checked[sid] = cfg
    return {"collector_id": collector_id, "state_dir": state_dir, "pool_notes": notes, "sources": checked}


def enabled_sources(config: dict) -> list:
    """(id, settings) for each enabled source, in report order."""
    sources = config.get("sources", {})
    return [(sid, sources[sid]) for sid in SOURCE_IDS if sid in sources and sources[sid].get("enabled", True)]


def command_argv(cfg: dict, default: list) -> list:
    """The argv to run: ``command`` from the source's settings, else ``default``. Never a shell string."""
    value = cfg.get("command", default)
    if not (isinstance(value, list) and value and all(isinstance(v, str) and v for v in value)):
        raise CmdError("not_configured")
    return [os.path.expanduser(value[0]), *value[1:]]


def read_credential(ctx, sid: str, cfg: dict) -> str:
    """A source's secret, in memory only. ``credential.command`` (a user-supplied argv whose stdout is the secret)
    wins; otherwise the environment variable ``credential.env`` (default ``CREDENTIAL_ENV[sid]``). A missing or
    empty value, or a failing command, is ``not_configured``. The secret is never placed in an argv or a log."""
    spec = cfg.get("credential", {})
    if not isinstance(spec, dict):
        raise CmdError("not_configured")
    if "command" in spec:
        try:
            raw = ctx.runner(command_argv(spec, []), SOURCE_TIMEOUT)
        except CmdError as exc:
            raise CmdError("timeout" if exc.code == "timeout" else "not_configured") from None
    else:
        name = spec.get("env", CREDENTIAL_ENV[sid])
        if not (isinstance(name, str) and _ENV_NAME.match(name)):
            raise CmdError("not_configured")
        raw = ctx.env.get(name) or ""
    value = raw.strip()
    if not value:
        raise CmdError("not_configured")
    return value


def account_list(cfg: dict) -> list:
    """The ``accounts`` of an agy or Colab source: a list of objects with a unique ``label``. Absent means one
    account labelled ``default`` that uses the current login."""
    raw = cfg.get("accounts", [{"label": "default"}])
    if not (isinstance(raw, list) and raw):
        raise CmdError("not_configured")
    seen = set()
    for entry in raw:
        label = entry.get("label") if isinstance(entry, dict) else None
        if not (isinstance(label, str) and _PROFILE_ID.match(label)) or label in seen:
            raise CmdError("not_configured")
        seen.add(label)
    return raw


def source_timeouts(config: dict) -> dict:
    """Wall-clock limit per source id: ``timeout_seconds`` (1-300) from its settings, else the built-in default."""
    out = {}
    for sid in SOURCE_IDS:
        cfg = config.get("sources", {}).get(sid) or {}
        override = cfg.get("timeout_seconds")
        if isinstance(override, (int, float)) and not isinstance(override, bool) and 1 <= override <= 300:
            out[sid] = float(override)
        elif sid in ACCOUNT_SECONDS:
            accounts = cfg.get("accounts")
            count = len(accounts) if isinstance(accounts, list) and accounts else 1
            out[sid] = ACCOUNT_BASE + ACCOUNT_SECONDS[sid] * count
        else:
            out[sid] = SOURCE_TIMEOUTS[sid]
    return out


# ---------------------------------------------------------------- sources
# Each source is ``src_<id>(ctx, cfg)``: ``cfg`` is its settings from the config file. It returns
# {"observed_at": str, "pools": [...], "asks": [...]}; a failure raises CmdError(code), which becomes an error code
# on the source and an unknown pool (see ``failure_pools``).
def _muse_held_pool(ctx, data) -> "dict | None":
    """``server_sent_no_usage`` says nothing about current use: Muse has sent it while every model call failed with
    429 "Subscription quota exhausted". So no 0% is invented for any window. A last-known weekly reading
    that has not reset yet is kept (dated when it was seen); otherwise the pool is ``unknown``.
    """
    if "server_sent_no_usage" not in f"{data.get('reason')} {data.get('hint')}":
        return None
    last = ctx.state.get("muse_weekly")
    seen_at = norm_time(last.get("seen_at")) if isinstance(last, dict) else None
    resets_at = norm_time(last.get("resets_at")) if isinstance(last, dict) else None
    if not isinstance(last, dict) or not resets_at or not seen_at or last.get("used_pct") is None \
            or parse_utc(resets_at) <= ctx.now:
        observed = fmt_utc(ctx.now)
        return {"observed_at": observed, "pools": [make_pool(
            ctx.rules, "muse", "subscription", "percent", "unknown", "muse", observed)]}
    windows = [{"label": "weekly", "used_pct": int_pct(last["used_pct"]), "resets_at": resets_at}]
    pool = pool_from_windows(ctx.rules, ctx.now, "muse", "subscription", "percent", "muse", seen_at, windows,
                             used=windows[0]["used_pct"], limit=100, remaining=100 - windows[0]["used_pct"])
    enforce_pool_freshness([pool], ctx.now, ctx.rules)
    return {"observed_at": seen_at, "pools": [pool]}


def _muse_observed(allow: dict) -> "str | None":
    raw = allow.get("asOfMs")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return fmt_utc(raw / 1000)
    if isinstance(raw, str):
        return norm_time(raw)
    return None


def src_muse(ctx, cfg) -> dict:
    data = load_json_line(ctx.runner(command_argv(cfg, ["muse", "usage", "--json"]), ctx.timeouts["muse"]))
    allow = data["allowance"]
    if not isinstance(allow, dict):  # the CLI answered but reports no allowance this window
        held = _muse_held_pool(ctx, data)
        if held:
            return held
        observed = fmt_utc(ctx.now)
        return {"observed_at": observed, "pools": [make_pool(
            ctx.rules, "muse", "subscription", "percent", "unknown", "muse", observed)]}
    windows = []
    for label, key in (("5h", "window"), ("weekly", "weekly")):
        w = allow.get(key)
        if isinstance(w, dict) and w.get("usedPercent") is not None:
            resets = w.get("resetsAtMs")
            windows.append({"label": label, "used_pct": int_pct(w["usedPercent"]),
                            "resets_at": fmt_utc(resets / 1000) if resets else None})
    if not windows:
        raise CmdError("unavailable")
    observed = _muse_observed(allow)
    weekly = next((w for w in windows if w["label"] == "weekly"), windows[0])
    if observed and weekly["label"] == "weekly" and weekly["resets_at"]:
        ctx.state.put("muse_weekly", {"used_pct": weekly["used_pct"], "resets_at": weekly["resets_at"],
                                      "seen_at": observed})
    pool = pool_from_windows(ctx.rules, ctx.now, "muse", "subscription", "percent", "muse", observed, windows,
                             used=weekly["used_pct"], limit=100, remaining=100 - weekly["used_pct"])
    enforce_pool_freshness([pool], ctx.now, ctx.rules)
    return {"observed_at": observed, "pools": [pool]}


def gh_rate_guard(ctx, gh) -> None:
    """Raise ``rate_limited`` when the GitHub REST core quota is nearly spent (``gh api rate_limit`` is free).

    One check per run (shared by the GitHub-backed sources). The reset time is kept in the state file for the log.
    An unreadable answer proceeds unguarded; the call-level rate-limit detection in ``run_cmd`` still applies.
    """
    with ctx.gh_lock:
        if ctx.gh_limit is None:
            try:
                core = load_json_line(ctx.runner([*gh, "api", "rate_limit"], 8.0))["resources"]["core"]
                ctx.gh_limit = {"remaining": int(core["remaining"]), "reset": int(core["reset"])}
            except CmdError as exc:
                if exc.code == "rate_limited":
                    ctx.gh_limit = {"remaining": 0, "reset": None}
                else:
                    ctx.gh_limit = {}
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                ctx.gh_limit = {}
        info = dict(ctx.gh_limit)
    if info and info["remaining"] < GH_MIN_CORE:
        ctx.state.put("gh_rate_limit", {"remaining": info["remaining"], "seen_at": fmt_utc(ctx.now),
                                        "reset_at": fmt_utc(info["reset"]) if info["reset"] else None})
        raise CmdError("rate_limited")


def _find_key(node, key, depth=0):
    if depth > 6:
        return None
    if isinstance(node, dict):
        if isinstance(node.get(key), dict):
            return node[key]
        for child in node.values():
            found = _find_key(child, key, depth + 1)
            if found is not None:
                return found
    return None


def _newest_session_files(sessions: Path, want: int = 5, max_days: int = 4) -> list:
    """Newest rollout files, found by walking only the newest YYYY/MM/DD dirs (never a full-tree rglob)."""
    def subdirs(parent: Path, width: int) -> list:
        try:
            names = [d.name for d in parent.iterdir() if d.is_dir() and d.name.isdigit() and len(d.name) == width]
        except OSError:
            return []
        return sorted(names, reverse=True)

    found, days = [], 0
    for year in subdirs(sessions, 4):
        for month in subdirs(sessions / year, 2):
            for day in subdirs(sessions / year / month, 2):
                days += 1
                found += list((sessions / year / month / day).glob("*.jsonl"))
                if len(found) >= want or days >= max_days:
                    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[:want]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[:want]


def _latest_rate_limits(sessions: Path):
    if not sessions.is_dir():
        raise CmdError("not_configured")  # Codex has never written a session here: nothing to read
    for path in _newest_session_files(sessions):
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 2 * 1024 * 1024))
            tail = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            limits = _find_key(event, "rate_limits")
            if limits:
                return limits, norm_time(event.get("timestamp"))
    raise CmdError("unavailable")


def codex_rate_limits(argv: list, timeout: float) -> dict:
    """``account/rateLimits/read`` over ``codex app-server`` (stdio JSON-RPC); returns the response ``result``.

    OPENAI_API_KEY is removed from the child environment so the subscription login is used. The child is
    always killed. No response content is logged; any failure is a bare CmdError code.
    """
    env = child_env(("OPENAI_API_KEY",))
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, text=True)
    except (OSError, ValueError):
        raise CmdError("unavailable")
    lines: "queue.Queue" = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        for msg in ({"id": 1, "method": "initialize",
                     "params": {"clientInfo": {"name": "fleet-status", "title": None, "version": "1"}}},
                    {"method": "initialized"},
                    {"id": 2, "method": "account/rateLimits/read"}):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise CmdError("timeout")
            if line is None:
                raise CmdError("unavailable")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == 2:
                if not isinstance(reply.get("result"), dict):
                    raise CmdError("unavailable")
                return reply["result"]
    except (OSError, ValueError):
        raise CmdError("unavailable")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        reader.join(1.0)
        for pipe in (proc.stdin, proc.stdout):  # no file handle outlives the call
            try:
                pipe.close()
            except Exception:
                pass


def rpc_to_limits(result: dict) -> dict:
    """Normalise an ``account/rateLimits/read`` result to the session-log ``rate_limits`` shape."""
    rl = result["rateLimits"]
    limits = {}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict):
            limits[key] = {"used_percent": w.get("usedPercent"), "window_minutes": w.get("windowDurationMins"),
                           "resets_at": w.get("resetsAt")}
    cr = rl.get("credits")
    if isinstance(cr, dict):
        limits["credits"] = {"has_credits": cr.get("hasCredits"), "unlimited": cr.get("unlimited"),
                             "balance": cr.get("balance")}
    limits["reached"] = bool(rl.get("rateLimitReachedType"))
    reset = result.get("rateLimitResetCredits")
    count = reset.get("availableCount") if isinstance(reset, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        limits["reset_credits"] = count
    return limits


WINDOW_LABELS = {300: "5h", 10080: "weekly", 43200: "monthly"}


def codex_sessions_dir(cfg: dict, env) -> Path:
    """``sessions_dir`` from the settings, else ``$CODEX_HOME/sessions``, else ``~/.codex/sessions``."""
    custom = cfg.get("sessions_dir")
    if isinstance(custom, str) and custom:
        return Path(custom).expanduser()
    home = env.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "sessions"


def src_codex(ctx, cfg) -> dict:
    argv = command_argv(cfg, ["codex", "app-server"])
    try:
        limits, observed = rpc_to_limits(ctx.codex_rpc(argv, ctx.timeouts["codex"] - 3)), fmt_utc(ctx.now)
    except (CmdError, KeyError, TypeError, ValueError):  # app-server unusable: fall back to the newest session log
        limits, observed = _latest_rate_limits(codex_sessions_dir(cfg, ctx.env))
    windows = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        label = WINDOW_LABELS.get(w.get("window_minutes")) if isinstance(w, dict) else None
        if not label:
            continue
        resets = w.get("resets_at")
        passed = bool(resets) and resets <= ctx.now.timestamp()  # window rolled over: the reading is stale
        windows.append({"label": label, "used_pct": None if passed or w.get("used_percent") is None
                        else int_pct(w["used_percent"]), "resets_at": fmt_utc(resets) if resets else None})
    if not windows:
        raise CmdError("unavailable")
    weekly = next((w for w in windows if w["label"] == "weekly"), windows[0])
    pools = [pool_from_windows(
        ctx.rules, ctx.now, "codex", "subscription", "percent", "codex", observed, windows,
        used=weekly["used_pct"], limit=100, remaining=None if weekly["used_pct"] is None else 100 - weekly["used_pct"])]
    if limits.get("reached") and pools[0]["state"] not in ("low", "unknown"):  # the service says the limit is hit
        pools[0]["state"], pools[0]["advice"] = "low", ctx.rules["advice"]["low"]
    credits = limits.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits") and not credits.get("unlimited") \
            and credits.get("balance") is not None:
        balance = int(float(credits["balance"]))
        state = "low" if balance < ctx.rules["thresholds"]["codex_credits_low_below"] else "healthy"
        pools.append(make_pool(ctx.rules, "codex-credits", "credit", "credits", state, "codex", observed,
                               remaining=balance))
    if limits.get("reset_credits"):  # count of rate-limit reset credits
        pools.append(make_pool(ctx.rules, "codex-reset-credits", "credit", "credits", "healthy", "codex", observed,
                               remaining=limits["reset_credits"]))
    enforce_pool_freshness(pools, ctx.now, ctx.rules)
    return {"observed_at": observed, "pools": pools}


AGY_COMMAND = ["agy", "-p", "/usage", "--output-format", "json"]


def src_agy(ctx, cfg) -> dict:
    """Collect each configured account independently; never read its credential files.

    An account with a ``home`` runs as ``env HOME=<home> agy ...`` so several logins can live side by side.
    A failing account leaves an ``agy-<label>-unavailable`` pool in state ``unknown`` and the others continue.
    """
    observed = fmt_utc(ctx.now)
    pools = []
    for account in account_list(cfg):
        prefix = "agy-" + account["label"] + "-"
        try:
            argv = command_argv(account, AGY_COMMAND)
            home = account.get("home")
            if home is not None:
                if not (isinstance(home, str) and home):
                    raise CmdError("not_configured")
                argv = ["env", "HOME=" + os.path.expanduser(home), *argv]
            data = load_json_line(ctx.runner(argv, ACCOUNT_SECONDS["agy"]))
            groups = data["command"]["data"]["groups"]
            account_pools = []
            for group in groups:
                windows = []
                for bucket in group["buckets"]:
                    label = {"weekly": "weekly", "5h": "5h"}.get(bucket.get("window"))
                    if not label or bucket.get("disabled"):
                        continue
                    used = int_pct(100 - 100 * float(bucket["remaining_fraction"]))
                    windows.append({"label": label, "used_pct": used, "resets_at": norm_time(bucket.get("reset_time"))})
                if not windows:
                    continue
                gid = prefix + slug(group["name"], 30)
                weekly = next((w for w in windows if w["label"] == "weekly"), windows[0])
                item = pool_from_windows(ctx.rules, ctx.now, gid, "subscription", "percent", "agy", observed, windows,
                                         used=weekly["used_pct"], limit=100, remaining=100 - weekly["used_pct"])
                item["label"] = f"{gid} ({weekly['label']})"
                account_pools.append(item)
            if not account_pools:
                raise CmdError("unavailable")
            pools.extend(account_pools)
        except (CmdError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            item = make_pool(ctx.rules, prefix + "unavailable", "subscription", "percent", "unknown", "agy", observed)
            item["advice"] = "Unavailable: the account could not be read"
            pools.append(item)
    return {"observed_at": observed, "pools": pools}


def src_colab(ctx, cfg) -> dict:
    """One compute-unit pool per Colab account: ``colab-<label>-units``.

    Each account is read through its own ``command`` (default ``colab``), so separate logins can use separate
    wrappers. With ``expect_email`` set, ``whoami`` must report that address, otherwise the pool is unknown and
    no balance is trusted. The address is compared in memory and never published.
    """
    observed = fmt_utc(ctx.now)
    pools = []
    for account in account_list(cfg):
        label = account["label"]
        pid = "colab-" + label + "-units"
        try:
            argv = command_argv(account, ["colab"])
            expect = account.get("expect_email")
            item = None
            if expect is not None:
                if not (isinstance(expect, str) and expect.strip()):
                    raise CmdError("not_configured")
                who = re.search(r"^Email:\s+(\S+)", ctx.runner([*argv, "whoami"], 15.0), re.M)
                if not who or who.group(1).lower() != expect.strip().lower():
                    item = make_pool(ctx.rules, pid, "credit", "credits", "unknown", "colab", observed)
                    item["advice"] = "Colab account mismatch: the CLI is signed in to a different account than expected"
            if item is None:
                balance = re.search(r"^Current balance:\s+(\d+(?:\.\d+)?)", ctx.runner([*argv, "usage"], 15.0), re.M)
                if not balance:
                    raise CmdError("parse")
                units = int(float(balance.group(1)))
                item = make_pool(ctx.rules, pid, "credit", "credits", "healthy" if units > 0 else "low", "colab",
                                 observed, remaining=units)
        except CmdError:
            item = make_pool(ctx.rules, pid, "credit", "credits", "unknown", "colab", observed)
            item["advice"] = "Unavailable: the account could not be read"
        item["label"] = clean(f"Colab compute units ({label})")
        pools.append(item)
    return {"observed_at": observed, "pools": pools}


def shared_login_asks(state, pools, now, labels) -> list:
    """Warn when two agy accounts report identical reset times AND remaining values for 3 collections in a row.

    The counter lives in the usage state (derived values only: a hash of the readings, never a credential).
    """
    by_account = {}
    for p in pools:
        owner = max((lab for lab in labels if p["id"].startswith(f"agy-{lab}-") and p["id"] != f"agy-{lab}-unavailable"),
                    key=len, default=None)
        resets = tuple(w["resets_at"] for w in p["windows"])
        if owner and resets and all(resets) and p["remaining"] is not None:
            by_account.setdefault(owner, []).append((p["id"][len(f"agy-{owner}-"):], resets, p["remaining"]))
    key, asks = "shared_login_agy", []
    sigs = {name: hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest() for name, rows in by_account.items()}
    same = len(sigs) >= 2 and len(set(sigs.values())) == 1
    count = (((state.get(key) or {}).get("count") or 0) + 1) if same else 0
    state.put(key, {"count": count})
    if count >= SHARED_LOGIN_RUNS:
        asks.append({"id": "shared-login-agy", "title": "Possible shared agy login",
                     "detail": clean(f"agy accounts {', '.join(sorted(sigs))} report identical reset times and remaining "
                                     f"values for {count} collections in a row; check each account is signed in on its own", 280),
                     "kind": "confirm", "since": fmt_utc(now), "link_ref": None})
    return asks


def claude_argv(cfg: dict, *tail: str) -> list:
    """The Claude CLI with every auth environment variable removed, so the subscription login is used."""
    return ["env", *(arg for name in CLAUDE_AUTH_ENV for arg in ("-u", name)), *command_argv(cfg, ["claude"]), *tail]


_CLAUDE_LINE = re.compile(
    r"^Current (?P<scope>week|session)(?: \((?P<model>[^)]*)\))?: (?P<pct>\d+(?:\.\d+)?)% used"
    r"(?: \u00b7 resets (?P<when>.+?) \((?P<tz>[A-Za-z_]+/[A-Za-z_]+|UTC)\))?\s*$", re.M)
_CLAUDE_WHEN = re.compile(r"^(?:(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}) at )?(?P<h>\d{1,2})(?::(?P<m>\d\d))?(?P<ap>am|pm)$")
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def claude_reset_utc(when: str, tz: str, now: datetime) -> "str | None":
    """'Nov 8 at 3:59am' / '4am' in a named zone -> UTC. The year is inferred (rolls over across December);
    a bare time means its next occurrence. Returns None when the zone or text is not understood."""
    m = _CLAUDE_WHEN.match(when.strip())
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        return None
    hour = int(m["h"]) % 12 + (12 if m["ap"] == "pm" else 0)
    minute = int(m["m"] or 0)
    local_now = now.astimezone(zone)
    try:
        if m["mon"]:
            cand = datetime(local_now.year, _MONTHS[m["mon"]], int(m["day"]), hour, minute, tzinfo=zone)
            if cand < local_now - timedelta(days=1):  # a date already well past means next year (December -> January)
                cand = cand.replace(year=local_now.year + 1)
        else:
            cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand <= local_now:
                cand += timedelta(days=1)
    except (KeyError, ValueError):
        return None
    return fmt_utc(cand.astimezone(UTC))


def parse_claude_usage(text: str, now: datetime) -> list:
    """Windows for the all-models subscription limits from the ``/usage`` text (per-model lines are skipped)."""
    windows = []
    for m in _CLAUDE_LINE.finditer(text):
        if (m["model"] or "all models").lower() != "all models":
            continue
        resets = claude_reset_utc(m["when"], m["tz"], now) if m["when"] else None
        windows.append({"label": "weekly" if m["scope"] == "week" else "5h", "used_pct": int_pct(m["pct"]),
                        "resets_at": resets})
    return sorted(windows, key=lambda w: w["label"] != "weekly")


_FABLE_LINE = re.compile(r"^Current week \(Fable\): (?P<pct>\d+(?:\.\d+)?)% used"
                         r"(?: \u00b7 resets (?P<when>.+?) \((?P<tz>[A-Za-z_]+/[A-Za-z_]+|UTC)\))?\s*$", re.M)


def claude_fable_pool(ctx, text: str, observed: str) -> dict:
    """The separate Fable weekly limit from ``claude /usage``; 'unavailable' when the output has no Fable line."""
    m = _FABLE_LINE.search(text)
    if not m:
        pool = _unavailable_pool(ctx, "claude-fable", "subscription", "percent", "claude_sub",
                                 "claude /usage shows no Fable weekly limit")
    else:
        resets = claude_reset_utc(m["when"], m["tz"], ctx.now) if m["when"] else None
        pct = int_pct(m["pct"])
        pool = pool_from_windows(ctx.rules, ctx.now, "claude-fable", "subscription", "percent", "claude_sub", observed,
                                 [{"label": "weekly", "used_pct": pct, "resets_at": resets}],
                                 used=pct, limit=100, remaining=100 - pct)
    return pool


def src_claude_sub(ctx, cfg) -> dict:
    raw = ctx.runner(claude_argv(cfg, "-p", "/usage", "--output-format", "json"),
                     ctx.timeouts["claude_sub"], cwd=tempfile.gettempdir())
    data = load_json_line(raw)
    entries = data if isinstance(data, list) else [data]
    entry = next((e for e in reversed(entries)
                  if isinstance(e, dict) and e.get("type") == "result" and isinstance(e.get("result"), str)), None)
    if entry is None or entry.get("num_turns") != 0 or isinstance(entry.get("num_turns"), bool):
        raise CmdError("parse")
    text = entry["result"]
    windows = parse_claude_usage(text, ctx.now)
    if not any(w["label"] == "weekly" for w in windows):
        raise CmdError("parse")
    observed = fmt_utc(ctx.now)
    weekly = windows[0]
    pools = [pool_from_windows(
        ctx.rules, ctx.now, "claude-subscription", "subscription", "percent", "claude_sub", observed, windows,
        used=weekly["used_pct"], limit=100, remaining=100 - weekly["used_pct"])]
    if cfg.get("fable", True):  # the separate Fable weekly limit; "fable": false leaves it out
        pools.append(claude_fable_pool(ctx, text, observed))
    return {"observed_at": observed, "pools": pools}


# ---------------------------------------------------------------- state, http, stdio plumbing
class UsageState:
    """Small JSON file in the state dir holding derived readings only (never a token or key).

    With no ``path`` it is in-memory, so tests and ``state_dir=None`` runs never touch the disk. Writes are
    atomic, mode 0600 and serialised, because sources run in threads.
    """

    def __init__(self, path: "Path | None" = None):
        self.path, self.lock, self.data = path, threading.Lock(), {}
        if path is not None:
            try:
                loaded = json.loads(Path(path).read_text())
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.data = {}

    def get(self, key):
        with self.lock:
            value = self.data.get(key)
            return json.loads(json.dumps(value)) if value is not None else None

    def put(self, key, value) -> None:
        with self.lock:
            self.data[key] = value
            if self.path is None:
                return
            try:
                tmp = Path(str(self.path) + ".tmp")
                tmp.write_text(json.dumps(self.data, separators=(",", ":"), sort_keys=True))
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except OSError:
                pass  # state is an optimisation: a write failure must not fail the source


def http_request(method: str, url: str, headers: dict, body: "bytes | None", timeout: float) -> "tuple":
    """HTTPS request returning (status, text). Transport failures are a bare CmdError; no content is logged."""
    if not url.startswith("https://"):
        raise CmdError("unavailable")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https enforced above)
            return resp.status, resp.read(MAX_BODY_BYTES * 4).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except OSError:
            return exc.code, ""
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise CmdError("timeout" if "timed out" in str(exc) else "unavailable")


def stdio_rpc(argv, env: dict, messages: list, want_id: int, timeout: float) -> dict:
    """Send newline-delimited JSON-RPC ``messages`` to a child over stdio; return the ``result`` of ``want_id``.

    The child is always killed. No response content is logged; any failure is a bare CmdError code.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, text=True, cwd=os.path.expanduser("~"))
    except FileNotFoundError:
        raise CmdError("not_configured")
    except (OSError, ValueError):
        raise CmdError("unavailable")
    lines: "queue.Queue" = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        for msg in messages:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise CmdError("timeout")
            if line is None:
                raise CmdError("unavailable")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == want_id:
                if not isinstance(reply.get("result"), dict):
                    proc.stdin.close()
                    try:
                        proc.wait(timeout=min(5.0, max(0.0, deadline - time.monotonic())))
                    except Exception:
                        pass
                    raise CmdError("unavailable")
                return reply["result"]
    except (OSError, ValueError):
        raise CmdError("unavailable")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        reader.join(1.0)
        for pipe in (proc.stdin, proc.stdout):  # no file handle outlives the call
            try:
                pipe.close()
            except Exception:
                pass


def grok_billing(argv: list, timeout: float) -> dict:
    """``_x.ai/billing`` over ``grok agent --no-leader stdio`` (undocumented; the leading underscore is required).

    XAI_API_KEY is removed so the subscription login is used, and the auto-updater is disabled.
    """
    env = child_env(("XAI_API_KEY",))
    env["GROK_DISABLE_AUTOUPDATER"] = "1"
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": 1, "clientCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}}]
    deadline = time.monotonic() + timeout
    try:
        return stdio_rpc(argv, env, msgs, 2, timeout)
    except CmdError as exc:
        rem = deadline - time.monotonic()
        if exc.code != "unavailable" or rem < 1.0:
            raise
        return stdio_rpc(argv, env, msgs, 2, rem)


_GROK_PERIODS = {"USAGE_PERIOD_TYPE_WEEKLY": "weekly", "USAGE_PERIOD_TYPE_MONTHLY": "monthly"}


def src_grok(ctx, cfg) -> dict:
    argv = command_argv(cfg, ["grok", "agent", "--no-leader", "stdio"])
    billing = ctx.grok_rpc(argv, ctx.timeouts["grok"] - 3)["config"]
    period = billing["currentPeriod"]
    label = _GROK_PERIODS.get(period.get("type"))
    resets = norm_time(period.get("end"))
    if not label or not resets:
        raise CmdError("parse")
    raw_used = billing.get("creditUsagePercent")
    if raw_used is None:
        # grok 1.0.77 omits the field (proto3 JSON drops zero values) right after a period rolls over; accept that
        # only for a period that has already started, anything else stays a parse error (fail closed).
        started = norm_time(period.get("start"))
        if not started or parse_utc(started) > ctx.now:
            raise CmdError("parse")
        raw_used = 0
    used = int_pct(raw_used)
    observed = fmt_utc(ctx.now)
    pool = pool_from_windows(ctx.rules, ctx.now, "grok", "subscription", "percent", "grok", observed,
                             [{"label": label, "used_pct": used, "resets_at": resets}],
                             used=used, limit=100, remaining=100 - used)

    def val(key):
        node = billing.get(key)
        return float(node.get("val") or 0) if isinstance(node, dict) else 0.0

    # Prepaid balance or open on-demand headroom means the account is not hard-stopped at 100% of the plan.
    headroom = val("prepaidBalance") > 0 or val("onDemandCap") > val("onDemandUsed")
    if headroom and pool["state"] == "low":
        pool["state"], pool["advice"] = "watch", ctx.rules["advice"]["watch"]
    return {"observed_at": observed, "pools": [pool]}


def parse_cloud_credit(data: dict) -> "dict | None":
    """The usage bucket that carries ``limit_dollars`` (its codename key changes, so match on the field).

    Returns None when the OAuth usage response is valid but no promotional dollar bucket is active.
    """
    if not isinstance(data, dict):
        raise CmdError("parse")
    found = [b for b in data.values() if isinstance(b, dict) and b.get("limit_dollars") is not None]
    if not found:
        if isinstance(data.get("seven_day"), dict) or isinstance(data.get("limits"), list):
            return None
        raise CmdError("parse")
    bucket = min(found, key=lambda b: str(b.get("resets_at") or "9"))
    resets = norm_time(bucket.get("resets_at"))
    limit, used = int(round(100 * float(bucket["limit_dollars"]))), int(round(100 * float(bucket["used_dollars"])))
    if limit <= 0 or used < 0 or not resets:
        raise CmdError("parse")
    return {"used_cents": used, "limit_cents": limit, "resets_at": resets}


def _oauth_token(ctx, cfg) -> str:
    """The Claude OAuth access token. The credential may be the bare token or the JSON the Claude CLI stores
    (``claudeAiOauth.accessToken``). Parsed in memory; never logged or stored."""
    raw = read_credential(ctx, "claude_cloud", cfg)
    if raw.startswith("{"):
        try:
            raw = json.loads(raw)["claudeAiOauth"]["accessToken"]
        except (ValueError, KeyError, TypeError):
            raise CmdError("parse") from None
    if not isinstance(raw, str) or not raw.strip():
        raise CmdError("auth")
    return raw.strip()


def _fetch_cloud_usage(ctx, token: str) -> "tuple":
    headers = {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
               "User-Agent": USER_AGENT, "Accept": "application/json"}
    return ctx.http("GET", "https://api.anthropic.com/api/oauth/usage", headers, None, 15.0)


def src_claude_cloud(ctx, cfg) -> dict:
    cached = ctx.state.get("claude_cloud")
    fetched_at = norm_time(cached.get("fetched_at")) if isinstance(cached, dict) else None
    fetched = parse_utc(fetched_at) if fetched_at and valid_timestamp(fetched_at) else None
    if fetched and 0 <= (ctx.now - fetched).total_seconds() < CLAUDE_CLOUD_MIN_AGE and "credit" in cached:
        credit, observed = cached["credit"], fetched_at
    else:
        status, text = _fetch_cloud_usage(ctx, _oauth_token(ctx, cfg))
        if status == 401:  # expired access token: let the CLI refresh it once (best effort), then retry once
            try:
                ctx.runner(claude_argv(cfg, "-p", "/usage"), ctx.timeouts["claude_sub"], cwd=tempfile.gettempdir())
            except CmdError:
                pass
            status, text = _fetch_cloud_usage(ctx, _oauth_token(ctx, cfg))
        if status in (401, 403):
            raise CmdError("auth")
        if status == 429:
            raise CmdError("rate_limited")
        if status != 200:
            raise CmdError("unavailable")
        credit, observed = parse_cloud_credit(json.loads(text)), fmt_utc(ctx.now)
        ctx.state.put("claude_cloud", {"fetched_at": observed, "credit": credit})
    if credit is None:
        return {"observed_at": observed, "pools": []}
    used_pct = int_pct(100 * credit["used_cents"] / credit["limit_cents"])
    # The schema's window labels have no "credit expiry"; "monthly" carries the credit's expiry as its reset.
    windows = [{"label": "monthly", "used_pct": used_pct, "resets_at": credit["resets_at"]}]
    pool = pool_from_windows(
        ctx.rules, ctx.now, "claude-cloud", "credit", "usd_cents", "claude_cloud", observed, windows,
        used=credit["used_cents"], limit=credit["limit_cents"],
        remaining=max(0, credit["limit_cents"] - credit["used_cents"]))
    enforce_pool_freshness([pool], ctx.now, ctx.rules)
    return {"observed_at": observed, "pools": [pool]}


def src_api_probe(ctx, cfg) -> dict:
    """Daily free ``count_tokens`` probe: 200 means credit is available, 400 'credit balance is too low' exhausted.

    Produces no pool of its own: ``build_snapshot`` folds the result into the ``anthropic-api`` pool.
    """
    cached = ctx.state.get("api_probe")
    checked_at = norm_time(cached.get("checked_at")) if isinstance(cached, dict) else None
    checked = parse_utc(checked_at) if checked_at and valid_timestamp(checked_at) else None
    if checked and 0 <= (ctx.now - checked).total_seconds() < API_PROBE_MIN_AGE and cached.get("credit") in ("available", "exhausted"):
        return {"observed_at": checked_at, "credit_observed_at": checked_at, "pools": [], "api_credit": cached["credit"]}
    key = read_credential(ctx, "api_probe", cfg)
    model = cfg.get("model", "claude-haiku-4-5")
    if not (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model)):
        raise CmdError("not_configured")
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}]}).encode()
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
               "User-Agent": USER_AGENT}
    status, text = ctx.http("POST", "https://api.anthropic.com/v1/messages/count_tokens", headers, body, 12.0)
    if status == 200:
        credit = "available"
    elif status == 400 and "credit balance is too low" in text.lower():
        credit = "exhausted"
    elif status in (401, 403):
        raise CmdError("auth")
    else:
        raise CmdError("unavailable")
    observed = fmt_utc(ctx.now)
    ctx.state.put("api_probe", {"checked_at": observed, "credit": credit})
    return {"observed_at": observed, "credit_observed_at": observed, "pools": [], "api_credit": credit}


def src_github_billing(ctx, cfg) -> dict:
    """``github-actions`` pool: hosted Actions minutes this month against ``included_minutes``.

    ``owner`` is the organization (``owner_type`` "org", the default) or user ("user") whose billing is read
    through the authenticated ``gh`` CLI. Self-hosted minutes are not billed and not counted.
    """
    owner, kind, limit = cfg.get("owner"), cfg.get("owner_type", "org"), cfg.get("included_minutes")
    if not (isinstance(owner, str) and _OWNER.match(owner)) or kind not in ("org", "user") \
            or not _is_int(limit) or limit <= 0:
        raise CmdError("not_configured")
    base = f"/organizations/{owner}" if kind == "org" else f"/users/{owner}"
    gh = command_argv(cfg, ["gh"])
    gh_rate_guard(ctx, gh)
    data = load_json_line(ctx.runner(
        [*gh, "api", f"{base}/settings/billing/usage/summary"], ctx.timeouts["github_billing"]))
    items = [i for i in data["usageItems"] if i.get("product") == "Actions" and i.get("unitType") == "minutes"]
    minutes = int(round(sum(float(i["grossQuantity"]) for i in items)))
    remaining = max(0, limit - minutes)
    paid = any(float(i.get("netAmount", 0)) > 0 for i in data["usageItems"] if i.get("product", "").lower() == "actions")
    state = "low" if remaining == 0 else (
        "watch" if paid or remaining * 100 <= limit * ctx.rules["thresholds"]["github_actions_watch_remaining_pct"] else "healthy")
    observed = fmt_utc(ctx.now)
    windows = [{"label": "monthly", "used_pct": int_pct(100 * minutes / limit), "resets_at": next_month_start(ctx.now)}]
    runs_out_at = None
    try:
        days = [(ctx.now - timedelta(days=n)).date() for n in (1, 2, 3)]
        daily_items = []
        for year, month in sorted({(day.year, day.month) for day in days}):
            daily = load_json_line(ctx.runner(
                [*gh, "api", f"{base}/settings/billing/usage?year={year}&month={month}"],
                ctx.timeouts["github_billing"]))
            daily_items.extend(daily["usageItems"])
        burn = sum(float(i["quantity"]) for i in daily_items
                   if i.get("product", "").lower() == "actions"
                   and "minutes" in i.get("unitType", "").lower()
                   and i.get("date", "")[:10] in {day.isoformat() for day in days}) / 3
        if burn > 0:
            projected = ctx.now + timedelta(days=remaining / burn)
            if projected < parse_utc(windows[0]["resets_at"]):
                runs_out_at = fmt_utc(projected)
    except (CmdError, ValueError, TypeError, KeyError, OverflowError):
        pass
    return {"observed_at": observed, "pools": [make_pool(
        ctx.rules, "github-actions", "minutes", "minutes", state, "github_billing", observed, used=minutes,
        limit=limit, remaining=remaining, windows=windows, runs_out_at=runs_out_at)]}


OPENAI_REASONS = {"not_configured": "OpenAI admin key not configured", "auth": "OpenAI admin key rejected (401/403)",
                  "rate_limited": "OpenAI costs API rate limited", "timeout": "OpenAI costs API timed out",
                  "parse": "OpenAI costs response not understood"}


def _unavailable_pool(ctx, pid, kind, unit, source_id, reason) -> dict:
    """A pool with no figures and an 'Unavailable: <reason>' advice. The schema has no reason field or
    'unavailable' state, so the state is 'unknown' and the reason rides in the advice text."""
    pool = make_pool(ctx.rules, pid, kind, unit, "unknown", source_id, fmt_utc(ctx.now))
    pool["advice"] = clean("Unavailable: " + reason)
    return pool


def _openai_spend_cents(ctx, key: str, start: datetime) -> int:
    headers = {"Authorization": "Bearer " + key, "User-Agent": USER_AGENT}
    base = f"https://api.openai.com/v1/organization/costs?start_time={int(start.timestamp())}&bucket_width=1d&limit=31"
    total, page = 0.0, None
    for _ in range(5):  # at most five pages of daily buckets
        status, text = ctx.http("GET", base + (f"&page={page}" if page else ""), headers, None, 12.0)
        if status in (401, 403):
            raise CmdError("auth")
        if status == 429:
            raise CmdError("rate_limited")
        if status != 200:
            raise CmdError("unavailable")
        data = json.loads(text)
        for bucket in data["data"]:
            for res in bucket.get("results", []):
                amount = res.get("amount") or {}
                if str(amount.get("currency", "usd")).lower() != "usd":
                    raise CmdError("parse")
                total += float(amount.get("value") or 0)
        page = data.get("next_page") if data.get("has_more") else None
        if not page:
            return int(round(total * 100))
    raise CmdError("parse")


def src_openai_costs(ctx, cfg) -> dict:
    """OpenAI credit grant: remaining = ``grant_usd`` minus organization costs since ``grant_start``.

    ``expires`` (optional, with ``timezone``, default UTC) dates the grant's expiry. Any failure yields an
    'unavailable' pool with a reason instead of a source failure, so the snapshot always builds.
    """
    fail = lambda reason: {"observed_at": fmt_utc(ctx.now), "pools": [  # noqa: E731
        _unavailable_pool(ctx, "openai-api", "credit", "usd_cents", "openai_costs", reason)]}
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(cfg.get("timezone", "UTC"))
        start = datetime.fromisoformat(cfg["grant_start"]).replace(tzinfo=zone)
        expiry = (datetime.fromisoformat(cfg["expires"]).replace(tzinfo=zone).astimezone(UTC)
                  if cfg.get("expires") else None)
        limit = int(round(float(cfg["grant_usd"]) * 100))
        if limit <= 0:
            raise ValueError
    except (KeyError, ValueError, TypeError, ImportError):  # an unknown time zone is a KeyError
        return fail("grant_usd and grant_start are missing or invalid in the config")
    if expiry is not None and ctx.now >= expiry:
        return fail(f"grant expired {expiry.astimezone(zone).date()}; update the settings")
    try:
        spent = _openai_spend_cents(ctx, read_credential(ctx, "openai_costs", cfg), start)
    except CmdError as exc:
        return fail(OPENAI_REASONS.get(exc.code, "OpenAI costs API unavailable"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return fail(OPENAI_REASONS["parse"])
    windows = [{"label": "monthly", "used_pct": int_pct(100 * spent / limit),
                "resets_at": fmt_utc(expiry) if expiry is not None else None}]
    pool = pool_from_windows(ctx.rules, ctx.now, "openai-api", "credit", "usd_cents", "openai_costs", fmt_utc(ctx.now),
                             windows, used=spent, limit=limit, remaining=max(0, limit - spent))
    if expiry is not None:
        days_left = (expiry - ctx.now).total_seconds() / 86400
        if days_left < cfg.get("expiring_days", 14) and spent < limit * cfg.get("expiring_unused_pct", 50) / 100:
            pool["advice"] = f"Expiring unused credit: spend before {expiry.astimezone(zone).date()}"
    return {"observed_at": fmt_utc(ctx.now), "pools": [pool]}


ANTHROPIC_REASONS = {"not_configured": "Anthropic admin key not configured", "auth": "Anthropic admin key rejected (401/403)",
                     "rate_limited": "Anthropic cost report rate limited", "timeout": "Anthropic cost report timed out",
                     "parse": "Anthropic cost report response not understood"}
ANTHROPIC_NEARLY_SPENT = "Anthropic API nearly spent: no new API work until the next credit"


def _anthropic_month_cents(ctx, key: str, start: datetime) -> int:
    """Month-to-date cost from the Admin cost report. Each ``amount`` is a decimal string in USD cents."""
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": USER_AGENT}
    base = ("https://api.anthropic.com/v1/organizations/cost_report?starting_at="
            f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}&bucket_width=1d&limit=31")
    total, page = 0.0, None
    for _ in range(5):
        status, text = ctx.http("GET", base + (f"&page={page}" if page else ""), headers, None, 12.0)
        if status in (401, 403):
            raise CmdError("auth")
        if status == 429:
            raise CmdError("rate_limited")
        if status != 200:
            raise CmdError("unavailable")
        data = json.loads(text)
        for bucket in data["data"]:
            for res in bucket.get("results", []):
                if str(res.get("currency", "USD")).upper() != "USD":
                    raise CmdError("parse")
                total += float(res.get("amount") or 0)
        page = data.get("next_page") if data.get("has_more") else None
        if not page:
            return int(round(total))
    raise CmdError("parse")


def _spend_pool(ctx, pid: str, source_id: str, spent: int, limit: int, observed: str) -> dict:
    """Month-to-date API spend (USD cents) against a monthly credit; shared by the cost-report and ledger sources."""
    windows = [{"label": "monthly", "used_pct": int_pct(100 * spent / limit), "resets_at": next_month_start(ctx.now)}]
    pool = pool_from_windows(ctx.rules, ctx.now, pid, "credit", "usd_cents", source_id, observed, windows,
                             used=spent, limit=limit, remaining=max(0, limit - spent))
    if spent * 100 > limit * 80:
        pool["advice"] = ANTHROPIC_NEARLY_SPENT
    return pool


def src_anthropic_ledger(ctx, cfg) -> dict:
    """``anthropic-api-ledger`` pool: this month's spend from the local ledger that ``api_ledger.py record`` keeps.

    For API users with no Admin key (so no cost report). The ledger holds only token counts and computed cost,
    never a key, prompt or response; ``monthly_credit_usd`` in the settings is the limit. A summary written for an
    earlier month means nothing has been recorded this month yet (spend 0). No ledger file at all is
    ``not_configured``. The ledger is local and complete as of now, so the reading is dated at collection time.
    """
    now_obs = fmt_utc(ctx.now)
    try:
        limit = int(round(float(cfg["monthly_credit_usd"]) * 100))
        if limit <= 0:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return {"observed_at": now_obs, "pools": [_unavailable_pool(
            ctx, "anthropic-api-ledger", "credit", "usd_cents", "anthropic_ledger",
            "monthly_credit_usd is missing or invalid in the config")]}
    custom = cfg.get("ledger_dir")
    directory = Path(custom).expanduser() if isinstance(custom, str) and custom else ctx.state_dir
    if directory is None:
        raise CmdError("not_configured")
    try:
        summary = json.loads((Path(directory) / LEDGER_SUMMARY_FILE).read_text())
    except FileNotFoundError:
        raise CmdError("not_configured") from None
    except (OSError, ValueError):
        raise CmdError("parse") from None
    spent_raw = summary.get("spent_cents") if isinstance(summary, dict) else None
    if not _is_int(spent_raw) or spent_raw < 0:
        raise CmdError("parse")
    spent = spent_raw if summary.get("month") == ctx.now.strftime("%Y-%m") else 0
    return {"observed_at": now_obs, "pools": [_spend_pool(ctx, "anthropic-api-ledger", "anthropic_ledger", spent, limit, now_obs)]}


def src_anthropic_costs(ctx, cfg) -> dict:
    """``anthropic-api`` pool: month-to-date Admin cost report against ``monthly_credit_usd``.

    Any failure yields an 'unavailable' pool with a reason; a rejected key (401/403) also raises a credential
    ask. The Admin key never leaves this process (not in argv, env or snapshot).
    """
    now_obs = fmt_utc(ctx.now)
    asks: list = []

    def fail(reason, ask=False):
        if ask:
            asks.append({"id": "anthropic-credential-rejected", "title": "Anthropic admin key rejected",
                         "detail": clean("The Anthropic cost report API refused the configured Admin key; "
                                         "replace it with a valid one.", 280),
                         "kind": "at_desk", "since": now_obs, "link_ref": None})
        return {"observed_at": now_obs, "asks": asks, "pools": [
            _unavailable_pool(ctx, "anthropic-api", "credit", "usd_cents", "anthropic_costs", reason)]}
    try:
        limit = int(round(float(cfg["monthly_credit_usd"]) * 100))
        if limit <= 0:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return fail("monthly_credit_usd is missing or invalid in the config")
    start = ctx.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        spent = _anthropic_month_cents(ctx, read_credential(ctx, "anthropic_costs", cfg), start)
    except CmdError as exc:
        return fail(ANTHROPIC_REASONS.get(exc.code, "Anthropic cost report unavailable"), ask=exc.code == "auth")
    except (ValueError, KeyError, TypeError, AttributeError):
        return fail(ANTHROPIC_REASONS["parse"])
    return {"observed_at": now_obs, "pools": [_spend_pool(ctx, "anthropic-api", "anthropic_costs", spent, limit, now_obs)],
            "asks": asks}


SOURCES = [("muse", src_muse), ("codex", src_codex), ("agy", src_agy), ("colab", src_colab),
           ("claude_sub", src_claude_sub), ("grok", src_grok), ("claude_cloud", src_claude_cloud),
           ("github_billing", src_github_billing), ("anthropic_costs", src_anthropic_costs),
           ("openai_costs", src_openai_costs), ("anthropic_ledger", src_anthropic_ledger), ("api_probe", src_api_probe)]
assert tuple(sid for sid, _ in SOURCES) == SOURCE_IDS


class Ctx:
    def __init__(self, now, rules, config, runner, env, codex_rpc=codex_rate_limits, grok_rpc=grok_billing,
                 http=http_request, state=None, state_dir=None):
        self.now, self.rules, self.config, self.runner, self.env = now, rules, config, runner, env
        self.codex_rpc, self.grok_rpc, self.http = codex_rpc, grok_rpc, http
        self.state, self.state_dir = state or UsageState(), state_dir
        self.timeouts = source_timeouts(config)
        self.gh_lock, self.gh_limit = threading.Lock(), None


ASK_KIND_RANK = {"confirm": 0, "at_desk": 1, "phone_ok": 2, "deferred": 3}


def select_asks(asks: list, cap: int) -> list:
    """At most ``cap`` asks, ordered by kind (confirm, at_desk, phone_ok, deferred), then ``since``, then id, so the
    cut never depends on read order."""
    def key(a):
        return (ASK_KIND_RANK.get(a.get("kind"), len(ASK_KIND_RANK)), a.get("since") or "", a.get("id") or "")
    return sorted(asks, key=key)[:cap]


def run_source(fn, ctx, seconds: float = SOURCE_TIMEOUT, retries: int = 1):
    """Returns (result|None, error_code|None). One failure never affects another source.

    A timeout or unavailable result is retried ``retries`` times; parse/auth/stale/not_configured are not.
    """
    code = "unavailable"
    for _ in range(retries + 1):
        try:
            return with_timeout(lambda: fn(ctx), seconds), None
        except CmdError as exc:
            code = exc.code
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            return None, "parse"
        except Exception:
            code = "unavailable"
        if code not in RETRY_CODES:
            break
    return None, code


def fold_api_credit(rules, pools, credit, observed, now: "datetime | None" = None) -> None:
    """Fold the daily count_tokens probe into the ``anthropic-api`` pool (it never adds a pool of its own).

    Only an eligible probe (<=6 h old with a valid timestamp) may affect a fresh ledger or produce a healthy/low
    state-only pool. Exhausted forces ``low`` over any fresh ledger reading without changing the ledger's
    measurement timestamp; available never upgrades ledger state. A stale or undated ledger stays ``unknown``.
    Without a ledger, a stale or undated probe produces an ``unknown`` pool with explanatory advice and null figures.
    """
    now_dt = now or datetime.now(UTC)
    probe_obs = norm_time(observed) if isinstance(observed, str) else None
    if probe_obs and not valid_timestamp(probe_obs):
        probe_obs = None
    eligible = observation_is_fresh(probe_obs, now_dt, rules)
    pool = next((p for p in pools if p["id"] == "anthropic-api"), None)
    if pool is None:
        if eligible:
            state = "low" if credit == "exhausted" else "healthy"
            pools.append(make_pool(rules, "anthropic-api", "credit", "usd_cents", state, "api_probe", probe_obs))
            pools[-1]["advice"] = rules["advice"].get(state)
        else:
            item = make_pool(rules, "anthropic-api", "credit", "usd_cents", "unknown", "api_probe", probe_obs)
            suppress_pool_measurements(item, rules, "stale" if probe_obs else "missing_timestamp")
            pools.append(item)
    elif eligible and credit == "exhausted" and pool["state"] not in ("low", "unknown"):
        pool["state"], pool["advice"] = "low", rules["advice"]["low"]


# ---------------------------------------------------------------- snapshot
# Pools a failed source leaves behind in state unknown: (pool id, kind, unit). agy and Colab depend on the account
# labels and claude_sub on its ``fable`` setting (``failure_rows``); api_probe has no pool of its own.
FALLBACK_POOLS = {
    "muse": (("muse", "subscription", "percent"),),
    "codex": (("codex", "subscription", "percent"),),
    "claude_sub": (("claude-subscription", "subscription", "percent"), ("claude-fable", "subscription", "percent")),
    "grok": (("grok", "subscription", "percent"),),
    "claude_cloud": (("claude-cloud", "credit", "usd_cents"),),
    "github_billing": (("github-actions", "minutes", "minutes"),),
    "anthropic_costs": (("anthropic-api", "credit", "usd_cents"),),
    "anthropic_ledger": (("anthropic-api-ledger", "credit", "usd_cents"),),
    "openai_costs": (("openai-api", "credit", "usd_cents"),),
}


def account_labels(cfg: dict) -> list:
    """The valid account labels in an agy or Colab source's settings (``default`` when there are none)."""
    raw = cfg.get("accounts")
    labels = [a["label"] for a in raw if isinstance(a, dict) and isinstance(a.get("label"), str)
              and _PROFILE_ID.match(a["label"])] if isinstance(raw, list) else []
    return list(dict.fromkeys(labels)) or ["default"]


def failure_rows(sid: str, cfg: dict) -> list:
    """(pool id, kind, unit, label or None) for each pool a failed source stands for."""
    if sid == "agy":
        return [("agy-" + lab + "-unavailable", "subscription", "percent", None) for lab in account_labels(cfg)]
    if sid == "colab":
        return [("colab-" + lab + "-units", "credit", "credits", f"Colab compute units ({lab})")
                for lab in account_labels(cfg)]
    rows = [(*row, None) for row in FALLBACK_POOLS.get(sid, ())]
    if sid == "claude_sub" and cfg.get("fable", True) is False:
        rows = rows[:1]
    return rows


def failure_pools(rules: dict, sid: str, cfg: dict, code: "str | None") -> list:
    """Pools in state ``unknown`` for a source that failed, so a broken source is visible instead of absent."""
    advice = clean("Unavailable: " + FAILURE_REASONS.get(code, FAILURE_REASONS["unavailable"]))
    pools = []
    for pid, kind, unit, label in failure_rows(sid, cfg):
        pool = make_pool(rules, pid, kind, unit, "unknown", sid, None)
        pool["advice"] = advice
        if label:
            pool["label"] = label
        pools.append(pool)
    return pools


def build_snapshot(now, rules, config, runner=run_cmd, state_dir=None, env=None, codex_rpc=codex_rate_limits,
                   grok_rpc=grok_billing, http=http_request) -> dict:
    """Build one snapshot from the enabled sources in ``config``.

    Nothing is carried over from the previous snapshot. ``state_dir`` holds only ``usage-state.json``: derived
    readings for the cached Claude cloud and API-probe polls and the last-known Muse weekly value. It never holds
    a token or key. With ``state_dir=None`` nothing touches the disk.
    """
    env = os.environ if env is None else env
    state_path = Path(state_dir) if state_dir else None
    state = UsageState(state_path / USAGE_STATE_FILE if state_path else None)
    ctx = Ctx(now, rules, config, runner, env, codex_rpc, grok_rpc, http, state, state_path)
    registry = dict(SOURCES)
    active = [(sid, registry[sid], cfg) for sid, cfg in enabled_sources(config)]
    outcomes = {}

    def go(sid, fn, cfg):
        outcomes[sid] = run_source(lambda c: fn(c, cfg), ctx, ctx.timeouts[sid])

    threads = [threading.Thread(target=go, args=item, daemon=True) for item in active]
    for t in threads:
        t.start()
    for t, (sid, _, _) in zip(threads, active):
        t.join(2 * ctx.timeouts[sid] + 2)
    sources, pools, asks = [], [], []
    for sid, _, _ in active:
        result, code = outcomes.get(sid, (None, "timeout"))
        sources.append({"id": sid, "ok": result is not None, "observed_at": result["observed_at"] if result else None,
                        "error_code": ERROR_CODES.get(code, "unavailable") if code else None})
        if result:
            pools += result.get("pools", [])
            asks += result.get("asks", [])
    enforce_pool_freshness(pools, now, rules)
    probe = outcomes.get("api_probe", (None, None))[0]
    if probe:
        fold_api_credit(rules, pools, probe["api_credit"],
                        probe["credit_observed_at"] if "credit_observed_at" in probe else probe.get("observed_at"), now)
    merge_pools_and_pace(pools, now, rules, ctx.state)
    enforce_pool_freshness(pools, now, rules)
    agy = next((cfg for sid, _, cfg in active if sid == "agy"), None)
    if agy is not None:
        asks += shared_login_asks(ctx.state, pools, now, account_labels(agy))
    for source, (sid, _, cfg) in zip(sources, active):
        if not source["ok"]:
            pools += failure_pools(rules, sid, cfg, source["error_code"])

    for p in pools:  # ``pool_notes`` from the config ride in the advice of the pool they name
        note = config.get("pool_notes", {}).get(p["id"])
        if note:
            p["advice"] = clean(f"{p['advice']}. {note}" if p["advice"] else note)

    omitted = 0
    kept, seen = [], set()
    for ask in asks:
        blank = any(not isinstance(ask.get(key), str) or not ask[key].strip() for key in ("title", "detail"))
        if ask.get("id") in seen or blank or record_has_secret(ask):
            continue  # one bad record must not refuse the whole snapshot: drop it and count it in `omitted`
        seen.add(ask["id"])
        kept.append(ask)
    omitted += len(asks) - len(kept)
    out_asks = select_asks(kept, CAPS["asks"])
    out_pools = pools[:CAPS["pools"]]
    resets = []
    for p in out_pools:
        base_label = re.sub(r"\s+\((?:weekly|5h|monthly)\)\Z", "", p["label"])
        for w in p["windows"]:
            if w["resets_at"] and parse_utc(w["resets_at"]) > now:
                resets.append({"at": w["resets_at"], "pool_id": p["id"], "effect": "reset", "kind": "reset",
                               "label": clean(f"{base_label} {w['label']}", 120)})
    resets.sort(key=lambda r: r["at"])
    out_resets = resets[:CAPS["resets"]]
    omitted += (len(kept) - len(out_asks)) + (len(pools) - len(out_pools)) + (len(resets) - len(out_resets))
    return {"schema": "fleet-status/v1", "generated_at": fmt_utc(now), "collector_id": config["collector_id"],
            "sources": sources, "asks": out_asks, "pools": out_pools, "resets": out_resets,
            "work": [], "landed": [], "landed_count_today": 0, "closed_wp_today": 0,
            "truncated": omitted > 0, "omitted": omitted}


def encode_body(snapshot: dict, pretty: bool = False, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Serialise; refuse on a secret pattern or when the body is over the size cap."""
    if structure_has_secret(snapshot):
        raise OutputRefused("secret")
    text = json.dumps(snapshot, indent=2, sort_keys=True) if pretty else json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    body = (text + "\n").encode("utf-8")
    if len(body) > max_bytes:
        raise OutputRefused("size")
    return body


class Log:
    """Timestamps, fixed words and key=value codes only: no values, titles, paths or output."""

    def __init__(self, path: Path, now: datetime):
        self.path, self.now = path, now
        path.touch(mode=0o600, exist_ok=True)
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.write_text("\n".join(path.read_text().splitlines()[-200:]) + "\n")

    def line(self, text: str) -> None:
        with open(self.path, "a") as fh:
            fh.write(f"{fmt_utc(self.now)} {text}\n")


def write_atomic(path: Path, body: bytes) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv=None, runner=run_cmd, now=None, env=None, codex_rpc=codex_rate_limits, grok_rpc=grok_billing,
         http=http_request, stdout=None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(
        prog="collect.py", description="Collect a fleet-status/v1 usage snapshot from the sources enabled in a config file "
        "and print it as JSON (or write it with --output). Exit codes: 0 success (sources that failed are reported "
        "as state unknown in the feed), 2 config or usage error, 3 the snapshot was refused (schema or secret check).")
    ap.add_argument("--config", default=env.get("FLEET_STATUS_CONFIG"),
                    help="config file (default: $FLEET_STATUS_CONFIG); see fleet-status.example.json")
    ap.add_argument("--state-dir", default=None,
                    help="where usage-state.json and collector.log live (default: state_dir from the config, "
                         "else " + DEFAULT_STATE_DIR + ")")
    ap.add_argument("--output", default="-", help="write the feed to this file (atomically, mode 0600); - is stdout")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON")
    ap.add_argument("--rules", default=str(HERE / "rules.json"), help="thresholds, advice text and pool labels")
    ap.add_argument("--schema", default=str(HERE / "fleet-status-v1.schema.json"),
                    help="JSON Schema the feed is validated against before it is emitted")
    args = ap.parse_args(argv)
    if not args.config:
        print("error: no config file; pass --config or set FLEET_STATUS_CONFIG (see fleet-status.example.json)",
              file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        rules = json.loads(Path(args.rules).read_text())
        schema = json.loads(Path(args.schema).read_text())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("error: cannot read the rules or schema file", file=sys.stderr)
        return 2
    if not enabled_sources(config):
        print("error: no sources are enabled in the config", file=sys.stderr)
        return 2

    now = now or datetime.now(UTC).replace(microsecond=0)
    state = Path(args.state_dir or config["state_dir"] or DEFAULT_STATE_DIR).expanduser()
    try:
        created = not state.exists()
        state.mkdir(parents=True, exist_ok=True)
        if created:  # a directory that was already there keeps its permissions
            os.chmod(state, 0o700)
        log = Log(state / "collector.log", now)
    except OSError:
        print("error: cannot use the state directory", file=sys.stderr)
        return 2
    snap = build_snapshot(now, rules, config, runner, state_dir=state, env=env, codex_rpc=codex_rpc,
                          grok_rpc=grok_rpc, http=http)
    failed = [s["id"] for s in snap["sources"] if not s["ok"]]
    for source in snap["sources"]:
        if not source["ok"]:
            log.line(f"source failed id={source['id']} code={source['error_code']}")
    log.line(f"snapshot sources={len(snap['sources'])} sources_failed={len(failed)} pools={len(snap['pools'])} "
             f"asks={len(snap['asks'])}")
    try:
        errors = validate(snap, schema)
    except SchemaError:
        errors = ["schema unsupported"]
    if errors:
        log.line(f"refused reason=schema errors={len(errors)}")
        print("error: the snapshot did not validate against the schema (see collector.log)", file=sys.stderr)
        return 3
    try:
        body = encode_body(snap, args.pretty)
    except OutputRefused as exc:
        log.line(f"refused reason={exc}")
        print(f"error: the snapshot was refused ({exc}); see collector.log", file=sys.stderr)
        return 3
    if args.output == "-":
        out = stdout or sys.stdout
        out.write(body.decode("utf-8"))
        out.flush()
    else:
        try:
            target = Path(args.output).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(target, body)
        except OSError:
            print("error: cannot write the output file", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
