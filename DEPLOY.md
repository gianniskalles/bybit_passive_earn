# DEPLOY.md — εγκατάσταση στο VPS (για τον Hermes)

Φάση 6 του `FINISH_PLAN.md`. **Μία εντολή** κάνει όλη τη δουλειά· εσύ
επικολλάς το log.

Κανόνες:
- **Μην «διορθώσεις» τίποτα** στον κώδικα, στα units ή στο config στο VPS.
  Σε FAIL: επικόλλησε το log και σταμάτα.
- **Ποτέ `DRY_RUN: false`.** Το αλλάζει μόνο ο Giannis.
- **⛔ ΜΗΝ αγγίξεις:** `hermes-gateway`, `hermes-litellm`, `hermes-george`,
  `hermes-seo_agent` (και κανένα unit που δεν αρχίζει από `yield-`).

## Προϋποθέσεις

- [ ] Το PR είναι merged στο `main` και το CI (και τα δύο jobs, `pytest` και
  `vps-layout`) είναι πράσινο.
- [ ] Ο Giannis έχει κάνει το repo **private** (FINISH_PLAN Α4).

## Η εντολή

```bash
cd /opt/hermes/yield_rotation && sudo -u hermes git pull --ff-only origin main
sudo deploy/deploy.sh
```

Στο τέλος τυπώνει τη διαδρομή του log
(`/opt/hermes/logs/deploy/deploy_<utc>.log`). **Επικόλλησε ολόκληρο το log:**

```bash
sudo cat "$(ls -1t /opt/hermes/logs/deploy/deploy_*.log | head -n 1)"
```

- Κάθε βήμα τυπώνει την ωμή του έξοδο και μετά `PASS step n/7` ή
  `FAIL step n/7`. Στο πρώτο FAIL σταματά (`DEPLOY STOPPED at step n/7`).
- Η τελευταία γραμμή σε επιτυχία: `ALL 7 STEPS PASSED`.
- **Ξανατρέχει με ασφάλεια** (idempotent): κρατά έγκυρο κλειδί HMAC και risk
  state, παραλείπει το LLM regression αν έχει ήδη περάσει για το ίδιο prompt
  και μοντέλο, ξαναεγκαθιστά τα ίδια units.

## Τι κάνει το `deploy/deploy.sh`

| Βήμα | Τι | FAIL όταν |
|---|---|---|
| 1 | Βρίσκει units/cron με `yield\|heartbeat\|run_yield_cycle`. Απενεργοποιεί **μόνο** παλιά `yield-*` units· τα units του repo τα κρατά. | Unit που ταιριάζει αλλά **δεν** είναι `yield-*` (δεν το αγγίζει — το απενεργοποιείς με το χέρι αν είναι ο παλιός κύκλος), γραμμή cron που ταιριάζει (δεν αγγίζει crontab), ή αδυναμία ανάγνωσης systemd/crontab |
| 2 | Commit, καθαρό checkout, **δικό του venv** `/opt/hermes/venvs/yield_rotation` (το φτιάχνει αν λείπει) + `pip install` εκεί, **`pytest` ως hermes**, flags του `hermes chat` (το CLI μένει στο `/opt/hermes/.venv`, ανέγγιχτο) | Τοπικές αλλαγές, venv που δεν δημιουργείται, αποτυχημένο test, flag που λείπει |
| 3 | Κλειδιά στο `/opt/hermes/.env` — **μόνο fingerprints** (sha256). Νέο HMAC αν λείπει ή είναι το smoke-test· αντιγράφει `BYBIT_*` από το `/opt/data/.env` αν υπάρχουν μόνο εκεί· backup πριν από κάθε αλλαγή· mode 600 | Λείπει κλειδί Bybit ή Telegram, ή `BYBIT_TESTNET` είναι ενεργό (τότε **δεν γράφει τίποτα**) |
| 4 | Δοκιμαστικό μήνυμα Telegram | Δεν στάλθηκε |
| 5 | **Πρώτα: το config λέει `DRY_RUN: true`** (αλλιώς FAIL πριν τρέξει οτιδήποτε). Risk state: κρατά έγκυρο, μετακινεί στην άκρη (`.pre-v6.<utc>`) μη επαληθεύσιμο (π.χ. το παλιό χωρίς `source`) → heartbeat (bootstrap) → **ένας κύκλος με `--dry-run`** → heartbeat → `NORMAL` | `DRY_RUN` όχι `true`, εμποδιστικός κωδικός, `data_errors`, `NO_ELIGIBLE_PRODUCTS` (τυπώνει το `filtered_by_wrapper`), κατάσταση operator, όχι `NORMAL` |
| 6 | **LLM regression του prompt v6 με το πραγματικό μοντέλο** (5 σενάρια × 5), πριν από οποιονδήποτε timer | Οτιδήποτε κάτω από 25/25 |
| 7 | **Πρώτα: `DRY_RUN: true` ξανά.** `install.sh` (με `--with-bot` αν υπάρχει `YIELD_TELEGRAM_BOT_TOKEN`), `systemd-analyze verify`, timers `active` | `DRY_RUN` όχι `true` (δεν εγκαθίσταται κανένας timer), άκυρο unit, timer όχι active |
| τέλος | Τελευταίος έλεγχος `DRY_RUN: true`· **μόνο** μετά τυπώνει `ALL 7 STEPS PASSED … DRY_RUN: true (verified …)` | `DRY_RUN` όχι `true` |

**Δεν κάνει ποτέ:** αλλαγή σε unit που δεν αρχίζει από `yield-` (κάθε
`systemctl` περνά από έλεγχο που αρνείται), επεξεργασία crontab, τίποτα σε
testnet, εκτύπωση κλειδιού, αλλαγή του `DRY_RUN`, εγκατάσταση οτιδήποτε στο
`/opt/hermes/.venv` (το venv του Hermes CLI).

**Προαιρετικό — εντολές `/unwind`, `/resume`:** χρειάζονται δικό τους bot (ο
Giannis το φτιάχνει στο @BotFather). Αν δοθεί token, μπαίνει ως
`YIELD_TELEGRAM_BOT_TOKEN` στο `/opt/hermes/.env` και ξανατρέχεις το
`deploy.sh`· το βήμα 7 εγκαθιστά τότε και το bot.

---

## Μετά — Testnet (Φάση 6, βήμα 4) — ⛔ ΣΤΑΜΑΤΑ ΕΔΩ

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

## Μετά — Επταήμερο dry-run και audit

Μετά το `ALL 7 STEPS PASSED`, αφήνεις τα timers να τρέχουν 7 ημέρες. Καθημερινά η σύνοψη έρχεται στο
Telegram στις 06:55 UTC. Στο τέλος:

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
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

## Μετά — Μόνο ο Giannis

1. Νέο κλειδί Bybit: **μόνο Earn**, χωρίς Withdraw, IP whitelist στο VPS.
2. Κεφάλαια στο UNIFIED, ποσό 2–3 φορές το `minStakeAmount`.
3. `SIMULATED_IDLE_BALANCE: null` και `DRY_RUN: false` στο config (ο έλεγχος
   config αρνείται να τρέξει αν υπάρχει simulated balance με live).

---

## Πώς ελέγχεις ότι όλα τρέχουν

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
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
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
sudo -u hermes $PY $REPO/risk_state.py write UNWIND "rollback"
sudo -u hermes $PY $REPO/bybit_earn_tool.py --positions --coin USDT   # μέχρι να αδειάσει
sudo -u hermes $PY $REPO/bybit_earn_tool.py --orders                  # μέχρι Success
systemctl disable --now yield-cycle.timer yield-heartbeat.timer yield-summary.timer yield-telegram-bot.service
```

Τα backup του `.env` είναι στο `/opt/hermes/.env.bak.*` και το παλιό risk
state στο `/opt/hermes/state/risk_state.json.pre-v6.*`.

---

## Carry (CARRY_PLAN, Φάση 6) — αντικαθιστά το yield rotation

**Ένα σύστημα τη φορά, ποτέ και τα δύο στον ίδιο λογαριασμό** (CARRY_PLAN §13.2).

```bash
cd /opt/hermes/yield_rotation && sudo -u hermes git pull --ff-only origin main
sudo deploy/deploy.sh --system carry     # το carry: σβήνει τους timers του yield rotation
sudo deploy/deploy.sh                    # ξανά, αργότερα: κρατά το σύστημα που τρέχει
sudo deploy/deploy.sh --system yield     # πίσω στο yield rotation (μόνο αν το carry δεν κρατά τίποτα)
```

Ίδια 7 βήματα, ίδιο log, ίδιοι κανόνες (`yield-*` μόνο, ποτέ crontab, ποτέ
testnet, ποτέ `DRY_RUN: false`). Με `--system carry`:

| Βήμα | Διαφορά |
|---|---|
| 2 | Χωρίς έλεγχο του `hermes chat` (το carry δεν έχει LLM) |
| 3 | `keys --system carry`: δικό του κλειδί `BYBIT_CARRY_API_KEY` / `BYBIT_CARRY_API_SECRET` (subaccount μόνο για το carry· προαιρετικό όσο `DRY_RUN: true`, γιατί το paper διαβάζει μόνο δημόσια δεδομένα· **ποτέ** ίδιο με το `BYBIT_API_KEY` του yield rotation) |
| 5 | `config/carry.yaml` με `DRY_RUN: true` → carry risk state (άκυρο → στην άκρη) → **book**: άκυρο μετακινείται **μόνο** αν το carry δεν κρατά τίποτα, αλλιώς FAIL → heartbeat `--system carry` → ένας κύκλος (paper) → heartbeat → `NORMAL` |
| 6 | Δεν υπάρχει LLM regression (δεν υπάρχει prompt) |
| 7 | `install.sh --system carry`: **πρώτα** σβήνει `yield-cycle/heartbeat/summary.timer`, μετά ανοίγει `yield-carry-cycle` (5'), `yield-carry-heartbeat` (5', offset), `yield-carry-summary` (06:50 UTC), `yield-carry-calibrate` (1η του μήνα, 07:10 UTC). FAIL αν μείνει ενεργός έστω ένας timer του άλλου συστήματος |

**Δικλείδες για το book και το κλειδί HMAC.** Το book του carry είναι
υπογεγραμμένο με το `HERMES_RISK_HMAC_KEY`· νέο κλειδί = `BOOK_UNREADABLE` →
`BOOK_MISMATCH` → κράτημα. Γι' αυτό, όσο το carry κρατά οτιδήποτε (θέση στο
exchange, spot πάνω από dust, paper θέση, μη κενό book) **ή δεν μπορεί να
διαβαστεί**:
- το βήμα 3 **δεν** αλλάζει το κλειδί HMAC (FAIL, δεν γράφει τίποτα· επαναφέρεις
  το παλιό κλειδί)·
- το βήμα 5 **δεν** μετακινεί το book (FAIL· ανάκτηση με Telegram `/adopt carry`)·
- η επιστροφή στο yield rotation αρνείται στο βήμα 1, πριν από οτιδήποτε.

Έλεγχος με το χέρι: `sudo -u hermes $PY $REPO/deploy/preflight.py carry-exposure`
(0 = δεν κρατά τίποτα).

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
systemctl list-timers 'yield-*' --no-pager
sudo -u hermes $PY $REPO/deploy/preflight.py carry-exposure
sudo -u hermes $PY $REPO/risk_state.py --system carry verify   # code OK, state NORMAL
tail -n 1 /opt/hermes/logs/carry/$(date -u +%F).jsonl | $PY -m json.tool | head -n 80
```

**Telegram:** `/status` (και τα δύο συστήματα + κράτημα), `/unwind carry`,
`/resume carry` (λύνει και το `CARRY_HOLD`), `/unwind all`, `/adopt carry` —
όλα με `/confirm <κωδικός>`. Η ημερήσια σύνοψη του carry έχει μία γραμμή ανά
σύμβολο, π.χ. `ETH: 2,6% — χρειάζεται 6,7% (5% πάνω από το Earn 1,7%)`.

**Μετά το deploy:** 14 ημέρες paper trading (CARRY_PLAN §10). Testnet μόνο με
έγκριση του Giannis. Με `BYBIT_TESTNET=1` το carry γράφει μόνο στο
`/opt/hermes/state/carry-testnet/` και στο `/opt/hermes/logs/carry-testnet`
(config `carry.testnet.yaml`)· τα αρχεία του mainnet δεν τα αγγίζει, και με
το config του mainnet αρνείται να τρέξει. Υπογράφει **μόνο** με
`BYBIT_CARRY_TESTNET_API_KEY` / `_SECRET` (ποτέ με το `BYBIT_CARRY_API_KEY`), και
ο κύκλος του mainnet ποτέ με αυτά· το βήμα 3 αποτυγχάνει αν το κλειδί testnet
είναι ίδιο με κλειδί του mainnet.

## Carry — τι κάνει ο Giannis στο Bybit UI (και πουθενά αλλού)

Τα παρακάτω δεν γίνονται από το API ή από τον Hermes. Σειρά:

1. **Subaccount μόνο για το carry.** Κεντρικός λογαριασμός → Subaccounts →
   Create. Τύπος **Unified Trading Account**. Κανένα άλλο bot ή χειροκίνητη
   συναλλαγή εκεί, ποτέ.
2. **Earn στο subaccount.** Μέσα στο subaccount: Earn → Easy Earn → USDT
   Flexible. Έλεγξε ότι επιτρέπεται εγγραφή. Αν όχι, σταμάτα και πες το: το
   στρώμα A θέλει άλλο σχέδιο.
3. **Cross Margin.** Στο subaccount: Unified Trading Account → Margin Mode →
   **Cross Margin** (όχι Isolated, όχι Portfolio).
4. **One-Way για το ETHUSDT perpetual.** Derivatives → USDT Perpetual →
   ρυθμίσεις → Position Mode → **One-Way Mode**. Ο κώδικας αρνείται Hedge Mode.
5. **ETH ως εγγύηση.** Assets → Unified Trading → Collateral → ETH →
   **On**. Το spot σκέλος πρέπει να μετρά ως εγγύηση για το short.
6. **Spot Margin Trading: Off.** Το carry δεν δανείζεται ποτέ.
7. **API key του subaccount.** API Management → Create → System-generated:
   - Read-Write. Unified Trading: **Orders** και **Positions**, **Spot**
     (trade), **Earn**.
   - **Χωρίς Withdraw, χωρίς Transfer**, χωρίς τίποτα άλλο.
   - IP restriction: **μόνο η IP του VPS**.
   - Δεν είναι το κλειδί του yield rotation (κεντρικός λογαριασμός).
8. **Κεφάλαιο, μόνο πριν από το live.** Μεταφορά 100 USDT
   (`TOTAL_CAPITAL_CAP_USD`) από τον κεντρικό λογαριασμό στο Unified Trading
   του subaccount. Το κλειδί δεν μπορεί να κάνει transfer. Κανένα ETH ή άλλο
   νόμισμα στο subaccount: ό,τι δεν είναι στο book είναι `FOREIGN_BALANCE`.
9. **Testnet, όταν το εγκρίνεις.** Τα ίδια 1–7 στο testnet.bybit.com, με
   ξεχωριστό κλειδί testnet, που μπαίνει **μόνο** στα
   `BYBIT_CARRY_TESTNET_API_KEY` / `BYBIT_CARRY_TESTNET_API_SECRET`.

Το κλειδί (βήμα 7) το δίνεις στον Hermes για το `/opt/hermes/.env` ως
`BYBIT_CARRY_API_KEY` / `BYBIT_CARRY_API_SECRET`. Το `DRY_RUN: false` και το
`DEADMAN_URL` αλλάζουν στο git, όχι στο UI.

## Carry — testnet (μόνο με έγκριση του Giannis)

> **17/10: παραλείπεται.** Τη θέση του παίρνει ο επιβλεπόμενος κύκλος στο
> mainnet (ενότητα «Carry — επιβλεπόμενος κύκλος στο mainnet»). Τα εργαλεία
> μένουν, αλλά δεν τρέχουν.

Όλα με `BYBIT_TESTNET=1` **στην εντολή** (ποτέ στο `.env`). Το carry τότε:
- διαβάζει το `config/carry.testnet.yaml` (`DRY_RUN: false`: πραγματικές
  εντολές **testnet**· το `config/carry.yaml` μένει `DRY_RUN: true`)·
- γράφει μόνο στα `/opt/hermes/state/carry-testnet/` και
  `/opt/hermes/logs/carry-testnet/`·
- υπογράφει μόνο με `BYBIT_CARRY_TESTNET_API_KEY` / `_SECRET`·
- κρατά κάθε απάντηση της Bybit στο `/opt/hermes/logs/carry-testnet/raw/`
  (μόνο το σώμα· κανένα header, κλειδί ή υπογραφή).

Οι timers του mainnet συνεχίζουν κανονικά.

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
cd $REPO && sudo -u hermes git pull --ff-only origin main
grep -n '^DRY_RUN' $REPO/config/carry.yaml $REPO/config/carry.testnet.yaml   # true / false
sudo -u hermes $PY $REPO/deploy/preflight.py keys --system carry   # testnet key: fingerprint, ≠ mainnet
```

**Α. Capture (μόνο ανάγνωση):**

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
sudo -u hermes env HOME=/opt/hermes BYBIT_TESTNET=1 $PY $REPO/scripts/testnet.py carry-capture
```

→ `tests/data/carry/testnet_<utc>.json` και ό,τι τυπώθηκε στο stderr.

**Β. Αναγκαστικός πλήρης κύκλος** (redeem → είσοδος → ≥1 settlement → έξοδος →
USDT πίσω στο Earn), ως transient unit:

```bash
sudo systemd-run --uid=hermes --gid=hermes --unit=yield-carry-testnet \
    --setenv=HOME=/opt/hermes --setenv=BYBIT_TESTNET=1 \
    --property=WorkingDirectory=/opt/hermes/yield_rotation \
    /opt/hermes/yield_rotation/scripts/carry_testnet_loop.sh
systemctl status yield-carry-testnet --no-pager
journalctl -u yield-carry-testnet --since -30min --no-pager | tail -n 40
```

Κάθε 5': ένας κύκλος και ένας heartbeat. Ο βρόχος αρνείται να ξεκινήσει χωρίς
`BYBIT_TESTNET=1`, ή αν έχει μείνει αρχείο `STOP` από προηγούμενο τρέξιμο.
Το `deploy.sh` δεν αγγίζει αυτό το unit.

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
L=/opt/hermes/logs/carry-testnet
# ο τελευταίος κύκλος: κατάσταση, αποφάσεις, ενέργειες, εντολές, alerts
tail -n 1 $L/$(date -u +%F).jsonl | $PY -c 'import json,sys; r=json.loads(sys.stdin.read()); print(r["risk_state"], r["decisions"], [a["kind"] for a in r["actions"]], len(r["orders"]), r["alerts"])'
grep -h '"type": "funding"' $L/ledger/*.jsonl | tail -n 3              # funding μετά από settlement
# έξοδος, ΜΟΝΟ αφού φανεί funding:
sudo -u hermes env HOME=/opt/hermes BYBIT_TESTNET=1 $PY $REPO/risk_state.py --system carry write UNWIND "testnet forced exit"
```

Σειρά που περιμένουμε:
1. NO_NEW_POSITIONS (bootstrap) → NORMAL.
2. `EARN_REDEEM_FOR_ENTRY` → perp + spot → γραμμή funding στο ledger.
3. UNWIND → τα δύο σκέλη κλείνουν (book FLAT) → `EARN_RETURN`.

**Σταμάτημα:**
- Κανονικά με το αρχείο STOP: ο τρέχων κύκλος τελειώνει πρώτα.
  `sudo -u hermes touch /opt/hermes/state/carry-testnet/STOP`
- Αμέσως, μόνο σε ανάγκη: `sudo systemctl stop yield-carry-testnet`.
- Μετά: `sudo systemctl reset-failed yield-carry-testnet 2>/dev/null || true`.
  Το `STOP` το σβήνεις πριν από νέο τρέξιμο.

**Παράδοση:**

```bash
sudo tar czf /opt/hermes/carry_testnet_$(date -u +%Y%m%dT%H%M%SZ).tgz \
    /opt/hermes/logs/carry-testnet /opt/hermes/state/carry-testnet
```

Δεν περιέχει κλειδιά.

**Αν εμφανιστεί** `CARRY_HOLD`, `ORPHAN_LEG`, `BOOK_MISMATCH` ή `CYCLE_CRASH`:
1. UNWIND, όπως πιο πάνω.
2. Περιμένεις FLAT.
3. STOP, και στέλνεις το tgz.

## Carry — επιβλεπόμενος κύκλος στο mainnet (μία φορά, ~40 USDT)

`scripts/mainnet_roundtrip.py`, μίας χρήσης:
1. redeem από το Earn
2. είσοδος στο ελάχιστο μέγεθος ETHUSDT
3. αναμονή ενός settlement
4. έξοδος
5. USDT πίσω στο Earn
6. σταματά μόνο του

Κάθε βήμα είναι ένας κύκλος της παραγωγής: ίδιο plan, ίδια εκτέλεση, ίδιο
book, ίδιο ledger, ίδιο Telegram.
- **Όριο:** σκληρό όριο 50 USDT notional· καμία επιλογή δεν το ανεβάζει.
- **Καταγραφή:** οι ακατέργαστες απαντήσεις μπαίνουν στο
  `/opt/hermes/logs/carry/raw/`, μόνο το σώμα (κανένα header, κλειδί ή
  υπογραφή).
- **Config:** το `config/carry.yaml` **δεν αλλάζει** (μένει `DRY_RUN: true`).
- **Risk state:** δεν γράφεται ποτέ. Η έξοδος επιβάλλεται μόνο στη μνήμη του
  script, και μόνο αυστηρότερα από το υπογεγραμμένο state.

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
cd $REPO && sudo -u hermes git pull --ff-only origin main
# 1. οι timers του carry σταματούν όσο τρέχει (ο heartbeat τρέχει από το ίδιο το script)
sudo systemctl stop yield-carry-cycle.timer yield-carry-heartbeat.timer \
    yield-carry-summary.timer yield-carry-calibrate.timer
# 2. έλεγχος, μόνο ανάγνωση: ελάχιστη θέση, USDT που χρειάζονται, όλα τα εμπόδια
sudo -u hermes env HOME=/opt/hermes $PY $REPO/scripts/mainnet_roundtrip.py --check
```

**Μόνο αφού ο Giannis δει την έξοδο του `--check` και πει «πάμε»:**

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
sudo systemd-run --uid=hermes --gid=hermes --unit=yield-carry-roundtrip \
    --setenv=HOME=/opt/hermes --property=WorkingDirectory=/opt/hermes/yield_rotation \
    --property=TimeoutStopSec=600 \
    $PY $REPO/scripts/mainnet_roundtrip.py --i-approve-real-money
journalctl -u yield-carry-roundtrip --since -30min --no-pager | tail -n 40
```

**Τι αρνείται:**
- χωρίς `--i-approve-real-money`·
- με `BYBIT_TESTNET`·
- αν το `carry.yaml` δεν λέει `DRY_RUN: true`·
- αν τρέχει timer του carry·
- αν το carry δεν είναι FLAT·
- αν το carry risk state δεν είναι NORMAL ή υπάρχει `CARRY_HOLD`·
- αν το ETH δεν είναι ενεργό collateral ή ο λογαριασμός δεν είναι Cross Margin·
- αν η ελάχιστη θέση με το buffer των 5 USDT ξεπερνά τα 50 USDT ή τα διαθέσιμα USDT.

Όσο τρέχει κρατά το lock του κύκλου, και το `deploy.sh` αρνείται να τρέξει.

**Διάρκεια:** περίπου 8–9 ώρες (ως το επόμενο settlement + 10'). Κάθε εντολή
έρχεται στο Telegram.

**Πρόωρο σταμάτημα:** `sudo systemctl stop yield-carry-roundtrip`. Το script
πρώτα κλείνει και τα δύο σκέλη (έως 10'), και μετά τερματίζει. Αν κοπεί
βίαια με ανοιχτή θέση:

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
sudo systemd-run --uid=hermes --gid=hermes --unit=yield-carry-roundtrip \
    --setenv=HOME=/opt/hermes --property=WorkingDirectory=/opt/hermes/yield_rotation \
    --property=TimeoutStopSec=600 \
    $PY $REPO/scripts/mainnet_roundtrip.py --i-approve-real-money --finish
```

**Τέλος:**
- Αποτέλεσμα στο `/opt/hermes/logs/carry/roundtrip_<utc>.json`.
- Κωδικοί εξόδου:
  - 0 = FLAT και USDT πίσω στο Earn·
  - 5 = FLAT χωρίς είσοδο (ο λόγος είναι στο report)·
  - 4 = έμεινε θέση (σταμάτα και ειδοποίησε).

Μετά:

```bash
sudo systemctl start yield-carry-cycle.timer yield-carry-heartbeat.timer \
    yield-carry-summary.timer yield-carry-calibrate.timer
sudo tar czf /opt/hermes/carry_roundtrip_$(date -u +%Y%m%dT%H%M%SZ).tgz \
    /opt/hermes/logs/carry/roundtrip_*.json /opt/hermes/logs/carry/raw /opt/hermes/logs/carry/ledger
```

### Δεύτερος επιβλεπόμενος κύκλος (μετά το audit της 18/10)

Ίδιες εντολές με πάνω (`--check`, μετά «πάμε», μετά `--i-approve-real-money`).
Το `DRY_RUN` του `carry.yaml` μένει `true`. Τι αλλάζει:
- **Πριν την είσοδο:** τα 0,00000998 ETH του πρώτου κύκλου (κάτω από το
  ελάχιστο spot) μπαίνουν στο book ως υπόλειμμα (μήνυμα στο Telegram). Η
  αγορά spot είναι τόσο μικρότερη (0,01001 αντί 0,01002).
- **Στο hold:** η θέση πρέπει να διαβάζεται σε κάθε κύκλο. Αν δεν διαβαστεί
  → `CRITICAL: POSITION_UNREADABLE` αμέσως στο Telegram, και το script πάει
  κατευθείαν στην έξοδο.
- **Στο report** (`roundtrip_<utc>.json`, ενότητα `verify`), τι περιμένουμε:

  | Πεδίο | Αναμενόμενο |
  |---|---|
  | `position_unreadable_cycles` | 0 |
  | `earn.apr` | το APR της κλίμακας (~6,9% με < 100 USDT στο Earn), όχι 1,94% |
  | `dust_adopted` / `dust_before` | 0,00000998 |
  | `dust_after` | ένα νέο υπόλειμμα < 0,00001, και το ίδιο ποσό ETH στο wallet |
  | `ledger_minus_wallet` | ~0: το ledger ίσο με την αλλαγή του wallet, εκτός από τον τόκο του Earn |

Μετά, εκτός από τους timers, πακετάρισμα και των records:

```bash
sudo tar czf /opt/hermes/carry_roundtrip2_$(date -u +%Y%m%dT%H%M%SZ).tgz \
    /opt/hermes/logs/carry/roundtrip_*.json /opt/hermes/logs/carry/raw \
    /opt/hermes/logs/carry/ledger /opt/hermes/logs/carry/*.jsonl
```

Paper και live έχουν πλέον χωριστό book (`carry_paper_book.json`) και ledger
(`/opt/hermes/logs/carry/paper/ledger/`). Στον πρώτο κύκλο DRY_RUN μετά το
deploy, οι παλιές γραμμές paper φεύγουν από το live ledger μία φορά
(`LEDGER_SPLIT`, όχι blocking).

## Carry — μετάβαση σε live (μόνο ο Giannis)

Το `config/carry.yaml` μένει **για πάντα** `DRY_RUN: true` (ένα test το
κλειδώνει), και το `deploy.sh` αρνείται οτιδήποτε άλλο.

**Ο διακόπτης.** Live σημαίνει ένα υπογεγραμμένο αρχείο εκτός repo,
`/opt/hermes/state/carry_live.json`. Υπάρχουν δύο τρόποι να γραφτεί. Και οι
δύο κάνουν τους ίδιους ελέγχους (`carry/live_ops.py`) και γράφουν το ίδιο
αρχείο:
- **Telegram** (ο Giannis δεν έχει SSH):
  `/live enable 100` → `/confirm <κωδικός> LIVE 100 USDT`.
- **Τερματικός:** `scripts/carry_live.py enable --capital-cap 100`.

**Προστασίες του Telegram** (ίδιες με το `/unwind`):
- μόνο από το επιτρεπόμενο chat **και** τον επιτρεπόμενο αποστολέα· κάθε
  άλλο μήνυμα αγνοείται χωρίς απάντηση·
- πρώτα τρέχουν όλοι οι έλεγχοι. Αν κάτι λείπει: «❌ Not live» με τη λίστα, και
  κανένας κωδικός·
- κωδικός μίας χρήσης, ισχύει 10 λεπτά, μία μόνο προσπάθεια·
- επιβεβαίωση μόνο με την ακριβή φράση `LIVE <cap> USDT` μετά τον κωδικό·
- οι έλεγχοι τρέχουν **ξανά** στο `/confirm`, ώστε να πιαστεί ό,τι άλλαξε
  στο μεταξύ.

**Οι έλεγχοι:**
- mainnet· το `carry.yaml` με `DRY_RUN: true` και `DEADMAN_URL: null`·
- δεν υπάρχει ήδη διακόπτης·
- στο `.env`: κλειδί HMAC, το κλειδί και το secret του subaccount, ένα
  `DEADMAN_URL` με https·
- το όριο είναι ≤ `TOTAL_CAPITAL_CAP_USD`, και το config του live περνά
  πλήρη validation·
- δεν τρέχει ο επιβλεπόμενος κύκλος·
- το risk state είναι NORMAL, χωρίς CARRY_HOLD·
- Cross Margin, ETH collateral On, θέσεις και Earn αναγνώσιμα·
- το carry είναι FLAT (exchange και live book).

**Το όριο κεφαλαίου.** Υπογράφεται μέσα στον διακόπτη. Μια μεταγενέστερη
αλλαγή του `TOTAL_CAPITAL_CAP_USD` στο repo δεν το ανεβάζει ποτέ.

**Ο διακόπτης και το deploy.** Όσο υπάρχει ο διακόπτης, το `deploy.sh`
αρνείται να τρέξει (βήμα 1): ο χειροκίνητος κύκλος του θα έκανε live
συναλλαγές.

| Περίπτωση | Τι κάνει ο κύκλος |
|---|---|
| Χωρίς διακόπτη | paper, όπως πριν |
| Έγκυρος διακόπτης | live |
| Διακόπτης που δεν επαληθεύεται (χαλασμένος, άλλο κλειδί) | ποτέ live, και CRITICAL |
| Χαμένος ή χαλασμένος διακόπτης με ανοιχτή live θέση | live **μόνο UNWIND** ως FLAT, και CRITICAL |

Το τελευταίο υπάρχει για να μη μείνει ποτέ πραγματική θέση χωρίς διαχείριση.

**DEADMAN_URL:** μόνο στο `/opt/hermes/.env`, ποτέ στο repo. Το γράφει ο
Hermes· δεν είναι μυστικό υψηλής αξίας. Χωρίς αυτό ένας κύκλος live δεν τρέχει
καθόλου (`CONFIG_INCOMPLETE`). Το pingάρουν και οι κύκλοι paper, άρα ελέγχεται
πριν το live.

### Hermes (πριν το live)

```bash
PY=/opt/hermes/venvs/yield_rotation/bin/python REPO=/opt/hermes/yield_rotation
cd $REPO && sudo -u hermes git pull --ff-only origin main
sudo $REPO/deploy/deploy.sh --system carry      # paper, όπως πάντα· επανεκκινεί και το bot
# DEADMAN_URL (το ping URL που έδωσε ο Giannis) στο .env:
sudo -u hermes sh -c 'grep -q "^DEADMAN_URL=" /opt/hermes/.env || \
    echo "DEADMAN_URL=https://hc-ping.com/<uuid>" >> /opt/hermes/.env'
sudo -u hermes env HOME=/opt/hermes $PY $REPO/deploy/preflight.py keys --system carry
#   DEADMAN_URL: OK (from the .env; never in the repo)  sha256:...
systemctl is-active yield-telegram-bot.service           # active
sudo -u hermes env HOME=/opt/hermes $PY $REPO/scripts/carry_live.py status
#   paper (DRY_RUN): no live switch ...   carry exposure: FLAT
```

Ο Hermes **δεν** ενεργοποιεί ποτέ το live (ούτε `/live`, ούτε
`carry_live.py enable`/`disable`).

### Giannis

1. **healthchecks.io:**
   - νέο check «carry», period 5 λεπτά, grace 15 λεπτά·
   - ειδοποίηση στο email ή στο Telegram σου·
   - δίνεις το ping URL στον Hermes.
2. **Έλεγχος του deadman:** μετά από 1–2 κύκλους paper, το check στο
   healthchecks.io γίνεται πράσινο.
3. **Bybit UI**, όπως στους επιβλεπόμενους κύκλους:
   - Cross Margin·
   - ETH collateral On·
   - το Earn του subaccount όπως το θέλεις.
4. **Στο Telegram:** `/live` δείχνει paper και exposure FLAT.
5. **Ενεργοποίηση:** `/live enable 100`.
   - Αν κάτι λείπει, απαντά «❌ Not live» με τη λίστα.
   - Αλλιώς απαντά με κωδικό.
   - Μέσα σε 10 λεπτά στέλνεις ακριβώς `/confirm <κωδικός> LIVE 100 USDT`.
   - Απάντηση: «🟢 CARRY LIVE». Ο επόμενος κύκλος κάνει live συναλλαγές.
6. **Σταμάτημα:**
   - `/unwind carry`·
   - περιμένεις FLAT·
   - `/live disable` και μετά `/confirm <κωδικός>`. Το disable αρνείται όσο
     υπάρχει θέση.

   Για νέο deploy: disable → deploy → enable ξανά.

**Όριο 100 USDT της κλίμακας του Earn (διόρθωση 18/10).** Η είσοδος παίρνει
το κόστος της από το **πάνω** κομμάτι του υπολοίπου (Earn + αδρανή USDT).
Γι' αυτό το στρώμα A της απόφασης εισόδου είναι το APR αυτού του κομματιού.

Παράδειγμα με 150 USDT στο Earn και όριο θέσης 80: το κομμάτι [70, 150] είναι
30 USDT στο 6,94% και 50 στο 1,94%, άρα 3,82%. Ο μέσος όρος σε όλο το ποσό
θα ήταν 5,27%.

Ως 100 USDT είναι πάντα 6,94%. Η ημερήσια σύνοψη δείχνει:
- «Earn: X%»: ο μέσος όρος, όσο πραγματικά αποδίδει το Earn·
- «χρειάζεται Y%»: το κατώφλι εισόδου, πάνω στο κομμάτι.

## Carry — ανέβασμα σε ~200 USDT και BTCUSDT (απόφαση 24), ενώ είμαστε live

**Το config** (στο PR, merge τη μέρα των χρημάτων):
- `SYMBOLS: [ETHUSDT, BTCUSDT]`·
- `TOTAL_CAPITAL_CAP_USD: 200`·
- `MAX_NOTIONAL_BY_SYMBOL: {BTCUSDT: 130, ETHUSDT: 50}`·
- `USDT_BUFFER_USD: 20` (10%).

Σύνολο: 130 + 50 + 20 = 200.

**Γιατί χωριστά όρια:** ένα κοινό όριο 90 άφηνε στο BTC μόνο 5% περιθώριο.

| Σύμβολο | Ελάχιστη θέση | Όριο | Χωρά ως | Περιθώριο |
|---|---|---|---|---|
| BTC | 0,001 perp / 0,001002 spot, ~85,5 USDT στα 85.300 | 130 | ~129.700 USDT/BTC | +52% |
| ETH | 0,01 perp / 0,01002 spot | 50 | ~4.990 USDT/ETH | +106% από τα 2.417 |

**Η σειρά μετράει:**
- **Το deploy.sh αρνείται όσο υπάρχει ο διακόπτης.**
- **Δεν περνά κατάσταση operator:** θέλει risk state NORMAL, και ο heartbeat
  δεν αλλάζει ποτέ κατάσταση operator. Γι' αυτό το `/resume carry` μπαίνει
  πριν το deploy.
- **`/live enable 100` αρνείται με το νέο config:** τα όρια 2 × 90 + 20 δεν
  χωράνε στο 100. Μόνο `/live enable 200`.
- **Αν γίνει `git pull` χωρίς τη σειρά** ενώ είμαστε live: ο κύκλος μένει
  live αλλά `NO_NEW_POSITIONS`, με `CRITICAL: LIVE_CONFIG_BEYOND_SWITCH`.
  Κλείνει, δεν ανοίγει, και δεν σταματά.

| # | Ποιος | Τι |
|---|---|---|
| 1 | Giannis | Telegram `/unwind carry` → `/confirm <κωδικός>`. Περιμένεις ως το `/live` να δείξει `carry exposure: FLAT` (λίγοι κύκλοι). |
| 2 | Giannis | `/live disable` → `/confirm <κωδικός>` → «⚪ CARRY back to paper». |
| 3 | Giannis | `/resume carry` → `/confirm <κωδικός>` (NORMAL, για το deploy και το enable). |
| 4 | Giannis | Merge του PR (ή ο Claude, όταν του το πεις). |
| 5 | Hermes | `cd $REPO && sudo -u hermes git pull --ff-only origin main` και `sudo $REPO/deploy/deploy.sh --system carry` → «ALL 7 STEPS PASSED». |
| 6 | Giannis | Bybit UI (subaccount carry): Assets → Collateral → **BTC On** (το ETH είναι ήδη On). |
| 7 | Hermes | `sudo -u hermes env HOME=/opt/hermes $PY $REPO/deploy/preflight.py carry-symbols` → `OK`. Για κάθε σύμβολο δείχνει την ελάχιστη θέση, το όριό του (BTC 130, ETH 50) και την τιμή όπου παύει να χωρά. Το BTC και το ETH πρέπει να είναι collateral `active`. Με `FAIL` σταματάμε. |
| 8 | Giannis | `/live enable 200` → «All checks pass» → `/confirm <κωδικός> LIVE 200 USDT` → «🟢 CARRY LIVE». |
| 9 | Giannis | Bybit: μεταφορά των νέων USDT **Funding → Unified Trading** του subaccount. Ο επόμενος κύκλος live τα βάζει μόνος του στο Earn (`EARN_RETURN`). Αν υπάρχει ανοιχτή θέση, κρατά τα 20 του buffer. |
| 10 | Hermes | `/live` → `LIVE ... capital cap 200 USDT`. Μετά από έναν κύκλο: το `journalctl -u yield-carry-cycle` δείχνει `EARN_RETURN`, και η ημερήσια σύνοψη «Earn: X% στα Y USDT». |

**Αν στο βήμα 7 ένα σύμβολο «DOES NOT FIT»** (BTC πάνω από ~129.700 ή ETH πάνω
από ~4.990): ο κύκλος το αφήνει έξω με `SYMBOL_BELOW_MIN_SIZE`, και το enable
αρνείται. Το `carry-symbols` δείχνει για κάθε σύμβολο την τιμή στην οποία
παύει να χωρά. Η διόρθωση είναι να αλλάξουν τα δύο όρια στο
`MAX_NOTIONAL_BY_SYMBOL`, με το ίδιο σύνολο 180.

**Νέα USDT στο UTA (επιβεβαιωμένο με tests):**
- **Πότε μπαίνουν στο Earn:** όλα, σε κάθε κύκλο live χωρίς άλλη ενέργεια·
  με ανοιχτή θέση, όλα εκτός από τα 20 του buffer.
- **Τι δεν αλλάζει:**
  - το book μένει ως έχει, μαζί με το υπόλειμμα ETH·
  - το ledger γράφει μόνο την εντολή Earn, χωρίς PnL, χωρίς χρεώσεις, χωρίς
    round trip.
- **Εξαιρέσεις:**
  - Αν εκείνη τη στιγμή μια είσοδος περιμένει redeem, η επιστροφή στο Earn
    περιμένει.
  - Αν το funding περνά το κατώφλι εισόδου, τα νέα USDT χρηματοδοτούν
    είσοδο αντί για Earn. Αυτό είναι σωστό.
