# fleet-status

One JSON feed of how much of each AI subscription and API budget you have left.

`collect.py` reads the sources you enable (Claude, including the separate Fable weekly limit, Codex, agy, Muse, Grok, Colab compute units, GitHub Actions minutes, and Anthropic and OpenAI API spend), works out a state for each pool (`healthy`, `watch`, `spend_first`, `low`, `unknown`) and prints one `fleet-status/v1` document. Use it to drive a dashboard, a status bar, a pager rule, or a script that decides which provider gets the next job.

- Standard library only. Python 3.9 or newer, macOS or Linux. No server, no database, no telemetry.
- Every source is optional and switched on in one JSON config file. Credentials come from environment variables or a command you supply, never from the config itself.
- A source that is missing, broken or out of date becomes a pool in state `unknown`. It never fails the run.
- The feed validates against the bundled JSON Schema (`fleet-status-v1.schema.json`). The tests run from fixtures and need no accounts.

## What it measures

| Pool id | What it is | Windows | Source id |
|---|---|---|---|
| `muse` | Muse Code subscription allowance | 5h, weekly | `muse` |
| `codex` | Codex rate limit | 5h and/or weekly | `codex` |
| `codex-credits` | Codex credit balance | | `codex` |
| `codex-reset-credits` | Rate-limit reset credits you can still use | | `codex` |
| `agy-<account>-<group>` | One pool per agy model group, per account | 5h, weekly | `agy` |
| `colab-<account>-units` | Colab compute units left | | `colab` |
| `claude-subscription` | Claude subscription limits, all models | 5h, weekly | `claude_sub` |
| `claude-fable` | Claude Fable weekly limit | weekly | `claude_sub` |
| `claude-cloud` | Promotional Claude cloud credit, in dollars | monthly (its expiry) | `claude_cloud` |
| `grok` | Grok subscription credit | weekly or monthly | `grok` |
| `github-actions` | Hosted Actions minutes against what your plan includes | monthly | `github_billing` |
| `anthropic-api` | Anthropic API spend this month against a monthly credit you set | monthly | `anthropic_costs` (and `api_probe`) |
| `anthropic-api-ledger` | The same, from a ledger of your own calls (no Admin key needed) | monthly | `anthropic_ledger` |
| `openai-api` | OpenAI API spend since a credit grant started, against the grant | until it expires | `openai_costs` |

Every pool carries `used`, `limit`, `remaining`, its `windows` (percent used and reset time), a `state`, a one-line `advice`, the time the figure was `observed_at`, and, for percentage windows, a burn-rate forecast (`pace_pct_per_hr`, `runs_out_at`).

States come from `rules.json`:

| State | When |
|---|---|
| `low` | 10% or less remaining |
| `spend_first` | 60% or more remaining and the window resets within 48 hours: use this pool first |
| `watch` | 30% or less remaining |
| `healthy` | otherwise |
| `unknown` | no reading, an unreadable source, a window that already rolled over, or a reading more than 6 hours old (the figures are blanked, not carried forward) |

A reading older than the 6 hour limit is never shown as current. The thresholds, the advice text and the pool labels are all in `rules.json`; edit that file or pass `--rules`.

## How each source is read

Only the first four rows use documented, supported APIs. Everything else parses the output or a private protocol of a vendor tool, which the vendor can change at any time. When that happens the source reports `parse_error` (or `unavailable`) and its pool goes to `unknown`; nothing else is affected. Using an undocumented interface may also be against a provider's terms: read them and enable only what you are comfortable with. The example config leaves the least stable one, `claude_cloud`, switched off.

| Source id | What runs or is called | Credential | Reliability |
|---|---|---|---|
| `anthropic_costs` | `GET https://api.anthropic.com/v1/organizations/cost_report`, month to date, daily buckets. Amounts are USD cents. | An Anthropic Admin key (organization accounts only) | Documented API |
| `openai_costs` | `GET https://api.openai.com/v1/organization/costs` from the grant start date, paged. | An OpenAI admin key | Documented API |
| `api_probe` | Once a day, a `POST https://api.anthropic.com/v1/messages/count_tokens` with one word. A 200 means credit is available; a 400 "credit balance is too low" means exhausted. It folds into `anthropic-api` and has no pool of its own. | An Anthropic API key | Documented endpoint, documented as free to call |
| `github_billing` | `gh api /organizations/<owner>/settings/billing/usage/summary` (or `/users/<owner>/...`), summing hosted Actions minutes. For the run-out forecast it also reads `.../settings/billing/usage?year=&month=` for the last three full days. | Your `gh` login | Documented REST endpoints; the summary endpoints are in public preview, and the daily one needs GitHub's enhanced billing platform (without it there is no forecast) |
| `claude_sub` | `claude -p /usage --output-format json` with every auth environment variable removed, so the subscription login is used. The `Current week` and `Current session` text is parsed; local reset times are converted to UTC. Per-model lines are skipped except the Fable line. | The `claude` CLI login | Unofficial: the text format can change. The run is refused unless it made no model turn (`num_turns` is 0). |
| `claude_cloud` | `GET https://api.anthropic.com/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20`. The bucket with a `limit_dollars` field is the credit (its key name changes, so it is matched by field). Polled at most every 15 minutes; the answer is cached. On a 401 it runs `claude -p /usage` once to let the CLI refresh the token, then retries once. | A Claude OAuth token | Unofficial: an undocumented endpoint that uses your Claude login token. Opt in only if you accept that. |
| `codex` | Starts `codex app-server` and sends the JSON-RPC call `account/rateLimits/read` (`OPENAI_API_KEY` is removed from the child, and the child is always killed). If that fails it falls back to the last `rate_limits` event in the newest Codex session files (only the newest day folders and the last 2 MiB of each file). | The `codex` CLI login | Unofficial: not a documented usage API |
| `agy` | `agy -p /usage --output-format json`, once per configured account, optionally under that account's own `HOME` (`env HOME=<home> agy ...`). Pools are built from `command.data.groups[].buckets[]`. | The `agy` CLI login of each account | Unofficial: the JSON layout can change. A signed-out `agy` may try to open a browser to sign in; sign in first, or wrap the command (see `command`) so it cannot. |
| `muse` | `muse usage --json`, reading `allowance.window` and `allowance.weekly`. When the CLI says `server_sent_no_usage` it reports nothing about current use, so no zero is invented: the last weekly reading is kept (dated when it was seen) until its reset passes, then the pool is `unknown`. | The `muse` CLI login | Unofficial |
| `grok` | Starts `grok agent --no-leader stdio` and sends the JSON-RPC call `_x.ai/billing` (the leading underscore is required). `XAI_API_KEY` is removed from the child and the auto-updater is switched off. Reads `creditUsagePercent` over the current weekly or monthly period; at 100% the state is `low`, or `watch` while prepaid balance or on-demand headroom remains. | The `grok` CLI login | Unofficial: an undocumented call |
| `colab` | `<command> usage`, parsing `Current balance: N` (compute units), once per account. With `expect_email` set it first runs `<command> whoami` and requires `Email: <that address>`, otherwise the pool is `unknown` (the address is compared in memory and never published). Written against the [Colab CLI](https://github.com/googlecolab/google-colab-cli). | The Colab CLI login of each account | Unofficial: parses its text output |
| `anthropic_ledger` | Reads `api-ledger.json`, the month summary that `api_ledger.py` keeps from your own API calls (see below). No network. | None | Local file |

No source sends a prompt to a model or spends money.

## Install

There is nothing to build or install. Clone or download this repository; `collect.py` needs only Python 3.9 or newer. Install and sign in to the vendor CLIs for the sources you want (`gh`, `claude`, `codex`, `agy`, `muse`, `grok`, `colab`) the way you normally do.

```sh
cp fleet-status.example.json ~/.config/fleet-status.json   # then edit it: delete the sources you do not use
python3 -I collect.py --config ~/.config/fleet-status.json --pretty
```

`-I` runs Python isolated (no user site, no current directory on the import path), which is how the script is meant to be run. Without `--output` the feed goes to standard output.

To run it on a schedule, use cron, a systemd timer or launchd. Every 5 minutes is plenty; the cloud-credit and probe sources cache their answers (15 minutes and 24 hours). Schedulers start with a minimal `PATH`, so either set `PATH` in the job or give absolute paths in each source's `command`:

```cron
PATH=/usr/local/bin:/usr/bin:/bin
*/5 * * * * python3 -I /path/to/fleet-status/collect.py --config $HOME/.config/fleet-status.json --output $HOME/.local/state/fleet-status/feed.json
```

Exit codes: `0` success (sources that could not be read are reported as `unknown` in the feed, so a consumer should look at `sources[].ok` and each pool's `state`), `2` a config or usage error, `3` the snapshot was refused because it failed the schema or the secret check.

## Configuration

One JSON file, given with `--config` or the `FLEET_STATUS_CONFIG` environment variable. A source runs only if it is listed under `sources` and not set to `"enabled": false`. Unknown keys and unknown source names are errors (a typo should not silently turn a source off). Keys that start with `_` are comments. See `fleet-status.example.json`, which uses placeholder account labels.

```json
{
  "collector_id": "my-machine",
  "state_dir": "~/.local/state/fleet-status",
  "pool_notes": {"anthropic-api": "Monthly credit is a budget I chose"},
  "sources": {
    "claude_sub": {"enabled": true},
    "agy": {"accounts": [{"label": "account-a", "home": "~/agy-profiles/account-a"}, {"label": "account-b"}]},
    "github_billing": {"owner": "your-github-user", "owner_type": "user", "included_minutes": 2000}
  }
}
```

Top level:

| Key | Meaning |
|---|---|
| `collector_id` | Name of this feed, `a-z`, `0-9` and `-`, up to 40 characters. Default `fleet-status`. |
| `state_dir` | Where `usage-state.json` and `collector.log` live. Default `~/.local/state/fleet-status`; `--state-dir` overrides it. |
| `pool_notes` | `{pool id: text}`. The text is appended to that pool's advice. |
| `sources` | The sources to run, by id. |

Settings every source accepts: `enabled` (default `true`) and `timeout_seconds` (1 to 300; the built-in limits are 5 to 45 seconds, and agy and Colab get more for each extra account). A source that times out or reports `unavailable` is retried once.

Per-source settings:

| Source | Settings |
|---|---|
| `muse` | `command`, default `["muse", "usage", "--json"]` |
| `codex` | `command`, default `["codex", "app-server"]`; `sessions_dir`, default `$CODEX_HOME/sessions` or `~/.codex/sessions` |
| `agy` | `accounts`: a list of `{"label", "home", "command"}`. `label` is required (see below), `home` is optional (the account runs under that `HOME`), `command` defaults to `["agy", "-p", "/usage", "--output-format", "json"]`. With no `accounts` there is one account labelled `default` that uses the current login. |
| `colab` | `accounts`: a list of `{"label", "command", "expect_email"}`. `command` defaults to `["colab"]`; use a different wrapper per account if each has its own login. `expect_email` is optional. |
| `claude_sub` | `command`, default `["claude"]`; `fable`, default `true` (`false` leaves the Fable pool out) |
| `claude_cloud` | `credential`; `command`, default `["claude"]` (only used to refresh an expired token) |
| `grok` | `command`, default `["grok", "agent", "--no-leader", "stdio"]` |
| `github_billing` | `owner` and `included_minutes` (required), `owner_type` (`"org"` by default, or `"user"`), `command`, default `["gh"]` |
| `anthropic_costs` | `monthly_credit_usd` (required), `credential` |
| `openai_costs` | `grant_usd` and `grant_start` (an ISO date, required), `expires` (optional ISO date and time), `timezone` (default `UTC`), `expiring_days` (default 14) and `expiring_unused_pct` (default 50) for the "spend it before it expires" advice, `credential` |
| `anthropic_ledger` | `monthly_credit_usd` (required), `ledger_dir`, default the state directory |
| `api_probe` | `model`, default `claude-haiku-4-5`; `credential` |

An account `label` is 1 to 20 characters of `a-z`, `0-9`, `_` and `-`, starting with a letter or digit, and unique within its source. It becomes part of the pool id and label (`agy-account-a-gemini-models`, `colab-account-a-units`), so it should not be an address or a name you would not share.

`command` is always a JSON list, never a shell string, and nothing is expanded except a leading `~` in its first element and in the path settings. Treat the config file like a script: it can name any program to run, so only use one you wrote or reviewed.

A source whose settings are wrong (a bad account list, a missing `included_minutes`, a missing credit amount) reports `not_configured` or an `unknown` pool with the reason in its advice; the others keep running.

### Credentials

Only four sources need one. Each reads it, in memory, from the first of these that is set:

1. `credential.command`: a command you supply (a JSON list) whose standard output is the secret. This works with any password manager. A failing or empty command is `not_configured`.
2. `credential.env`: the name of an environment variable. If you set neither, the default variable below is used.

| Source | Default environment variable | What it holds |
|---|---|---|
| `anthropic_costs` | `FLEET_STATUS_ANTHROPIC_ADMIN_KEY` | Anthropic Admin key |
| `api_probe` | `FLEET_STATUS_ANTHROPIC_API_KEY` | Anthropic API key |
| `openai_costs` | `FLEET_STATUS_OPENAI_ADMIN_KEY` | OpenAI admin key |
| `claude_cloud` | `FLEET_STATUS_CLAUDE_OAUTH_TOKEN` | Claude OAuth access token, or the JSON the Claude CLI stores (`claudeAiOauth.accessToken`) |

These variables are used instead of the vendors' own (such as `ANTHROPIC_API_KEY`) on purpose, so a key you set for other work is never picked up by accident. Examples:

```json
"anthropic_costs": {"monthly_credit_usd": 200, "credential": {"command": ["pass", "show", "anthropic/admin-key"]}}
```

```json
"openai_costs": {"grant_usd": 100, "grant_start": "2026-11-01", "credential": {"env": "MY_OPENAI_KEY"}}
```

On macOS, to read an item from the login Keychain (use your own item name and login name; create the item with `security add-generic-password -s <name> -a <login> -w`):

```json
"credential": {"command": ["security", "find-generic-password", "-s", "my-keychain-item", "-a", "my-login-name", "-w"]}
```

The Claude CLI keeps its own login in the login Keychain on macOS. To use it for `claude_cloud`, point `credential.command` at `security find-generic-password` with that item's service name (shown in Keychain Access) and your login name. The collector accepts the stored JSON as it is.

A secret is sent only in a request header to the vendor's own API. It is never put in a command line, the feed, the state files or the log. Every `FLEET_STATUS_*` variable is removed from the environment of the commands the collector starts; a variable you name with `credential.env` is not, so give those commands nothing you would not want them to see, or use `credential.command` instead.

## Output

One document, `fleet-status/v1`:

| Field | Meaning |
|---|---|
| `generated_at`, `collector_id` | When it was built and by what name |
| `sources[]` | One entry per enabled source: `ok`, when it was `observed_at`, and an `error_code` (`timeout`, `unavailable`, `unauthorized`, `rate_limited`, `parse_error`, `not_configured`) |
| `pools[]` | The usage pools described above, at most 20 |
| `resets[]` | Upcoming window resets, soonest first, at most 20 |
| `asks[]` | Short notes for a person, at most 20. This collector raises two: `anthropic-credential-rejected` (the cost report refused your Admin key) and `shared-login-agy` (two agy accounts reported identical figures three runs in a row, so one login is probably shared). |
| `truncated`, `omitted` | Set when anything was cut to fit the caps, with the number dropped |
| `work`, `landed`, `landed_count_today`, `closed_wp_today` | Part of the schema for consumers that fill them. This collector always writes empty lists and zeros. |

A source can be `ok` and still leave an `unknown` pool, for example one agy account that could not be read. A source that failed leaves an `unknown` pool for each pool it stands for, with advice such as `Unavailable: the source timed out`, so a broken source is visible rather than absent.

The excerpt below is built from the test fixtures (`python3 tests/example.py --write` regenerates it).

<!-- example:start -->
Trimmed for the README: 4 of 19 pools and 3 of 18 resets are shown; `examples/sample-output.json` has the whole feed.

```json
{
  "asks": [],
  "closed_wp_today": 0,
  "collector_id": "my-machine",
  "generated_at": "2026-11-01T21:30:00Z",
  "landed": [],
  "landed_count_today": 0,
  "omitted": 0,
  "pools": [
    {
      "advice": null,
      "id": "muse",
      "kind": "subscription",
      "label": "Muse Code",
      "limit": 100,
      "observed_at": "2026-11-01T21:28:49Z",
      "pace_pct_per_hr": 5,
      "remaining": 56,
      "runs_out_at": null,
      "source_id": "muse",
      "state": "healthy",
      "unit": "percent",
      "used": 44,
      "windows": [
        {
          "label": "5h",
          "resets_at": "2026-11-02T01:17:12Z",
          "used_pct": 6
        },
        {
          "label": "weekly",
          "resets_at": "2026-11-02T00:00:00Z",
          "used_pct": 44
        }
      ]
    },
    {
      "advice": null,
      "id": "claude-subscription",
      "kind": "subscription",
      "label": "Claude subscription",
      "limit": 100,
      "observed_at": "2026-11-01T21:30:00Z",
      "pace_pct_per_hr": 1,
      "remaining": 83,
      "runs_out_at": "2026-11-04T10:31:45Z",
      "source_id": "claude_sub",
      "state": "healthy",
      "unit": "percent",
      "used": 17,
      "windows": [
        {
          "label": "weekly",
          "resets_at": "2026-11-08T09:00:00Z",
          "used_pct": 17
        }
      ]
    },
    {
      "advice": null,
      "id": "github-actions",
      "kind": "minutes",
      "label": "GitHub Actions minutes",
      "limit": 2000,
      "observed_at": "2026-11-01T21:30:00Z",
      "pace_pct_per_hr": null,
      "remaining": 1997,
      "runs_out_at": null,
      "source_id": "github_billing",
      "state": "healthy",
      "unit": "minutes",
      "used": 3,
      "windows": [
        {
          "label": "monthly",
          "resets_at": "2026-12-01T00:00:00Z",
          "used_pct": 0
        }
      ]
    },
    {
      "advice": "Monthly credit is a budget I chose, not a vendor limit",
      "id": "anthropic-api",
      "kind": "credit",
      "label": "Anthropic API spend",
      "limit": 20000,
      "observed_at": "2026-11-01T21:30:00Z",
      "pace_pct_per_hr": null,
      "remaining": 18800,
      "runs_out_at": null,
      "source_id": "anthropic_costs",
      "state": "healthy",
      "unit": "usd_cents",
      "used": 1200,
      "windows": [
        {
          "label": "monthly",
          "resets_at": "2026-12-01T00:00:00Z",
          "used_pct": 6
        }
      ]
    }
  ],
  "resets": [
    {
      "at": "2026-11-02T00:00:00Z",
      "effect": "reset",
      "kind": "reset",
      "label": "Muse Code weekly",
      "pool_id": "muse"
    },
    {
      "at": "2026-11-02T01:17:12Z",
      "effect": "reset",
      "kind": "reset",
      "label": "Muse Code 5h",
      "pool_id": "muse"
    },
    {
      "at": "2026-11-02T15:19:22Z",
      "effect": "reset",
      "kind": "reset",
      "label": "agy-account-a-gemini-models 5h",
      "pool_id": "agy-account-a-gemini-models"
    }
  ],
  "schema": "fleet-status/v1",
  "sources": [
    {
      "error_code": null,
      "id": "muse",
      "observed_at": "2026-11-01T21:28:49Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "codex",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "agy",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "colab",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "claude_sub",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "grok",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "claude_cloud",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "github_billing",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "anthropic_costs",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "openai_costs",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    },
    {
      "error_code": null,
      "id": "api_probe",
      "observed_at": "2026-11-01T21:30:00Z",
      "ok": true
    }
  ],
  "truncated": false,
  "work": []
}
```
<!-- example:end -->

## Privacy

- No prompts, responses, conversation text, tokens or keys are stored or published. The sources ask a CLI or an API for a usage number; none sends a prompt to a model. The Codex fallback parses only the lines of a session file that contain `rate_limits`.
- Files written, all in the state directory (created with mode `0700` if it does not exist yet): `usage-state.json` (mode `0600`), which holds derived readings only (the last Muse weekly figure, the cached Claude cloud credit in dollars and its expiry, the last probe result and its time, window high-water marks, and a counter for the shared-login check that is a hash of the readings); and `collector.log` (mode `0600`), which holds timestamps, source ids, error codes and counts. If you pass `--output`, the feed is written there atomically with mode `0600`.
- Before anything is printed or written, every string in the feed is scanned. A token, key, bearer string, credential-style `name=value`, long hex string, home or system path or email address makes the whole snapshot refused (exit 3). A note or label you add is scanned like everything else.
- The only network requests are the ones in the table above, to the vendor's own API, for sources you enabled and credentials you supplied. The CLIs and `gh` talk to their own services as they normally do. There is no telemetry and nothing is sent anywhere else.
- The feed does contain your usage figures and the account labels you chose. Check it before you share it.

## Limitations

- One login per source, except `agy` and `colab`, which take a list of accounts.
- Anything that parses a vendor tool's output can break when the tool changes; the tests pin the formats seen when this was written, against fixtures, not against the live services.
- GitHub billing endpoints depend on your plan and on the permissions of your `gh` login. The collector first reads the free `gh api rate_limit` and reports `rate_limited` without calling further when fewer than 25 REST calls are left.
- `agy` and `colab` read their accounts one after another, so many accounts mean a long run.

## Local API spend ledger

If you have an Anthropic API key but no Admin key, the cost report is not available to you. `api_ledger.py` keeps a ledger of your own calls instead, and the `anthropic_ledger` source turns it into the `anthropic-api-ledger` pool:

```sh
your-api-command | python3 -I api_ledger.py record --tee
```

It reads a Messages API response, or the JSON result of `claude -p --output-format json`, on standard input, writes it through unchanged with `--tee`, and records only the model name, token counts and the computed cost (cents, rounded up, from the price table at the top of the file; edit `PRICES` when prices change, and a model with no price is refused, not guessed). It never stores a key, a prompt or a response, and a repeated response id is recorded once. Use the same `--state-dir` as the collector's `state_dir`, or set `ledger_dir` on the source. It needs `fcntl`, so macOS or Linux.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use fixtures in `tests/fixtures` and fake every command and HTTP call. They need no account, no CLI and no network, and they check, among other things, that a missing source degrades to `unknown`, that sentinel credentials never reach the output, the state files or the log, that the stale-reading limit holds at 21600 and 21601 seconds, and that the example feed and the README excerpt are current. The GitHub workflow runs them on `ubuntu-latest` with Python 3.9 and 3.13, read-only permissions and no secrets.

## License

MIT, see `LICENSE`. Pace forecasting, window merging and the Claude auth environment list are ported from [clalt](https://github.com/dhitalkamal/clalt) (MIT, Copyright (c) 2026 Kamal Dhital); see `NOTICE`.
