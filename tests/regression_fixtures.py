"""LLM regression scenarios — decision QUALITY only (T4.4).

Everything deterministic (risk-state gates, staleness, amounts, REDEEM
mechanics, product ids, config) is covered by the wrapper unit tests and
never depends on the model.  These scenarios ask: given real-shaped
Bybit data and the production config, does the model STAKE when it should
and HOLD when it should?

Each scenario: payload (built from a recorded Bybit capture), risk state,
and `expect`:
  {"stake": ["<product_id>"]}  the agent's decisions contain exactly these STAKEs
  {"stake": []}                the agent emits no STAKE
"""

from replay import load_payload, mutate

BASE = "SYNTHETIC_usdt_flexible.json"


def _base():
    return load_payload(BASE)


def _set_usdt_apr(apr):
    def fn(result):
        for p in result["list"]:
            if p["coin"] == "USDT":
                p["estimateApr"] = apr
    return fn


def _set_history_apr(apr):
    def fn(result):
        for point in result["list"]:
            point["apr"] = apr
    return fn


def _set_wallet(amount):
    def fn(result):
        result["list"][0]["coin"][0]["walletBalance"] = amount
        result["list"][0]["coin"][0]["equity"] = amount
    return fn


def scenarios():
    base = _base()
    below = mutate(mutate(base, "/v5/earn/product", _set_usdt_apr("0.05%")),
                   "/v5/earn/apr-history", _set_history_apr("0.05%"))
    spike = mutate(base, "/v5/earn/apr-history", _set_history_apr("0.05%"))
    broke = mutate(base, "/v5/account/wallet-balance", _set_wallet("0"))
    return [
        {"name": "01_stake_when_rate_qualifies", "payload": base, "risk_state": "NORMAL",
         "description": "USDT 1.2% now and over 24 h, 100 idle -> STAKE product 1",
         "expect": {"stake": ["1"]}},
        {"name": "02_hold_when_rate_below_entry", "payload": below, "risk_state": "NORMAL",
         "description": "USDT 0.05% (< ENTRY_APR 0.1%) -> no STAKE",
         "expect": {"stake": []}},
        {"name": "03_hold_on_spike", "payload": spike, "risk_state": "NORMAL",
         "description": "estimate 1.2% but 24 h mean 0.05% -> no STAKE (trend, not spike)",
         "expect": {"stake": []}},
        {"name": "04_hold_without_idle_balance", "payload": broke, "risk_state": "NORMAL",
         "description": "wallet 0 -> no STAKE",
         "expect": {"stake": []}},
        {"name": "05_no_stake_under_no_new_positions", "payload": base,
         "risk_state": "NO_NEW_POSITIONS",
         "description": "qualifying rate but NO_NEW_POSITIONS -> no STAKE (the wrapper "
                        "would drop it anyway; this measures prompt adherence)",
         "expect": {"stake": []}},
    ]


def get(name):
    for s in scenarios():
        if s["name"] == name:
            return s
    raise KeyError(name)
