#!/usr/bin/env python3
"""Builds the sample feed in examples/ and the README excerpt from the test fixtures (no accounts, no network).

``python3 tests/example.py --write`` regenerates both; ``test_example.py`` fails when they are out of date.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import CODEX_RPC, ROOT, FakeHttp, FakeRunner, build, fc, make_config, openai_body  # noqa: E402

SAMPLE = ROOT / "examples" / "sample-output.json"
README = ROOT / "README.md"
START, END = "<!-- example:start -->", "<!-- example:end -->"
EXCERPT_POOLS = ("muse", "claude-subscription", "github-actions", "anthropic-api")


class TwoAccountRunner(FakeRunner):
    """The second agy account has used more of its allowance than the first."""

    def __call__(self, argv, timeout, cwd=None):
        out = super().__call__(argv, timeout, cwd)
        if "agy" in argv and "HOME=/fixture/agy-b" in argv:
            return re.sub(r'("remaining_fraction":\s*)0\.99[0-9]*', r"\g<1>0.62", out)
        return out


def example_snapshot():
    config = make_config()
    config["collector_id"] = "my-machine"
    config["pool_notes"] = {"anthropic-api": "Monthly credit is a budget I chose, not a vendor limit"}
    with tempfile.TemporaryDirectory() as t:
        return build(t, TwoAccountRunner(), config=config, http=FakeHttp(openai=((200, openai_body(42.5)),)),
                     codex_rpc=lambda argv, timeout: CODEX_RPC)


def sample_text(snap=None):
    return json.dumps(snap or example_snapshot(), indent=2, sort_keys=True) + "\n"


def excerpt_text(snap=None):
    snap = snap or example_snapshot()
    shown = {**snap, "pools": [p for p in snap["pools"] if p["id"] in EXCERPT_POOLS], "resets": snap["resets"][:3]}
    return json.dumps(shown, indent=2, sort_keys=True)


def readme_block(snap=None):
    snap = snap or example_snapshot()
    note = (f"Trimmed for the README: {len(EXCERPT_POOLS)} of {len(snap['pools'])} pools and 3 of {len(snap['resets'])} "
            "resets are shown; `examples/sample-output.json` has the whole feed.")
    return f"{START}\n{note}\n\n```json\n{excerpt_text(snap)}\n```\n{END}"


def main():
    if "--write" not in sys.argv:
        print(__doc__)
        return 2
    snap = example_snapshot()
    assert fc.validate(snap, json.loads((ROOT / "fleet-status-v1.schema.json").read_text())) == []
    SAMPLE.parent.mkdir(exist_ok=True)
    SAMPLE.write_text(sample_text(snap))
    text = README.read_text()
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    README.write_text(head + readme_block(snap) + tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
