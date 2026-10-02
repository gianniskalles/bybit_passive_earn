#!/usr/bin/env bash
# Install / update the systemd units (run as root). Idempotent. Never touches
# .env, the risk states, the carry book or the configs.
#
#   sudo /opt/hermes/yield_rotation/deploy/install.sh                       # yield rotation
#   sudo /opt/hermes/yield_rotation/deploy/install.sh --system carry        # funding carry
#   ... [--with-bot]                                                        # + Telegram commands
#
# One system at a time, never both on one account (CARRY_PLAN §13.2):
#   --system carry  disables the yield rotation's timers, then enables the carry's.
#   --system yield  REFUSES while a carry timer is enabled: switching back goes
#                   through deploy.sh, which first checks that the carry holds
#                   nothing (deploy/preflight.py carry-exposure) and disables the
#                   carry timers itself.
set -euo pipefail

REPO=/opt/hermes/yield_rotation
VENV_PY=/opt/hermes/venvs/yield_rotation/bin/python
UNIT_DIR=/etc/systemd/system
YIELD_TIMERS=(yield-cycle.timer yield-heartbeat.timer yield-summary.timer)
CARRY_TIMERS=(yield-carry-cycle.timer yield-carry-heartbeat.timer yield-carry-summary.timer
              yield-carry-calibrate.timer)

SYSTEM=yield
BOT=0
while (( $# )); do
    case $1 in
        --system) SYSTEM=${2:-}; shift 2 ;;
        --with-bot) BOT=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ $SYSTEM == yield || $SYSTEM == carry ]] || { echo "--system must be yield or carry" >&2; exit 2; }

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -x $VENV_PY ]] || { echo "missing $VENV_PY (the project venv; deploy.sh creates it)" >&2; exit 1; }
[[ -f $REPO/run_yield_cycle.py && -f $REPO/run_carry_cycle.py ]] || { echo "repo not at $REPO" >&2; exit 1; }
id hermes >/dev/null 2>&1 || { echo "user hermes does not exist" >&2; exit 1; }

if [[ $SYSTEM == yield ]]; then
    for t in "${CARRY_TIMERS[@]}"; do
        if [[ $(systemctl is-enabled "$t" 2>/dev/null || true) == enabled ]]; then
            echo "REFUSED: $t is enabled. Switching back to the yield rotation goes through" >&2
            echo "deploy.sh --system yield, which checks that the carry holds nothing first." >&2
            exit 1
        fi
    done
fi

install -d -o hermes -g hermes -m 0750 /opt/hermes/state /opt/hermes/logs/yield_rotation \
    /opt/hermes/logs/carry /opt/hermes/reports
for unit in "$REPO"/deploy/*.service "$REPO"/deploy/*.timer; do
    install -m 0644 "$unit" "$UNIT_DIR/$(basename "$unit")"
done
systemctl daemon-reload

if [[ $SYSTEM == carry ]]; then
    for t in "${YIELD_TIMERS[@]}"; do
        systemctl disable --now "$t" 2>/dev/null || true
        [[ $(systemctl is-active "$t" 2>/dev/null || true) != active ]] \
            || { echo "could not stop $t" >&2; exit 1; }
    done
    for t in "${CARRY_TIMERS[@]}"; do
        systemctl enable --now "$t"
    done
else
    for t in "${YIELD_TIMERS[@]}"; do
        systemctl enable --now "$t"
    done
fi
if (( BOT )); then
    systemctl enable yield-telegram-bot.service
    systemctl restart yield-telegram-bot.service   # load the new code (commands)
fi

systemctl list-timers 'yield-*' --no-pager
