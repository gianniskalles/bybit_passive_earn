# DEPLOY.md — εγκατάσταση στο VPS (για τον Hermes)

Φάση 6 του `FINISH_PLAN.md`. Εκτέλεσε τα βήματα **με τη σειρά**, χωρίς να
παραλείψεις κανένα. Αν ένας έλεγχος δεν βγάλει το αναμενόμενο, **σταμάτα** και
στείλε την έξοδο στον Giannis — μην «διορθώσεις» τίποτα στον κώδικα στο VPS.

Κανόνες:
- **Ποτέ `DRY_RUN: false`.** Το αλλάζει μόνο ο Giannis, στο τελευταίο βήμα.
- **Ποτέ μην τυπώνεις κλειδιά** σε τερματικό, log ή μήνυμα.
- Όλες οι εντολές τρέχουν ως root εκτός αν λένε `sudo -u hermes`.

Συντομεύσεις που χρησιμοποιούνται παρακάτω:

```bash
REPO=/opt/hermes/yield_rotation
PY=/opt/hermes/.venv/bin/python
H="sudo -u hermes"
```

---

## Βήμα 0 — Προϋποθέσεις

- [ ] Το PR των Φάσεων 0–5 είναι merged στο `main` και το CI είναι πράσινο.
- [ ] Ο Giannis έχει κάνει το repo **private** (FINISH_PLAN Α4).

## Βήμα 1 — Σταμάτημα του παλιού κύκλου και heartbeat

> **⛔ ΜΗΝ αγγίξεις** — ανήκουν σε άλλες υπηρεσίες του VPS, ακόμα κι αν
> εμφανιστούν σε κάποια λίστα:
> - `hermes-gateway`
> - `hermes-litellm`
> - `hermes-george`
> - `hermes-seo_agent`
>
> Αν ένα από αυτά (ή οποιοδήποτε άλλο `hermes-*`) βγει στα αποτελέσματα
> παρακάτω, **άφησέ το ως έχει**.

```bash
PAT='yield|heartbeat|run_yield_cycle'
systemctl list-timers --all --no-pager | grep -iE "$PAT" || true
systemctl list-units --all --no-pager  | grep -iE "$PAT" || true
crontab -l -u hermes 2>/dev/null        | grep -iE "$PAT" || true
crontab -l 2>/dev/null                  | grep -iE "$PAT" || true
```

Απενεργοποίησε **μόνο** τα timers/services/cron του παλιού κύκλου ή heartbeat
που βρήκες με αυτό το φίλτρο (`systemctl disable --now <unit>` / σβήσε τη
γραμμή του cron). Πριν απενεργοποιήσεις ένα unit, έλεγξε ότι το `ExecStart`
του (`systemctl cat <unit>`) δείχνει σε `run_yield_cycle.py` ή `heartbeat.py`
του `/opt/hermes/yield_rotation`· αν όχι, **σταμάτα και ρώτα**.
Επανάλαβε τις εντολές: δεν πρέπει να τυπώνουν τίποτα που τρέχει.

## Βήμα 2 — Κώδικας και εξαρτήσεις

```bash
cd $REPO
$H git status --short          # πρέπει να είναι κενό· αν όχι, σταμάτα
$H git fetch origin && $H git checkout main && $H git pull --ff-only origin main
$H git log --oneline -1
$H $PY -m pip install -r requirements.txt
$H $PY -m pip install -r requirements-dev.txt
cd $REPO && $H $PY -m pytest -q          # αναμενόμενο: όλα passed (1 skipped)
```

Έλεγξε ότι το CLI του agent υποστηρίζει τα flags της παραγωγής:

```bash
$H /opt/hermes/.venv/bin/hermes chat --help | grep -E -- '--query-file|--toolsets|-Q|-m|--reasoning'
```

Πρέπει να εμφανίζονται και τα πέντε. Αν λείπει κάποιο, **σταμάτα**.

## Βήμα 3 — Κλειδιά στο `/opt/hermes/.env`

Ο κώδικας διαβάζει πλέον από το κοινόχρηστο `/opt/data/.env` **μόνο** το
`TELEGRAM_BOT_TOKEN`. Τα `HERMES_RISK_HMAC_KEY` και `BYBIT_*` πρέπει να
βρίσκονται στο `/opt/hermes/.env`.

```bash
ls -l /opt/hermes/.env         # owner hermes, mode 600
$H cp /opt/hermes/.env /opt/hermes/.env.bak.$(date +%Y%m%d%H%M%S)
```

**3α. Νέο κλειδί HMAC** (αντικαθιστά το `smoke-test-only-do-not-use-in-prod`):

```bash
NEW=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
$H sed -i '/^HERMES_RISK_HMAC_KEY=/d' /opt/hermes/.env
printf 'HERMES_RISK_HMAC_KEY=%s\n' "$NEW" | $H tee -a /opt/hermes/.env >/dev/null
unset NEW
chmod 600 /opt/hermes/.env && chown hermes:hermes /opt/hermes/.env
```

**3β. Έλεγχος** (δεν τυπώνει τιμές):

```bash
cd $REPO && $H $PY - <<'EOF'
import settings
prof = settings.load_env_file(settings.env_file())
shared = settings.load_env_file(settings.shared_env_file())
for k in ("HERMES_RISK_HMAC_KEY", "BYBIT_API_KEY", "BYBIT_API_SECRET"):
    v = prof.get(k, "")
    bad = "smoke-test" in v
    print(f"{k:22} {'OK' if v and not bad else 'ΛΑΘΟΣ'}"
          f"{' (smoke-test key!)' if bad else ''}{'' if v else ' (λείπει από /opt/hermes/.env)'}")
print(f"{'TELEGRAM_BOT_TOKEN':22} {'OK' if prof.get('TELEGRAM_BOT_TOKEN') or shared.get('TELEGRAM_BOT_TOKEN') else 'λείπει'}")
print(f"{'BYBIT_TESTNET':22} {prof.get('BYBIT_TESTNET', '(κενό = mainnet)')}")
for k in ("HERMES_RISK_HMAC_KEY", "BYBIT_API_KEY", "BYBIT_API_SECRET"):
    if k in shared and k not in prof:
        print(f"ΠΡΟΣΟΧΗ: {k} υπάρχει ΜΟΝΟ στο /opt/data/.env — δεν θα διαβαστεί")
EOF
```

Όλα πρέπει να είναι `OK` και το `BYBIT_TESTNET` κενό. Αν ένα `BYBIT_*` λείπει
από το `/opt/hermes/.env` αλλά υπάρχει στο `/opt/data/.env`, **αντέγραψέ το**
με τον ίδιο τρόπο όπως στο 3α (χωρίς να το τυπώσεις).

Το σημερινό κλειδί Bybit μένει για τις αναγνώσεις του dry-run. Το νέο κλειδί
για χρήματα το φτιάχνει ο Giannis στο βήμα 10.

## Βήμα 4 — Δοκιμαστικό μήνυμα Telegram

```bash
cd $REPO && $H $PY - <<'EOF'
import yaml, settings
from notify import Notifier
cfg = yaml.safe_load(settings.config_file().read_text())
n = Notifier(settings.load_env(), cfg)
print("enabled:", n.enabled, "chat:", n.chat)
print("sent:", n.event("🧪 yield rotation: δοκιμαστικό μήνυμα από το deploy"))
EOF
```

Αναμενόμενο: `enabled: True`, `sent: True`, και ο Giannis επιβεβαιώνει ότι το
μήνυμα έφτασε.

**Προαιρετικό — εντολές `/unwind`, `/resume`:** χρειάζονται **δικό τους** bot
(ο Giannis το φτιάχνει στο @BotFather), γιατί το κοινόχρηστο token το
χρησιμοποιεί ήδη άλλη υπηρεσία. Αν ο Giannis δώσει token, βάλ' το ως
`YIELD_TELEGRAM_BOT_TOKEN` στο `/opt/hermes/.env` με τον τρόπο του 3α. Χωρίς
αυτό, παράλειψε το `--with-bot` στο βήμα 7.

## Βήμα 5 — Καινούργιο risk state (όχι μεταφορά του παλιού)

Το παλιό αρχείο δεν έχει το πεδίο `source` και είναι υπογεγραμμένο με το
παλιό κλειδί· ο νέος κώδικας το απορρίπτει. Δεν μεταφέρεται — μετακινείται
στην άκρη και το heartbeat ξεκινά από bootstrap.

```bash
S=/opt/hermes/state/risk_state.json
[ -f $S ] && $H mv $S $S.pre-v6.$(date +%Y%m%d%H%M%S)
cd $REPO && $H $PY heartbeat.py
$H $PY risk_state.py verify
```

Αναμενόμενο: `WROTE: NO_NEW_POSITIONS (bootstrap)` και στο `verify`:
`"code": "OK"`, `"state": "NO_NEW_POSITIONS"`, `"source": "heartbeat_bootstrap"`.

Ένας κύκλος με το χέρι, μετά ξανά heartbeat:

```bash
$H $PY run_yield_cycle.py > /tmp/cycle.json; echo "exit=$?"
tail -n 1 /opt/hermes/logs/yield_rotation/$(date -u +%F).jsonl | \
  $PY -c 'import json,sys; r=json.loads(sys.stdin.read()); print({k: r[k] for k in ("risk_state","agent_called","prompt_file","alerts","dry_run")}); print(r["risk_state_meta"]["code"], r["data_errors"])'
$H $PY heartbeat.py
$H $PY risk_state.py verify
```

Αναμενόμενο:
- `exit=0`, `risk_state: NO_NEW_POSITIONS`, `agent_called: True`,
  `prompt_file: prompt_v6.md`, `dry_run: True`, `data_errors: {}`,
  `risk_state_meta.code: OK`, κανένας κωδικός `CONFIG_INCOMPLETE` /
  `CRITICAL` / `AGENT_PARSE_ERROR` / `DECISION_VALIDATION_FAILED` /
  `CYCLE_CRASH` στα `alerts`.
- Μετά το heartbeat: `WROTE: NORMAL (bootstrap promoted after verified clean
  cycle)`, και το `verify` δείχνει `NORMAL`, `source: heartbeat_renew`.

Αν ο κύκλος βγάλει εμποδιστικό κωδικό, **σταμάτα** και στείλε το
`/tmp/cycle.json`.

Αν αντί για `agent_called: True` δεις `NO_ELIGIBLE_PRODUCTS` στα `alerts`,
κανένα προϊόν δεν πέρασε τα φίλτρα — πιθανότατα πεδίο της Bybit με άλλο όνομα
από το αναμενόμενο (HANDOFF §9). Το heartbeat θα προαγάγει κανονικά (ο κωδικός
δεν είναι εμποδιστικός), αλλά **στείλε** το `filtered_by_wrapper` του record
στον Giannis πριν προχωρήσεις:

```bash
$PY -c 'import json; r=json.load(open("/tmp/cycle.json")); print(json.dumps(r["filtered_by_wrapper"], indent=1))' 2>/dev/null \
  || tail -n 1 /opt/hermes/logs/yield_rotation/$(date -u +%F).jsonl | $PY -c 'import json,sys; print(json.loads(sys.stdin.read())["filtered_by_wrapper"])'
```

## Βήμα 6 — LLM regression του prompt v6 (πριν από οποιονδήποτε timer)

Μόνο εδώ υπάρχει το CLI του agent. Τρέχει τον κύκλο της παραγωγής απομονωμένα
(δικό του risk state, κλειδί και logs· `DRY_RUN` πάντα) με το πραγματικό
μοντέλο:

```bash
cd $REPO && $H $PY tests/run_regression.py --runs 5 --out /opt/hermes/logs/regression_v6_$(date -u +%F).json
```

Αναμενόμενο: `=== 25/25 passed ===` (5 σενάρια × 5). **Οποιοδήποτε FAIL →
σταμάτα** και στείλε το αρχείο `--out` στον Giannis. Το επταήμερο δεν ξεκινά
με αποτυχημένο regression — γι' αυτό τρέχει **πριν** εγκατασταθούν τα
timers: μέχρι να περάσει, τίποτα δεν τρέχει προγραμματισμένα.

## Βήμα 7 — systemd (μόνο αφού το βήμα 6 έβγαλε 25/25)

```bash
$REPO/deploy/install.sh              # ή: install.sh --with-bot  (αν υπάρχει YIELD_TELEGRAM_BOT_TOKEN)
systemctl list-timers 'yield-*' --no-pager
```

Αναμενόμενο: τρία timers — `yield-cycle` (κάθε 10'), `yield-heartbeat` (κάθε
5', στο :02/:07/…), `yield-summary` (06:55 UTC).

Το `NORMAL` του βήματος 5 έχει πιθανώς παλιώσει όσο έτρεχε το regression· ο
πρώτος προγραμματισμένος κύκλος θα τρέξει ως `NO_NEW_POSITIONS` με
`RISK_STATE_STALE` (αναμενόμενο, μη-εμποδιστικό) και το επόμενο heartbeat
το ανανεώνει. Μέσα σε ~15' το `risk_state.py verify` πρέπει να δείχνει
`NORMAL`, `code: OK`.

Επιβεβαίωσε ότι ο agent τρέχει χωρίς tools και με το prompt από stdin (μετά
τον επόμενο κύκλο):

```bash
journalctl -u yield-cycle -n 30 --no-pager
tail -n 1 /opt/hermes/logs/yield_rotation/$(date -u +%F).jsonl | $PY -c 'import json,sys; r=json.loads(sys.stdin.read()); print(r["prompt_file"], r["prompt_sha256"][:12], r["model_requested_on_cli"], r["alerts"])'
grep -n "toolsets=" $REPO/run_yield_cycle.py
```

## Βήμα 8 — Testnet (Φάση 6, βήμα 4) — ⛔ ΣΤΑΜΑΤΑ ΕΔΩ

**Μην προχωρήσεις χωρίς ρητή έγκριση του Giannis.** Ο Giannis θα δώσει
κλειδιά testnet και οδηγίες. Για αναφορά, το βήμα είναι:

1. Κλειδί testnet (μόνο Earn) σε **ξεχωριστό** αρχείο ή περιβάλλον — όχι στη
   θέση των κλειδιών του dry-run.
2. `BYBIT_TESTNET=1 ... scripts/testnet.py capture` → πραγματικές απαντήσεις
   στο `tests/data/testnet_<utc>.json`.
3. `BYBIT_TESTNET=1 ... scripts/testnet.py roundtrip` → Stake + Redeem μέσω
   `place-order`, με `orderId` και τελική κατάσταση `Success`, στο
   `tests/data/testnet_roundtrip_<utc>.json`.
4. Τα αρχεία αυτά γίνονται commit στο repo· τα tests κλειδώνουν πάνω τους
   (`tests/test_recorded_payloads.py`) και κλείνουν τα ανεπιβεβαίωτα πεδία
   Bybit (HANDOFF §9).

Και τα δύο scripts αρνούνται να τρέξουν χωρίς `BYBIT_TESTNET=1`.

## Βήμα 9 — Επταήμερο dry-run και audit

Αφήνεις τα timers να τρέχουν 7 ημέρες. Καθημερινά η σύνοψη έρχεται στο
Telegram στις 06:55 UTC. Στο τέλος:

```bash
L=/opt/hermes/logs/yield_rotation
cat $L/*.jsonl | $PY -c '
import json, sys, collections
recs = [json.loads(l) for l in sys.stdin if l.strip()]
codes = collections.Counter(a.split(":")[0] for r in recs for a in r["alerts"])
states = collections.Counter(r["risk_state"] for r in recs)
live = [e for r in recs for e in r.get("executions", []) if e.get("executed")]
print("cycles", len(recs), "| states", dict(states))
print("codes", dict(codes))
print("dry_run everywhere:", all(r.get("dry_run") is True for r in recs), "| executed orders:", len(live))
'
```

Κριτήρια για να προχωρήσει ο Giannis:
- ~1008 κύκλοι (6/ώρα × 24 × 7), χωρίς κενά > 30'.
- `dry_run everywhere: True` και `executed orders: 0`.
- Κανένας εμποδιστικός κωδικός χωρίς εξήγηση· `DATA_UNAVAILABLE` μόνο
  σποραδικά.
- Οι περισσότεροι κύκλοι σε `NORMAL`.

Στείλε την έξοδο στον Giannis.

## Βήμα 10 — Μόνο ο Giannis

1. Νέο κλειδί Bybit: **μόνο Earn**, χωρίς Withdraw, IP whitelist στο VPS.
2. Κεφάλαια στο UNIFIED, ποσό 2–3 φορές το `minStakeAmount`.
3. `SIMULATED_IDLE_BALANCE: null` και `DRY_RUN: false` στο config (ο έλεγχος
   config αρνείται να τρέξει αν υπάρχει simulated balance με live).

---

## Πώς ελέγχεις ότι όλα τρέχουν

```bash
systemctl list-timers 'yield-*' --no-pager                  # επόμενη/τελευταία εκτέλεση
systemctl status yield-cycle.service yield-heartbeat.service --no-pager
journalctl -u yield-cycle -u yield-heartbeat --since -1h --no-pager | tail -n 40
sudo -u hermes $PY $REPO/risk_state.py verify               # code OK, state NORMAL
tail -n 1 /opt/hermes/logs/yield_rotation/$(date -u +%F).jsonl | $PY -m json.tool | head -n 60
```

Υγιές σύστημα: τελευταίος κύκλος < 10' πριν, `risk_state` `NORMAL` με
`code: OK`, κανένας εμποδιστικός κωδικός, `data_errors: {}`.

Exit codes του κύκλου: `0` ok · `3` config ή prompt · `4` crash (το record
έχει `crash` με traceback). Ένα `failed` στο `systemctl status yield-cycle`
σημαίνει exit ≠ 0 — δες το τελευταίο record.

## Επαναφορά

Αν υπάρχουν θέσεις και θες έξοδο, **πρώτα** UNWIND με τον κύκλο να τρέχει
(το UNWIND το εκτελεί ο κύκλος, χωρίς LLM), και μόνο όταν τα positions
αδειάσουν και οι εντολές γίνουν `Success`, σταμάτημα:

```bash
sudo -u hermes $PY $REPO/risk_state.py write UNWIND "rollback"
sudo -u hermes $PY $REPO/bybit_earn_tool.py --positions --coin USDT   # μέχρι να αδειάσει
sudo -u hermes $PY $REPO/bybit_earn_tool.py --orders                  # μέχρι Success
systemctl disable --now yield-cycle.timer yield-heartbeat.timer yield-summary.timer yield-telegram-bot.service
```

Τα backup του `.env` είναι στο `/opt/hermes/.env.bak.*` και το παλιό risk
state στο `/opt/hermes/state/risk_state.json.pre-v6.*`.
