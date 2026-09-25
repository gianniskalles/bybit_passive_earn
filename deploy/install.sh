#!/usr/bin/env bash
# Install / update the systemd units for the yield rotation (run as root).
# Idempotent. Never touches .env, risk_state.json or the config.
#
#   sudo /opt/hermes/yield_rotation/deploy/install.sh            # timers only
#   sudo /opt/hermes/yield_rotation/deploy/install.sh --with-bot # + Telegram commands
set -euo pipefail

REPO=/opt/hermes/yield_rotation
VENV_PY=/opt/hermes/.venv/bin/python
UNIT_DIR=/etc/systemd/system
TIMERS=(yield-cycle.timer yield-heartbeat.timer yield-summary.timer)

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -x $VENV_PY ]] || { echo "missing $VENV_PY" >&2; exit 1; }
[[ -f $REPO/run_yield_cycle.py ]] || { echo "repo not at $REPO" >&2; exit 1; }
id hermes >/dev/null 2>&1 || { echo "user hermes does not exist" >&2; exit 1; }

install -d -o hermes -g hermes -m 0750 /opt/hermes/state /opt/hermes/logs/yield_rotation
for unit in "$REPO"/deploy/*.service "$REPO"/deploy/*.timer; do
    install -m 0644 "$unit" "$UNIT_DIR/$(basename "$unit")"
done
systemctl daemon-reload

for t in "${TIMERS[@]}"; do
    systemctl enable --now "$t"
done
if [[ ${1:-} == --with-bot ]]; then
    systemctl enable --now yield-telegram-bot.service
fi

systemctl list-timers 'yield-*' --no-pager
