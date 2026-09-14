# run_yield_cycle.py — yield rotation cycle runner

Run a single Bybit Earn yield-rotation cycle. **This is the dry-run
implementation; the live path is gated on user approval.**

## Quick start

```bash
# As hermes user (required — the tool loads /opt/hermes/.env)
sudo -u hermes env -i HOME=/opt/hermes \
  /opt/hermes/.venv/bin/python /opt/hermes/yield_rotation/run_yield_cycle.py \
  --dry-run
```

Exit code: `0` always. Read the JSON record (stdout) for outcome.
Log line: one JSON record per cycle in `/opt/hermes/logs/yield_rotation/YYYY-MM-DD.jsonl`.

## Architecture

The runner is the **verifier + filter** that wraps the LLM agent:

```
                ┌─────────────────────────────────────┐
                │       run_yield_cycle.py            │
                │  ┌──────────────────────────────┐   │
                │  │ 1. Load config, risk state    │   │
                │  │ 2. Pull snapshot from Bybit   │   │
                │  │ 3. Filter products (wrapper)  │   │
                │  │ 4. Compose prompt, call agent │   │
                │  │ 5. Validate agent JSON        │   │
                │  │ 6. Execute plan (executor.py) │   │
                │  └──────────────────────────────┘   │
                └─────────────────────────────────────┘
                              │
                              ▼
                ┌─────────────────────────────────────┐
                │   Decision record (stdout + log)   │
                │   - filtered_by_wrapper (audit)    │
                │   - prompt_version: v4             │
                │   - balance_source: real|simulated │
                │   - model_requested / actual       │
                └─────────────────────────────────────┘
```

## What "filtered_by_wrapper" is

One entry per product the wrapper **dropped** before asking the agent.
Reasons:
- `STALE_HISTORY`: APR history data is too old (default 4 h gap threshold)
- `STALE_SCAN`: the live /v5/earn/product scan is too old at decision time (default 900 s)
- `MIN_STAKE_TOO_HIGH`: product min > config max
- `TINY_POOL`: product remaining_quota < stake size
- `OUTSIDE_WHITELIST`: product's coin not in COIN_WHITELIST
- `TIERED_APR_UNCERTAIN`: hasTieredApr=true and tier table missing
- `NON_POSITIVE_RATE`: estimateApr <= 0

The agent never sees filtered products, so its decisions are over
a pre-vetted set.

## Configuration

See `config/yield_rotation.yaml`. Every parameter that affects a
decision is in the YAML, **not** in env vars. The runner reads
the YAML exactly once per cycle.

Key fields:
- `DRY_RUN`: `true` = log only, `false` = execute via Bybit API
- `SIMULATED_IDLE_BALANCE`: only honored when `DRY_RUN: true`; if
  set in `DRY_RUN: false`, the cycle **refuses to start** with
  CRITICAL.
- `ACCOUNT_TYPE`: `UNIFIED` (Bybit only supports UNIFIED for Earn)
- `MAX_SCAN_AGE_SECONDS`: 900 (live product scan freshness, seconds)
- `MAX_APR_HISTORY_GAP_HOURS`: 4 (APR-history data freshness, hours)
- `COIN_WHITELIST`, `ENTRY_APR`, `EXIT_APR`, `MIN_APR_EDGE`,
  `MAX_PER_PRODUCT_USD`, `MIN_MOVE_USD`, `RESERVE_USD`

## Risk state

The wrapper verifies the HMAC-signed risk state file at
`/opt/hermes/state/risk_state.json` on every cycle. If missing,
unreadable, or HMAC fails, risk_state is forced to
`NO_NEW_POSITIONS` and the agent is told no new positions are
allowed.

To set a manual risk state for testing:

```bash
HMAC=$(sudo -u hermes grep HERMES_RISK_HMAC_KEY /opt/hermes/.env | cut -d= -f2)
sudo -u hermes env -i HERMES_RISK_HMAC_KEY="$HMAC" \
  /opt/hermes/.venv/bin/python /opt/hermes/tools/risk_state.py \
  write /opt/hermes/state/risk_state.json NORMAL "manual override"
```

## CLI flags

All flags are explicit (no env vars, no global config):

```
--dry-run          Log only, do not execute trades. (default if
                   config says DRY_RUN: true)
--live             Execute trades. Refuses to start if config
                   has SIMULATED_IDLE_BALANCE set.
--config PATH      Path to YAML config (default:
                   /opt/hermes/yield_rotation/config/yield_rotation.yaml)
--model NAME       Hermes model (default: hermes-cheap)
--reasoning LEVEL  Reasoning level (default: medium)
--prompt-version V Prompt version (default: v4)
```

## Decision record schema

```json
{
  "ts": "ISO8601",
  "cycle_id": "YYYYMMDD_HHMMSS_xxxxxx",
  "session_id": "...",
  "risk_state": "NORMAL|UNWIND|NO_NEW_POSITIONS",
  "risk_state_meta": { "ts": ..., "reason": ..., "verifier": ... },
  "decisions": [
    {
      "action": "STAKE|REDEEM|HOLD|ALERT_ONLY|REDEEM_ALL",
      "coin": "USDT",
      "product_id": "...",
      "amount_usd": 5.0,
      "reason": "APR edge 0.012 > MIN_APR_EDGE 0.009; pool 10000 USDT > 1.5"
    }
  ],
  "holds": [
    { "coin": "USDT", "product_id": "...", "amount_usd": 5.0,
      "current_apr": 0.018, "reason": "edge insufficient" }
  ],
  "alerts": ["..."],
  "executions": [
    {
      "ts": "ISO8601",
      "action": "STAKE",
      "coin": "USDT",
      "would_call": "bybit_earn_tool.place_order(productId=1, amount=5, accountType=UNIFIED)",
      "executed": false,
      "reason": "dry-run"
    }
  ],
  "dry_run": true,
  "balance_source": "simulated|real",
  "filtered_by_wrapper": [
    { "product_id": "2", "reason": "OUTSIDE_WHITELIST: coin=BTC" }
  ],
  "prompt_version": "v4",
  "model_requested": "hermes-cheap",
  "snapshot_meta": { "ts": ..., "account_type": "UNIFIED", ... }
}
```

## Output destinations

- `stdout`: full JSON record (for cron / monitoring ingestion)
- `/opt/hermes/logs/yield_rotation/YYYY-MM-DD.jsonl`: append-only
  audit log (one record per cycle, JSON-per-line)
- `/opt/hermes/state/yield_rotation_sessions/<session_id>.raw`:
  raw agent stdout/stderr (for debugging)
- Telegram Home (5860907422): only on CRITICAL alerts, risk state
  transitions, or executed moves (live mode only)

## Files

- `run_yield_cycle.py` — this file
- `executor.py` — dry-run vs live execution wrapper
- `bybit_earn_tool.py` — Bybit Earn API client (CLI)
- `prompt_v4.md` — locked agent prompt (v4)
- `config/yield_rotation.yaml` — strategy config
- `tests/fixtures.py` + `tests/run_regression.py` — 40-case regression
