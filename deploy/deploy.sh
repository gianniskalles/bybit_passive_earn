#!/usr/bin/env bash
# deploy.sh — DEPLOY.md steps 1-7 in one command (run as root):
#
#   cd /opt/hermes/yield_rotation && sudo -u hermes git pull --ff-only origin main
#   sudo deploy/deploy.sh
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
# shown as sha256 fingerprints), or sets DRY_RUN.
set -uo pipefail

REPO=${YIELD_REPO:-/opt/hermes/yield_rotation}
HERMES_HOME=${YIELD_HERMES_HOME:-/opt/hermes}
PY=${YIELD_PY:-$HERMES_HOME/.venv/bin/python}
HERMES_BIN=${YIELD_HERMES_BIN:-$HERMES_HOME/.venv/bin/hermes}
RUN_AS=${YIELD_RUN_AS:-hermes}
INSTALL=${YIELD_INSTALL:-$REPO/deploy/install.sh}
LOG_DIR=${YIELD_DEPLOY_LOG_DIR:-$HERMES_HOME/logs/deploy}
CRONTAB=${YIELD_CRONTAB:-crontab}
PAT='yield|heartbeat|run_yield_cycle'
TOTAL=7

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
    as_hermes "$PY" -m pip install -q -r requirements.txt -r requirements-dev.txt
    as_hermes "$PY" -m pytest -q -p no:cacheprovider
    help=$(as_hermes "$HERMES_BIN" chat --help 2>&1) || { echo "$help"; return 1; }
    for flag in --query-file --toolsets -Q -m --reasoning; do
        if grep -qF -- "$flag" <<<"$help"; then
            echo "hermes chat supports $flag"
        else
            echo "hermes chat does NOT support $flag"; return 1
        fi
    done
}

step3_keys() { preflight keys; }

step4_telegram() { preflight telegram-test; }

step5_state_and_cycle() {
    preflight reset-state-if-invalid
    as_hermes "$PY" heartbeat.py
    echo "--- manual cycle (DRY_RUN) ---"
    as_hermes "$PY" run_yield_cycle.py
    preflight check-cycle
    as_hermes "$PY" heartbeat.py
    preflight expect-normal
}

step6_regression() {
    local report
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
    local t
    if preflight has-bot-token; then
        bash "$INSTALL" --with-bot
    else
        echo "YIELD_TELEGRAM_BOT_TOKEN not set — Telegram commands bot not installed"
        bash "$INSTALL"
    fi
    systemd-analyze verify /etc/systemd/system/yield-*.service /etc/systemd/system/yield-*.timer
    for t in yield-cycle.timer yield-heartbeat.timer yield-summary.timer; do
        echo "$t: $(systemctl is-enabled "$t") / $(systemctl is-active "$t")"
        [[ $(systemctl is-active "$t") == active ]] || return 1
    done
    systemctl list-timers 'yield-*' --no-pager
}

# --------------------------------------------------------------------------- #

echo "deploy.sh — $(date -u +%FT%TZ) — repo $REPO — log $LOG"
run_step 1 "stop the old cycle/heartbeat (yield-* only)" step1_old_units
run_step 2 "code, dependencies, tests, agent CLI flags" step2_code
run_step 3 "keys in /opt/hermes/.env (fingerprints only)" step3_keys
run_step 4 "Telegram test message" step4_telegram
run_step 5 "risk_state + one manual DRY_RUN cycle + heartbeat" step5_state_and_cycle
run_step 6 "LLM regression of the production prompt (before any timer)" step6_regression
run_step 7 "systemd units and timers" step7_systemd
echo
echo "ALL $TOTAL STEPS PASSED — timers running, DRY_RUN. Next: the 7-day dry-run."
echo "Testnet is NOT part of this script and waits for Giannis. Log: $LOG"
