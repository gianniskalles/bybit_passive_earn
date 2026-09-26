# HANDOFF.md

> **Σημείο εκκίνησης κάθε νέας συνεδρίας.** Διάβασέ το ολόκληρο πριν αλλάξεις
> οτιδήποτε. Το σχέδιο ολοκλήρωσης είναι το `FINISH_PLAN.md`· οι οδηγίες
> εγκατάστασης για τον Hermes είναι το `DEPLOY.md`.

**Project:** Bybit Earn yield rotation (USDT, FlexibleSaving)
**Repo στο VPS:** `/opt/hermes/yield_rotation/`
**Κατάσταση:** Φάσεις 0–5 του `FINISH_PLAN.md` ολοκληρωμένες. Η Φάση 6
(deploy, testnet, επταήμερο dry-run, live) **δεν έχει ξεκινήσει**.
`DRY_RUN: true`.

---

## 1. Αρχεία

| Αρχείο | Ρόλος |
|---|---|
| `run_yield_cycle.py` | Ο wrapper. Ένας κύκλος: config → risk state → δεδομένα Bybit → (LLM) → πύλες → ποσά → εκτέλεση → record. |
| `heartbeat.py` | Ο μόνος αυτόματος συντάκτης του `risk_state.json` (bootstrap, ανανέωση NORMAL). |
| `risk_state.py` | Ανάγνωση / εγγραφή / επαλήθευση του `risk_state.json`. CLI: `verify`, `write` (operator). |
| `signing.py` | **Μοναδική** υλοποίηση canonical JSON + HMAC-SHA256. |
| `executor.py` | Εκτέλεση του πλάνου· dry-run ή live, ίδιο request. |
| `bybit_earn_tool.py` | Bybit client. Σφάλμα = εξαίρεση, ποτέ `[]`. CLI μόνο ανάγνωσης. |
| `settings.py` | Όλα τα paths (env με default για το VPS) και το ενιαίο `load_env`. |
| `notify.py` | Ο μοναδικός αποστολέας Telegram, με dedup. |
| `summary.py` | Ημερήσια σύνοψη στο Telegram. |
| `telegram_bot.py` | `/status`, `/unwind`, `/resume` με επιβεβαίωση. |
| `prompt_v6.md` | Το prompt της παραγωγής (`PROMPT_VERSION: v6`). `prompt_v5.md` για σύγκριση· `archive/` τα παλαιότερα. |
| `config/yield_rotation.yaml` | Όλες οι παράμετροι (§6 κλειδωμένες). Κάθε πεδίο υποχρεωτικό. |
| `deploy/` | systemd units (`ExecStart` από το **δικό του venv** `/opt/hermes/venvs/yield_rotation`· το `/opt/hermes/.venv` είναι του Hermes CLI και δεν αγγίζεται), `install.sh`, και το **`deploy.sh`** (βήματα 1–7 του `DEPLOY.md` σε μία εντολή, PASS/FAIL ανά βήμα, log) με τους ελέγχους του στο `preflight.py`. |
| `scripts/testnet.py` | `capture` / `roundtrip` — μόνο με `BYBIT_TESTNET=1`. |
| `tests/` | `pytest` (χωρίς δίκτυο, χωρίς `/opt`)· `run_regression.py` (LLM, μόνο VPS). |

## 2. Ο κύκλος (`run_yield_cycle.run_cycle`)

1. **Config** — ελέγχεται ολόκληρο (`CONFIG_SCHEMA`: τύποι, εύρη). Άκυρο ή
   μη αναγνώσιμο → record `CONFIG_INCOMPLETE`, exit 3, τίποτα δεν τρέχει.
   `SIMULATED_IDLE_BALANCE` πρέπει να είναι `null` όταν `DRY_RUN: false`.
2. **Risk state** — `risk_state.verify()` → ενεργή κατάσταση:

   | Εγγραφή | Ενεργή κατάσταση |
   |---|---|
   | έγκυρη + φρέσκια (≤ 30') | ό,τι είναι υπογεγραμμένο |
   | έγκυρη, παλιά ή `ts` στο μέλλον | NORMAL→NO_NEW_POSITIONS, NO_NEW_POSITIONS→ίδιο, UNWIND→UNWIND |
   | απούσα / άκυρη / κακοσχηματισμένη / χωρίς κλειδί | NO_NEW_POSITIONS |

   Ο κύκλος **δεν τερματίζει ποτέ** λόγω risk state.
3. **Δεδομένα Bybit**, κάθε πηγή χωριστά· αποτυχία → `DATA_UNAVAILABLE`
   (μη-εμποδιστικό) και **fail closed**:
   - positions μη αναγνώσιμα, ή **μία** θέση χωρίς αναγνώσιμο
     `productId`/`amount` → κανένα LLM, καμία εντολή.
   - balance ή orders μη αναγνώσιμα → καμία STAKE (`DATA_GATE_DROPPED_STAKE`).
   - Νομίσματα κανονικοποιούνται σε κεφαλαία παντού.
4. **Scan** — μόνο προϊόντα του whitelist με status `Available`, χωρίς tiered
   APR, με γνωστό `redemption_eta_hours = redeemProcessingMinute/60` ≤
   `MAX_REDEMPTION_ETA_HOURS`, φρέσκο APR history (ανά `productId`,
   ταξινομημένο) και ≥ 6 σημεία στις τελευταίες 24 ώρες (`apr_ma_24h` =
   μέσος όρος στο χρονικό παράθυρο). Κάθε απόρριψη → `filtered_by_wrapper`.
   Κανένα πεδίο δεν «μαντεύεται» (κανόνας 7: άγνωστο = `null`).
5. **Απόφαση**
   - `UNWIND` → **το LLM δεν καλείται**· `REDEEM_ALL` για κάθε νόμισμα.
   - Αλλιώς: `prompt_<PROMPT_VERSION>.md` (λείπει → exit 3, sha256 στο
     record) → `hermes chat --query-file /dev/stdin -Q --toolsets= -m
     <RESOLVED_MODEL> --reasoning <...>` (χωρίς tools, prompt από stdin) →
     `extract_json` (το **τελευταίο** αντικείμενο με το `cycle_id` του
     κύκλου) → επικύρωση (`product_id` παντού, χωρίς ROTATE, ποσά του LLM
     αγνοούνται) → STAKE μόνο σε προϊόν του scan, REDEEM μόνο σε θέση.
6. **Πύλες** (ντετερμινιστικές, τελευταίες):
   - STAKE μόνο σε `NORMAL` (`RISK_GATE_DROPPED_STAKE`)· και το `Executor`
     αρνείται STAKE από μόνο του.
   - Θέση σε προϊόν με status ≠ Available → REDEEM από τον wrapper.
7. **Ποσά** (ποτέ από το LLM): `min(idle − RESERVE_USD, MAX_PER_PRODUCT_USD −
   (θέση + εκκρεμείς Stake + Stake με Success στα τελευταία 30'),
   remaining_capacity, max_stake_amount)` — το τελευταίο καλύπτει μια
   επιτυχημένη Stake που δεν φαίνεται ακόμα στα positions,
   προς τα κάτω στο `precision`· παράλειψη κάτω από `max(MIN_MOVE_USD,
   min_stake_amount)`. Άγνωστο `precision`/`min`/`max` → καμία STAKE.
   REDEEM = ολόκληρη η θέση.
8. **Εκκρεμείς εντολές** (`GET /v5/earn/order`) — **ασύμμετρα**:
   - **STAKE** (fail closed): status εκτός `success`/`fail` (και άγνωστο) =
     εκκρεμής → καμία STAKE σε αυτό το νόμισμα. Εκκρεμής που δεν
     αντιστοιχίζεται σε νόμισμα του whitelist, ή Stake χωρίς αναγνώσιμο
     `productId`/`orderValue` → **καμία STAKE** (`PENDING_ORDER_UNMATCHED`).
   - **REDEEM** (οι έξοδοι δεν περιμένουν ποτέ την αβεβαιότητα): μπλοκάρεται
     **μόνο** από γνωστή εκκρεμή Redeem (`orderType` redeem, `status`
     pending) στο **ίδιο** `productId`. Άγνωστο status ή μη αντιστοιχισμένη
     εντολή δεν μπλοκάρει ποτέ έξοδο — στη χειρότερη περίπτωση η Bybit
     απορρίπτει μια διπλή εξαργύρωση.
9. **Εκτέλεση** — μόνο `POST /v5/earn/place-order` (`category, orderType,
   accountType, amount, coin, productId, orderLinkId`). `orderLinkId =
   <cycle_id>-S|R-<productId>`. Το dry-run `would_call` είναι ακριβώς το
   request του live.
10. **Record** στο `LOG_DIR/YYYY-MM-DD.jsonl`. Οποιαδήποτε απρόβλεπτη
    εξαίρεση → `CYCLE_CRASH` με traceback, exit 4.
11. **Μετά:** Telegram (dedup), διαγραφή raw session files > 7 ημερών.

## 3. Κωδικοί

| Κωδικός | Εμποδίζει το heartbeat; |
|---|---|
| `CONFIG_INCOMPLETE`, `CRITICAL`, `AGENT_PARSE_ERROR`, `DECISION_VALIDATION_FAILED`, `CYCLE_CRASH` | **ναι** (`heartbeat.BLOCKING_CODES`) |
| `AGENT_TIMEOUT`, `DATA_UNAVAILABLE`, `NO_ELIGIBLE_PRODUCTS`, `RISK_STATE_*`, `RISK_GATE_DROPPED_STAKE`, `DATA_GATE_DROPPED_STAKE`, `PENDING_ORDERS`, `PENDING_ORDER_UNMATCHED`, `STALE_SCAN`, `CYCLE_LATENCY_HIGH` | όχι |

Κάθε αλλαγή εδώ γίνεται στο ίδιο commit με το `BLOCKING_CODES` και το
`test_blocking_codes_are_exact`.

## 4. Heartbeat

| Κατάσταση | Αποτέλεσμα |
|---|---|
| Bybit API μη προσβάσιμο | ABSTAIN |
| Τελευταίος κύκλος με εμποδιστικό κωδικό | ABSTAIN + alert |
| `LOG_DIR` δεν υπάρχει | exit 3 + alert (ποτέ bootstrap) |
| Αρχείο απόν | γράφει `NO_NEW_POSITIONS`, `source: heartbeat_bootstrap` |
| Μη αναγνώσιμο / κακοσχηματισμένο / λάθος HMAC | ABSTAIN + alert, **ποτέ αντικατάσταση** |
| Bootstrap + ο τελευταίος κύκλος καθαρός, επαλήθευσε **αυτή** την εγγραφή (ίδιο `ts`) και έτρεξε μετά | γράφει `NORMAL`, `source: heartbeat_renew` |
| `NORMAL` φρέσκο | τίποτα |
| `NORMAL` παλιό + scanner ζωντανός (κύκλος στα τελευταία 30') | ανανεώνει |
| `NORMAL` παλιό + scanner νεκρός | ABSTAIN + alert |
| Οτιδήποτε άλλο (`source: operator`, κάθε `UNWIND`) | ABSTAIN (+ alert αν παλιό) |

**ABSTAIN = καμία εγγραφή.** Το heartbeat δεν αγγίζει ποτέ κατάσταση operator.

## 5. Risk state και operator

Εγγραφή: `{profile, state, ts, reason, source, sig}`, όλα υπογεγραμμένα.
Εγγραφές χωρίς `source` (παλιό σχήμα) είναι `RISK_STATE_MALFORMED`.

Χειροκίνητα (γράφει `source: operator`):

```bash
sudo -u hermes /opt/hermes/venvs/yield_rotation/bin/python /opt/hermes/yield_rotation/risk_state.py write UNWIND "λόγος"
sudo -u hermes /opt/hermes/venvs/yield_rotation/bin/python /opt/hermes/yield_rotation/risk_state.py verify
```

Ή από Telegram (`yield-telegram-bot.service`, δικό του token
`YIELD_TELEGRAM_BOT_TOKEN`): `/unwind` ή `/resume` → `/confirm <κωδικός>` σε
2 λεπτά. Δεκτά μόνο μηνύματα με `chat.id` **και** `from.id` =
`ALERT_TELEGRAM_CHAT_ID`.

## 6. Κλειδωμένες αποφάσεις στρατηγικής — δεν αλλάζουν χωρίς ρητή εντολή του Giannis

Calibration 180 ημερών, USDT: p25 0,70% · median 1,23% · p75 1,62% · max 2,89%.

| Παράμετρος | Τιμή | Γιατί |
|---|---|---|
| `ENTRY_APR` | 0.001 | Οτιδήποτε θετικό κερδίζει το αδρανές υπόλοιπο |
| `EXIT_APR` | 0 | Ένα προϊόν USDT: η εξαργύρωση λόγω πτώσης επιτοκίου στέλνει τα χρήματα στο 0% |
| `MIN_APR_EDGE` | 0.009 | p75 − p25· αφορά μόνο δεύτερο προϊόν |
| `MAX_REDEMPTION_ETA_HOURS` | 2 | Όχι 0 — το 0 ενεργοποιεί έξοδο με την παραμικρή καθυστέρηση |
| `MAX_SCAN_AGE_SECONDS` | 900 | Ηλικία του ζωντανού scan |
| `MAX_APR_HISTORY_GAP_HOURS` | 4 | Ηλικία του APR history (ωριαία ανανέωση) |
| `COIN_WHITELIST` | `[USDT]` | Ποτέ μεταφορά μεταξύ διαφορετικών νομισμάτων |
| `RESOLVED_MODEL` | `google/gemini-2.5-flash` | Στέλνεται αυτούσιο στο CLI, όχι μέσω alias |
| `ACCOUNT_TYPE` | `UNIFIED` | — |
| `DRY_RUN` | `true` | Αλλάζει μόνο από τον Giannis, στη Φάση 6 |

Αποφάσεις σχεδίασης (§2 του `FINISH_PLAN.md`): bootstrap Α1-Β· το ποσό το
υπολογίζει ο wrapper (Α2)· ROTATE αφαιρέθηκε (Α3)· repo private (Α4 — ρύθμιση
στο GitHub, εκκρεμεί από τον Giannis).

## 7. Κανόνες για κάθε αλλαγή

1. Πρώτα test που αποτυγχάνει, μετά η διόρθωση. Τα tests ακολουθούν το
   spec· ποτέ αλλαγή test για να περάσει.
2. Καμία κλήση δικτύου στα tests· κανένα κλειδί, κανένα `.env` στο repo.
3. Ποτέ `DRY_RUN: false` σε default ή σε αρχείο.
4. Μία υλοποίηση HMAC (`signing.py`), ένας αποστολέας Telegram (`notify.py`),
   μία εντολή agent (`build_agent_command`).
5. Άγνωστο = `null`· αποτυχία ανάγνωσης = fail closed, ποτέ σιωπηλή.

## 8. Tests

```bash
pip install -r requirements-dev.txt
pytest                                   # οπουδήποτε· CI σε κάθε push (και job vps-layout:
                                         # repo στο /opt, χρήστης hermes, systemd-analyze verify)
/opt/hermes/venvs/yield_rotation/bin/python tests/run_regression.py --runs 5   # μόνο VPS, πραγματικό μοντέλο
```

`tests/conftest.py`: όλα τα paths σε `tmp_path`, κλειδιά σβησμένα,
**κάθε σύνδεση δικτύου απαγορεύεται**.

## 8α. Δεύτερη στρατηγική: funding carry (`CARRY_PLAN.md`)

Ουδέτερη θέση (long spot + short perp) πάνω στο ίδιο πλαίσιο ασφαλείας,
**χωρίς LLM**. Κατάσταση:

- ⛔ **Πύλη 0Α (Giannis): εκκρεμεί.** Πρόσβαση σε USDT perpetuals από
  λογαριασμό ΕΟΧ μετά το MiCA, Easy Earn/BYUSDT, subaccount. Αν αποτύχει,
  το σχέδιο σταματά — καμία παράκαμψη.
- ✅ **Φάση 0Β — κώδικας:** `carry/decide.py` (καθαρή απόφαση της §4, η ίδια
  που θα καλεί η παραγωγή), `carry/client.py` (μόνο δημόσια endpoints,
  fail-closed), `carry/backtest.py`, `tools/carry_calibrate.py`.
- ⏳ **Φάση 0Β — μέτρηση:** θέλει μία εκτέλεση από μέρος με πρόσβαση στη
  Bybit (το περιβάλλον ανάπτυξης μπλοκάρεται γεωγραφικά από το CloudFront
  της Bybit). Δημόσια δεδομένα, χωρίς κλειδί:
  `…/python tools/carry_calibrate.py --out reports/carry` → το
  `CARRY_CALIBRATION.md` και το `carry_data_<utc>.json` γίνονται commit.
- **Κανένας κώδικας συναλλαγών** πριν περάσουν και οι δύο πύλες.

## 9. Ανοιχτά — τι μένει

- **Φάση 6** (`DEPLOY.md`): deploy, νέο HMAC, LLM regression του v6,
  testnet, επταήμερο dry-run, νέο κλειδί Bybit, `DRY_RUN: false` μόνο από τον
  Giannis.
- **Ανεπιβεβαίωτα πεδία Bybit** — δεν υπάρχει ακόμα πραγματική καταγραφή
  (μόνο `tests/data/SYNTHETIC_usdt_flexible.json`): `precision`,
  `maxStakeAmount`, `redeemProcessingMinute` στο `/v5/earn/product`· το
  `result.list` του `/v5/earn/position`· τα πεδία του `/v5/earn/order`
  (`status`, `orderType`, `coin`, `orderValue`) και του
  `/v5/earn/apr-history` (`timestamp`, `apr`). Κάθε απόκλιση αποτυγχάνει
  ασφαλώς (καμία STAKE ή καμία εντολή, με λόγο στο record). Κλείνουν στο
  testnet με `scripts/testnet.py capture` → `tests/data/` →
  `tests/test_recorded_payloads.py`.
- **Prompt v6 δεν έχει δοκιμαστεί με το πραγματικό μοντέλο** (βήμα του
  `DEPLOY.md`).
