# Bybit Earn yield rotation

[![tests](https://github.com/gianniskalles/bybit_passive_earn/actions/workflows/tests.yml/badge.svg)](https://github.com/gianniskalles/bybit_passive_earn/actions/workflows/tests.yml)

Keeps idle USDT in a Bybit FlexibleSaving product when it pays anything at
all, and gets it out when the product stops being usable. An LLM (via
`hermes chat`) decides *what* to do; deterministic code decides everything
that touches money: risk gates, amounts, product ids, order placement.

**Status:** `DRY_RUN: true`. Going live is FINISH_PLAN Phase 6 — see
[`DEPLOY.md`](DEPLOY.md). Architecture and rules for contributors:
[`HANDOFF.md`](HANDOFF.md) (Greek).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No keys, no network and no `/opt` needed: `tests/conftest.py` points every
path into a temp dir and refuses any socket connection. CI runs the same on
every push.

The LLM regression (`tests/run_regression.py`) runs the production cycle
against the real model and only works where the `hermes` CLI exists (the
VPS).

## One cycle

```
config (validated) → risk_state (verified) → Bybit data (each source, fail closed)
  → UNWIND?  wrapper redeems everything, no LLM
  → else     prompt_<PROMPT_VERSION>.md + inputs → hermes chat (no tools, stdin)
             → last JSON object carrying this cycle_id → validation
  → gates    STAKE only in NORMAL and with readable positions/balance/orders;
             REDEEM for products no longer Available; nothing in a coin with a pending order
  → amounts  computed by the wrapper, never by the LLM
  → executor POST /v5/earn/place-order (dry-run records the identical request)
  → record   LOG_DIR/YYYY-MM-DD.jsonl  (+ Telegram, deduplicated)
```

Run one cycle by hand (on the VPS):

```bash
sudo -u hermes /opt/hermes/venvs/yield_rotation/bin/python /opt/hermes/yield_rotation/run_yield_cycle.py
```

Exit codes: `0` ok · `3` invalid config or missing prompt · `4` crash. A
decision record is written in every case.

## Risk state

`risk_state.json` is HMAC-signed (`signing.py`) with fields `profile, state,
ts, reason, source, sig`.

| Verified record | Effective state |
|---|---|
| valid + fresh (≤ 30 min) | as signed |
| valid, stale | NORMAL→NO_NEW_POSITIONS, NO_NEW_POSITIONS→same, UNWIND→UNWIND |
| missing / unreadable / bad signature / malformed | NO_NEW_POSITIONS |

`heartbeat.py` (every 5 min) bootstraps `NO_NEW_POSITIONS`, promotes it to
`NORMAL` after a verified clean cycle, and renews a stale `NORMAL` while
cycles keep running. It never overwrites an operator state or `UNWIND`.

Operator override (`source: operator`):

```bash
sudo -u hermes /opt/hermes/venvs/yield_rotation/bin/python /opt/hermes/yield_rotation/risk_state.py write UNWIND "manual stop"
sudo -u hermes /opt/hermes/venvs/yield_rotation/bin/python /opt/hermes/yield_rotation/risk_state.py verify
```

or on Telegram: `/unwind` / `/resume`, then `/confirm <code>`.

## Configuration

`config/yield_rotation.yaml` — every field is required and type/range
checked at start-up. Strategy values are locked (HANDOFF §6). Secrets live
in `/opt/hermes/.env` (`HERMES_RISK_HMAC_KEY`, `BYBIT_API_KEY`,
`BYBIT_API_SECRET`, optional `BYBIT_TESTNET`, `YIELD_TELEGRAM_BOT_TOKEN`);
from the shared `/opt/data/.env` only `TELEGRAM_BOT_TOKEN` is read. Paths can
be overridden with `YIELD_*` env vars (`settings.py`).

## Decision record (abridged)

```json
{
  "ts": "ISO8601", "cycle_id": "20260925_184351_2e2396",
  "risk_state": "NORMAL",
  "risk_state_meta": {"code": "OK", "signature_valid": true, "fresh": true,
                      "source": "heartbeat_renew", "ts": 1790000000000},
  "agent_called": true, "prompt_file": "prompt_v6.md", "prompt_sha256": "…",
  "model_requested_on_cli": "google/gemini-2.5-flash",
  "decisions": [{"action": "STAKE", "coin": "USDT", "product_id": "1", "reason": "…"}],
  "plan": [{"action": "STAKE", "coin": "USDT", "product_id": "1", "amount": "5", "origin": "agent"}],
  "executions": [{
    "action": "STAKE", "amount": "5", "order_link_id": "20260925_184351_2e2396-S-1",
    "would_call": {"method": "POST", "path": "/v5/earn/place-order",
                   "body": {"category": "FlexibleSaving", "orderType": "Stake",
                            "accountType": "UNIFIED", "amount": "5", "coin": "USDT",
                            "productId": "1", "orderLinkId": "20260925_184351_2e2396-S-1"}},
    "executed": false, "reason": "DRY_RUN"}],
  "orders": [], "data_errors": {}, "filtered_by_wrapper": [],
  "alerts": [], "dry_run": true, "balance_source": "simulated"
}
```

## Files

| File | Role |
|---|---|
| `run_yield_cycle.py` | the cycle (wrapper) |
| `heartbeat.py` | risk_state liveness (systemd, every 5 min) |
| `risk_state.py`, `signing.py` | signed risk state |
| `executor.py`, `bybit_earn_tool.py` | order execution, Bybit client (CLI read-only) |
| `notify.py`, `summary.py`, `telegram_bot.py` | Telegram alerts, daily summary, operator commands |
| `settings.py` | paths and `.env` loading |
| `prompt_v6.md` | production prompt (`archive/` for old ones) |
| `deploy/` | systemd units, `install.sh`, `deploy.sh` (one-command deploy, see DEPLOY.md), `preflight.py` |
| `scripts/testnet.py` | testnet capture / stake+redeem round trip |
