#!/usr/bin/env bash
# carry_testnet_loop.sh — the forced testnet cycle (CARRY_PLAN 13.6), as a
# transient systemd unit, not a background shell job:
#
#   sudo systemd-run --uid=hermes --gid=hermes --unit=yield-carry-testnet \
#       --setenv=HOME=/opt/hermes --setenv=BYBIT_TESTNET=1 \
#       --property=WorkingDirectory=/opt/hermes/yield_rotation \
#       /opt/hermes/yield_rotation/scripts/carry_testnet_loop.sh
#
# Every LOOP_SLEEP_S (300): one carry cycle, then the carry heartbeat — both
# on testnet only (BYBIT_TESTNET=1: config/carry.testnet.yaml, state in
# /opt/hermes/state/carry-testnet, logs and raw responses in
# /opt/hermes/logs/carry-testnet). Stops with
#   sudo systemctl stop yield-carry-testnet
# or the file <state>/carry-testnet/STOP. Refuses to start without
# BYBIT_TESTNET=1 or while a STOP file from an earlier run is present.
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
PY=${YIELD_PY:-/opt/hermes/venvs/yield_rotation/bin/python}
HOME_DIR=${YIELD_HERMES_HOME:-/opt/hermes}
STOP="$HOME_DIR/state/carry-testnet/STOP"
SLEEP_S=${LOOP_SLEEP_S:-300}

case "${BYBIT_TESTNET:-}" in
    1|true|yes) ;;
    *) echo "refusing: BYBIT_TESTNET=1 is required (this loop is testnet-only)" >&2; exit 2 ;;
esac
if [[ -e $STOP ]]; then
    echo "refusing: $STOP exists from an earlier run; remove it to start" >&2
    exit 2
fi
mkdir -p "$HOME_DIR/state/carry-testnet" "$HOME_DIR/logs/carry-testnet"

echo "carry testnet loop: every ${SLEEP_S}s; stop with systemctl stop yield-carry-testnet or $STOP"
while [[ ! -e $STOP ]]; do
    "$PY" "$REPO/run_carry_cycle.py"
    echo "cycle rc=$?"
    "$PY" "$REPO/heartbeat.py" --system carry
    echo "heartbeat rc=$?"
    for ((i = 0; i < SLEEP_S; i++)); do
        [[ -e $STOP ]] && break
        sleep 1
    done
done
echo "carry testnet loop: STOP file found, exiting"
