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
έγκριση του Giannis.

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
   ξεχωριστό κλειδί testnet.

Το κλειδί (βήμα 7) το δίνεις στον Hermes για το `/opt/hermes/.env` ως
`BYBIT_CARRY_API_KEY` / `BYBIT_CARRY_API_SECRET`. Το `DRY_RUN: false` και το
`DEADMAN_URL` αλλάζουν στο git, όχι στο UI.
