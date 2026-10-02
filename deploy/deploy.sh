#!/usr/bin/env bash
# deploy.sh — DEPLOY.md steps 1-7 in one command (run as root):
#
#   cd /opt/hermes/yield_rotation && sudo -u hermes git pull --ff-only origin main
#   sudo deploy/deploy.sh                  # the system already active (yield by default)
#   sudo deploy/deploy.sh --system carry   # the funding carry (CARRY_PLAN)
#   sudo deploy/deploy.sh --system yield   # back to the yield rotation
#
# One system at a time, never both on one account (CARRY_PLAN §13.2):
# --system carry disables the yield rotation's timers; --system yield
# disables the carry's, and only after `preflight carry-exposure` shows the
# carry holds nothing. Without --system the active one is kept (the carry
# when yield-carry-cycle.timer is enabled, else the yield rotation).
#
# The carry book and the HMAC key are never reset or changed while the carry
# holds anything (exchange, paper account or book): the book is signed with
# that key, and a new key means BOOK_UNREADABLE and a hold.
#
# Prints "PASS step n/7" or "FAIL step n/7" after each step's raw output,
# stops at the first FAIL, and writes everything to
# /opt/hermes/logs/deploy/deploy_<utc>.log — paste that file.
#
# Idempotent: re-running keeps a valid HMAC key and risk_state, skips the
# LLM regression if it already passed for this prompt + model, and reinstalls
# the same units.
#
# Never: touches a systemd unit whose name does not start with "yield-",
# edits a crontab, runs anything against testnet, prints a secret (keys are
# shown as sha256 fingerprints), sets DRY_RUN, or installs anything into
# /opt/hermes/.venv (the Hermes CLI's venv).
#
# DRY_RUN is enforced: steps 5 and 7 start by checking that the config says
# DRY_RUN: true (else FAIL before anything runs), the manual cycle runs with
# --dry-run, and the final message is printed only after a last check.
set -uo pipefail

REPO=${YIELD_REPO:-/opt/hermes/yield_rotation}
HERMES_HOME=${YIELD_HERMES_HOME:-/opt/hermes}
# The project's OWN venv. /opt/hermes/.venv belongs to the Hermes CLI and is
# never modified; only its `hermes` binary is used.
VENV=${YIELD_VENV:-$HERMES_HOME/venvs/yield_rotation}
PY=$VENV/bin/python
BASE_PY=${YIELD_BASE_PYTHON:-python3}
HERMES_BIN=${YIELD_HERMES_BIN:-$HERMES_HOME/.venv/bin/hermes}
RUN_AS=${YIELD_RUN_AS:-hermes}
INSTALL=${YIELD_INSTALL:-$REPO/deploy/install.sh}
LOG_DIR=${YIELD_DEPLOY_LOG_DIR:-$HERMES_HOME/logs/deploy}
CRONTAB=${YIELD_CRONTAB:-crontab}
PAT='yield|heartbeat|run_yield_cycle|run_carry_cycle'
TOTAL=7
CARRY_TIMERS=(yield-carry-cycle.timer yield-carry-heartbeat.timer yield-carry-summary.timer
              yield-carry-calibrate.timer)
YIELD_TIMERS=(yield-cycle.timer yield-heartbeat.timer yield-summary.timer)

SYSTEM=""
while (( $# )); do
    case $1 in
        --system) SYSTEM=${2:-}; shift 2 || { echo "--system needs yield or carry" >&2; exit 2; } ;;
        *) echo "unknown argument: $1 (usage: deploy.sh [--system yield|carry])" >&2; exit 2 ;;
    esac
done
if [[ -n $SYSTEM && $SYSTEM != yield && $SYSTEM != carry ]]; then
    echo "--system must be yield or carry" >&2
    exit 2
fi

if [[ $EUID -ne 0 && ${YIELD_DEPLOY_ALLOW_NONROOT:-} != 1 ]]; then
    echo "deploy.sh must run as root: sudo $0" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/deploy_$(date -u +%Y%m%dT%H%M%SZ).log"
: > "$LOG"
chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
TEE_PID=$!
# Let tee flush everything into the log before the script exits.
trap 'exec 1>&- 2>&-; wait "$TEE_PID" 2>/dev/null' EXIT

# --------------------------------------------------------------------------- #

as_hermes() {  # run a command as the service user, HOME=/opt/hermes, from the repo
    if [[ $(id -un) == "$RUN_AS" ]]; then
        (cd "$REPO" && env HOME="$HERMES_HOME" "$@")
    else
        (cd "$REPO" && sudo -u "$RUN_AS" env HOME="$HERMES_HOME" "$@")
    fi
}

sysd() {  # systemctl, refusing any unit that is not ours
    local verb=$1 arg
    shift
    for arg in "$@"; do
        [[ $arg == -* ]] && continue
        if [[ $arg != yield-* ]]; then
            echo "REFUSED: systemctl $verb $arg — deploy.sh only manages yield-* units"
            return 1
        fi
    done
    systemctl "$verb" "$@"
}

preflight() { as_hermes "$PY" "$REPO/deploy/preflight.py" "$@"; }

carry_enabled() {  # is any carry timer enabled?
    local t
    for t in "${CARRY_TIMERS[@]}"; do
        [[ $(systemctl is-enabled "$t" 2>/dev/null || true) == enabled ]] && return 0
    done
    return 1
}

carry_must_be_flat() {  # before anything that stops the carry
    echo "the carry timers are enabled; switching back to the yield rotation stops them"
    if ! preflight carry-exposure; then
        echo "REFUSED: the carry holds positions or cannot be read. Close them first"
        echo "(Telegram /unwind carry, then wait for FLAT), then re-run with --system yield."
        return 1
    fi
}

run_step() {  # run_step N TITLE FUNCTION
    local n=$1 title=$2 fn=$3 rc
    echo
    echo "===== STEP $n/$TOTAL: $title ====="
    ( set -e; "$fn" )
    rc=$?
    if (( rc == 0 )); then
        echo "PASS step $n/$TOTAL: $title"
    else
        echo "FAIL step $n/$TOTAL: $title (exit $rc)"
        echo
        echo "DEPLOY STOPPED at step $n/$TOTAL. Paste this log: $LOG"
        exit 1
    fi
}

# --------------------------------------------------------------------------- #
# Steps                                                                        #
# --------------------------------------------------------------------------- #

step1_old_units() {
    local ours unit bad=0 cron
    echo "system: $SYSTEM"
    if [[ $SYSTEM == yield ]] && carry_enabled; then
        carry_must_be_flat
    fi
    ours=$(cd "$REPO/deploy" && ls -1 ./*.service ./*.timer | xargs -n1 basename)
    echo "never touched: hermes-gateway hermes-litellm hermes-george hermes-seo_agent (and every non-yield-* unit)"
    echo "units matching /$PAT/:"
    local files loaded units
    # A failed listing must FAIL the step, never read as "no units".
    files=$(systemctl list-unit-files --type=service,timer --no-legend --no-pager --plain 2>&1) \
        || { echo "$files"; echo "cannot list systemd unit files"; return 1; }
    loaded=$(systemctl list-units --all --type=service,timer --no-legend --no-pager --plain 2>&1) \
        || { echo "$loaded"; echo "cannot list systemd units"; return 1; }
    units=$(printf '%s\n%s\n' "$files" "$loaded" | awk '{print $1}' \
            | grep -E '\.(service|timer)$' | grep -iE "$PAT" | sort -u || true)
    [[ -z $units ]] && echo "  (none)"
    for unit in $units; do
        if grep -qxF "$unit" <<<"$ours"; then
            echo "  keep    $unit (installed by this repo)"
        elif [[ $unit == yield-* ]]; then
            echo "  disable $unit (old yield unit)"
            sysd disable --now "$unit"
        else
            echo "  FOREIGN $unit — matches the pattern but is not yield-*; not touched."
            echo "          If it is the old cycle/heartbeat, disable it by hand, then re-run."
            bad=1
        fi
    done
    echo "crontab entries matching /$PAT/:"
    local tab who all=""
    if ! command -v "$CRONTAB" >/dev/null 2>&1; then
        echo "  (cron is not installed — no crontabs)"
        return $bad
    fi
    for who in "$RUN_AS" root; do
        # "no crontab for X" is fine; any other failure to read one is not.
        if ! tab=$("$CRONTAB" -l -u "$who" 2>&1); then
            if grep -qi "no crontab" <<<"$tab"; then tab=""; else
                echo "$tab"; echo "cannot read the crontab of $who"; return 1
            fi
        fi
        all+="$tab"$'\n'
    done
    cron=$(grep -iE "$PAT" <<<"$all" || true)
    if [[ -n $cron ]]; then
        echo "$cron" | sed 's/^/  /'
        echo "  Remove these cron lines by hand (deploy.sh never edits crontabs), then re-run."
        bad=1
    else
        echo "  (none)"
    fi
    return $bad
}

step2_code() {
    local dirty flag help
    # git as the repo owner: as root it refuses a hermes-owned repo.
    echo "commit: $(as_hermes git -C "$REPO" rev-parse --short HEAD) ($(as_hermes git -C "$REPO" log -1 --format=%cd))"
    dirty=$(as_hermes git -C "$REPO" status --porcelain --untracked-files=no)
    if [[ -n $dirty ]]; then
        echo "local changes in $REPO:"; echo "$dirty"
        return 1
    fi
    if [[ ! -x $PY ]]; then
        echo "creating the project venv $VENV (with $BASE_PY)"
        as_hermes mkdir -p "$(dirname "$VENV")"
        as_hermes "$BASE_PY" -m venv "$VENV" || true
    fi
    [[ -x $PY ]] || { echo "no python at $PY — could not create the project venv"; return 1; }
    echo "project venv: $VENV ($(as_hermes "$PY" --version 2>&1)); Hermes CLI: $HERMES_BIN"
    as_hermes "$PY" -m pip install -q -r requirements.txt -r requirements-dev.txt
    as_hermes "$PY" -m pytest -q -p no:cacheprovider
    if [[ $SYSTEM == carry ]]; then
        echo "carry: no LLM anywhere — the agent CLI is not checked"
        return 0
    fi
    help=$(as_hermes "$HERMES_BIN" chat --help 2>&1) || { echo "$help"; return 1; }
    for flag in --query-file --toolsets -Q -m --reasoning; do
        if grep -qF -- "$flag" <<<"$help"; then
            echo "hermes chat supports $flag"
        else
            echo "hermes chat does NOT support $flag"; return 1
        fi
    done
}

step3_keys() {
    if [[ $SYSTEM == carry ]]; then preflight keys --system carry; else preflight keys; fi
}

step4_telegram() { preflight telegram-test; }

step5_state_and_cycle() {
    if [[ $SYSTEM == carry ]]; then step5_carry; return; fi
    preflight dry-run-on   # before anything else in this step
    preflight reset-state-if-invalid
    as_hermes "$PY" heartbeat.py
    echo "--- manual cycle (DRY_RUN) ---"
    as_hermes "$PY" run_yield_cycle.py --dry-run
    preflight check-cycle
    as_hermes "$PY" heartbeat.py
    preflight expect-normal
}

step5_carry() {
    preflight carry-dry-run-on   # before anything else in this step
    preflight carry-reset-state-if-invalid
    preflight carry-reset-book-if-invalid   # never while the carry holds anything
    as_hermes "$PY" heartbeat.py --system carry
    echo "--- manual carry cycle (DRY_RUN: paper account, public market data) ---"
    as_hermes "$PY" run_carry_cycle.py
    preflight carry-check-cycle
    as_hermes "$PY" heartbeat.py --system carry
    preflight carry-expect-normal
}

step6_regression() {
    local report
    if [[ $SYSTEM == carry ]]; then
        echo "carry: no LLM, no prompt — nothing to regress"
        return 0
    fi
    report=$(preflight regression-report)
    [[ -n $report ]] || { echo "no report path"; return 1; }
    if preflight regression-ok "$report"; then
        echo "regression already passed for this prompt + model — skipping"
        return 0
    fi
    as_hermes mkdir -p "$(dirname "$report")"
    as_hermes "$PY" tests/run_regression.py --runs 5 --out "$report"
    preflight regression-ok "$report"
}

step7_systemd() {
    local t on off bot=()
    if [[ $SYSTEM == carry ]]; then
        preflight carry-dry-run-on   # no timer is installed for a live config
        on=("${CARRY_TIMERS[@]}"); off=("${YIELD_TIMERS[@]}")
    else
        preflight dry-run-on
        on=("${YIELD_TIMERS[@]}"); off=("${CARRY_TIMERS[@]}")
        if carry_enabled; then
            carry_must_be_flat   # again, right before stopping it
            for t in "${CARRY_TIMERS[@]}"; do sysd disable --now "$t"; done
        fi
    fi
    if preflight has-bot-token; then
        bot=(--with-bot)
    else
        echo "YIELD_TELEGRAM_BOT_TOKEN not set — Telegram commands bot not installed"
    fi
    bash "$INSTALL" --system "$SYSTEM" "${bot[@]}"
    systemd-analyze verify /etc/systemd/system/yield-*.service /etc/systemd/system/yield-*.timer
    for t in "${on[@]}"; do
        echo "$t: $(systemctl is-enabled "$t") / $(systemctl is-active "$t")"
        [[ $(systemctl is-active "$t") == active ]] || return 1
    done
    for t in "${off[@]}"; do   # never both systems
        echo "$t: $(systemctl is-enabled "$t" 2>/dev/null || true) / $(systemctl is-active "$t" 2>/dev/null || true)"
        [[ $(systemctl is-active "$t" 2>/dev/null || true) != active ]] || return 1
    done
    systemctl list-timers 'yield-*' --no-pager
}

# --------------------------------------------------------------------------- #

if [[ -z $SYSTEM ]]; then
    if carry_enabled; then SYSTEM=carry; else SYSTEM=yield; fi
fi
DRY_CHECK=dry-run-on
[[ $SYSTEM == carry ]] && DRY_CHECK=carry-dry-run-on

echo "deploy.sh — $(date -u +%FT%TZ) — repo $REPO — system $SYSTEM — log $LOG"
run_step 1 "stop the old cycle/heartbeat (yield-* only)" step1_old_units
run_step 2 "code, dependencies, tests, agent CLI flags" step2_code
run_step 3 "keys in /opt/hermes/.env (fingerprints only)" step3_keys
run_step 4 "Telegram test message" step4_telegram
run_step 5 "risk_state + one manual DRY_RUN cycle + heartbeat" step5_state_and_cycle
run_step 6 "LLM regression of the production prompt (before any timer)" step6_regression
run_step 7 "systemd units and timers" step7_systemd
echo
echo "===== FINAL CHECK ====="
if ! preflight "$DRY_CHECK"; then
    echo "FAIL final check: the config no longer says DRY_RUN: true"
    echo "DEPLOY STOPPED at the final check. Paste this log: $LOG"
    exit 1
fi
echo "ALL $TOTAL STEPS PASSED — $SYSTEM timers running, DRY_RUN: true (verified by the final check above)."
if [[ $SYSTEM == carry ]]; then
    echo "Next: 14 days of paper trading (CARRY_PLAN §10). Testnet is NOT part of this script and waits for Giannis. Log: $LOG"
else
    echo "Next: the 7-day dry-run. Testnet is NOT part of this script and waits for Giannis. Log: $LOG"
fi
