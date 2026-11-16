#!/usr/bin/env python3
"""Local Anthropic API spend ledger: a record of your own API calls, for people with no Admin key.

The Anthropic cost report needs an Admin key, which individual accounts do not have. Instead, record every API
call you make from its own response usage, and let the collector's ``anthropic_ledger`` source read the total:

* ``api-ledger-events.jsonl``: append-only, one JSON line per call (month, model, token counts, cost). Never
  rewritten; never holds a key, a prompt or a response body.
* ``api-ledger.json``: the month summary the collector reads,
  ``{"month","spent_cents","observed_at","basis":"local_events"}``, rebuilt from the events of the current month
  after each append (month-scoped: an earlier month's events never count).

Cost is computed from published per-token prices (``PRICES``: cents per million tokens, Anthropic first-party API
rates as of 2026-10-06; cache reads 0.1x input, 5-minute cache writes 1.25x, 1-hour writes 2x). Check them against
the current price list and edit ``PRICES`` when they change. A model with no entry is refused (exit 2, nothing
recorded) rather than guessed.

One-line hook (reads a ``claude -p --output-format json`` result or a Messages API response on stdin, writes it
through unchanged, records the usage)::

    your-api-command | python3 -I api_ledger.py record --tee

The ledger and the collector must use the same state directory: pass the same ``--state-dir`` here as the
collector's ``state_dir`` (default ``~/.local/state/fleet-status``), or set ``ledger_dir`` in the source settings.

Python 3.9+ on macOS or Linux (it locks the events file with ``fcntl``), standard library only, no network.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_DIR = os.path.expanduser("~/.local/state/fleet-status")
EVENTS_FILE = "api-ledger-events.jsonl"
SUMMARY_FILE = "api-ledger.json"
# cents per million tokens: (input, output). Source: Anthropic published API pricing, 2026-10-06.
PRICES = {
    "claude-fable-5-1": (1000, 5000), "claude-mythos-5-1": (1000, 5000), "claude-fable-5": (1000, 5000),
    "claude-opus-5-5": (400, 2000), "claude-opus-5": (500, 2500), "claude-opus-4-8": (500, 2500),
    "claude-opus-4-7": (500, 2500), "claude-opus-4-6": (500, 2500),
    "claude-sonnet-5-5": (200, 1000), "claude-sonnet-5": (200, 1000), "claude-sonnet-4-6": (300, 1500),
    "claude-haiku-5-5": (10, 50), "claude-haiku-4-5": (100, 500),
}
# Multipliers in percent of the input price.
READ_PCT, WRITE_5M_PCT, WRITE_1H_PCT = 10, 125, 200
UNITS_PER_CENT = 10 ** 8  # cost is held as an integer: tokens * cents_per_mtok * pct (1e6 tokens * 100 pct = 1e8)


class LedgerError(Exception):
    """Unusable input; the message never carries content from the input."""


def canonical_model(name: str) -> str:
    """Strip a date suffix or a ``[1m]``-style tag so ``claude-haiku-4-5-20251001`` prices as ``claude-haiku-4-5``."""
    base = str(name).split("[")[0]
    if base in PRICES:
        return base
    head, _, tail = base.rpartition("-")
    if tail.isdigit() and len(tail) == 8 and head in PRICES:
        return head
    raise LedgerError("unpriced_model")


def cost_units(model: str, inp: int, out: int, read: int, write_5m: int, write_1h: int) -> int:
    in_rate, out_rate = PRICES[canonical_model(model)]
    return (inp * in_rate * 100 + out * out_rate * 100 + read * in_rate * READ_PCT
            + write_5m * in_rate * WRITE_5M_PCT + write_1h * in_rate * WRITE_1H_PCT)


def _n(value) -> int:
    n = int(value or 0)
    if n < 0:
        raise LedgerError("negative_tokens")
    return n


def _from_usage(model, usage: dict) -> dict:
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        w5, w1 = _n(cc.get("ephemeral_5m_input_tokens")), _n(cc.get("ephemeral_1h_input_tokens"))
    else:
        w5, w1 = _n(usage.get("cache_creation_input_tokens")), 0
    return {"model": str(model), "input": _n(usage.get("input_tokens")), "output": _n(usage.get("output_tokens")),
            "cache_read": _n(usage.get("cache_read_input_tokens")), "cache_write_5m": w5, "cache_write_1h": w1}


def parse_usage(doc) -> "tuple[list[dict], str | None]":
    """Return (per-model token records, dedupe id) from a Messages API response or a ``claude -p`` JSON result."""
    if not isinstance(doc, dict):
        raise LedgerError("unrecognised_input")
    ident = doc.get("id") or doc.get("session_id")
    ident = str(ident) if isinstance(ident, str) else None
    mu = doc.get("modelUsage")
    if isinstance(mu, dict) and mu:  # claude CLI result: camelCase counts per model
        recs = []
        for model, u in mu.items():
            if not isinstance(u, dict):
                raise LedgerError("unrecognised_input")
            recs.append({"model": str(model), "input": _n(u.get("inputTokens")), "output": _n(u.get("outputTokens")),
                         "cache_read": _n(u.get("cacheReadInputTokens")),
                         "cache_write_5m": _n(u.get("cacheCreationInputTokens")), "cache_write_1h": 0})
        return recs, ident
    if isinstance(doc.get("usage"), dict) and doc.get("model"):  # Messages API response
        return [_from_usage(doc["model"], doc["usage"])], ident
    raise LedgerError("unrecognised_input")


def month_of(now: datetime) -> str:
    return now.strftime("%Y-%m")


def read_events(path: Path) -> list:
    events = []
    try:
        for line in path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # a torn line never blocks the ledger
            if isinstance(ev, dict):
                events.append(ev)
    except OSError:
        pass
    return events


def summarise(events: list, now: datetime) -> dict:
    month = month_of(now)
    mine = [e for e in events if e.get("month") == month]
    units = sum(int(e.get("cost_units", 0)) for e in mine)
    last = max((e.get("at", "") for e in mine), default="")
    return {"month": month, "spent_cents": -(-units // UNITS_PER_CENT),
            "observed_at": last or now.strftime("%Y-%m-%dT%H:%M:%SZ"), "basis": "local_events"}


def record(state_dir, doc, now: "datetime | None" = None) -> dict:
    """Append one event per model in ``doc`` and rewrite the summary. Returns the summary."""
    now = now or datetime.now(timezone.utc)
    recs, ident = parse_usage(doc)
    stamped = []
    for rec in recs:  # price everything first: an unpriced model records nothing
        stamped.append(dict(rec, cost_units=cost_units(rec["model"], rec["input"], rec["output"], rec["cache_read"],
                                                       rec["cache_write_5m"], rec["cache_write_1h"])))
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    events_path, summary_path = state / EVENTS_FILE, state / SUMMARY_FILE
    with open(state / (EVENTS_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = read_events(events_path)
        if ident and any(e.get("id") == ident for e in existing):
            return summarise(existing, now)
        at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = "".join(json.dumps(dict(s, month=month_of(now), at=at, **({"id": ident} if ident else {})),
                                   sort_keys=True, separators=(",", ":")) + "\n" for s in stamped)
        fd = os.open(events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(lines)
        summary = summarise(read_events(events_path), now)
        tmp = Path(str(summary_path) + ".tmp")
        tmp.write_text(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, summary_path)
    return summary


def main(argv=None, stdin=None, stdout=None, now=None) -> int:
    ap = argparse.ArgumentParser(description="Record Anthropic API usage in the local spend ledger.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="read a JSON result on stdin and record its usage")
    rec.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    rec.add_argument("--tee", action="store_true", help="write stdin to stdout unchanged (for use mid-pipe)")
    args = ap.parse_args(argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    text = stdin.read()
    if args.tee:
        stdout.write(text)
        stdout.flush()
    try:
        # A CLI stream may carry several JSON lines; the result line is the one with usage.
        docs = []
        for line in [text] + text.splitlines():
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue
        last_err = LedgerError("unrecognised_input")
        for doc in reversed(docs):
            try:
                record(args.state_dir, doc, now)
                return 0
            except LedgerError as exc:
                last_err = exc
        raise last_err
    except LedgerError as exc:
        print(f"api_ledger: not recorded ({exc})", file=sys.stderr)
        return 2
    except OSError:
        print("api_ledger: not recorded (io_error)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
