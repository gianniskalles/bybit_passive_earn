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
