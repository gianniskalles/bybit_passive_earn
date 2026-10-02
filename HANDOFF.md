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

- ✅ **Πύλη 0Α (Giannis): επιβεβαιώθηκε στις 26/9/2026** — πρόσβαση σε USDT
  perpetuals από τον λογαριασμό, Easy Earn, subaccount.
- ✅ **Φάση 0Β — κώδικας:** `carry/decide.py` (καθαρή απόφαση της §4, η ίδια
  που θα καλεί η παραγωγή), `carry/client.py` (μόνο δημόσια endpoints,
  fail-closed), `carry/backtest.py`, `tools/carry_calibrate.py`.
- ✅ **Φάση 0Β — μέτρηση (VPS, 27/9/2026): NO-GO** — 0/324 συνδυασμοί,
  καμία είσοδος σε 180 ημέρες (`calibration/CARRY_CALIBRATION.md`). Το
  ιστορικό APR του layer A καλύπτει μόνο ~7 ημέρες· το report το δηλώνει.
- **Απόφαση Giannis (CARRY_PLAN §13):** παρά το NO-GO χτίζονται οι Φάσεις
  1–6· ο κανόνας εισόδου είναι η πύλη. Το carry **αντικαθιστά** το yield
  rotation (layer A = USDT στο Flexible Easy Earn, το διαχειρίζεται το carry)·
  στο deploy του carry σβήνουν οι timers του yield rotation — ποτέ δύο
  συστήματα στον ίδιο λογαριασμό. Είσοδος: redeem → ολοκλήρωση → perp + spot
  (εγκατάλειψη μετά από `REDEEM_TIMEOUT_HOURS`). Έξοδος: δύο σκέλη → USDT
  πίσω στο Earn. Μηνιαία επανάληψη της μέτρησης με αναφορά στο Telegram.
- ✅ **Φάση 1 — config & risk state:**
  - `carry/config.py`: αυστηρό σχήμα για **κάθε** κλειδί του
    `config/carry.yaml` (κενό = σφάλμα, ο κύκλος δεν ξεκινά), σχέσεις
    (hysteresis, MMR_WARN < REDUCE < EMERGENCY, notional ≤ cap) και κατώφλια
    παραγωγής που μόνο ένα `TESTNET_ONLY: true` config κατεβαίνει — και αυτό
    απορρίπτεται χωρίς `BYBIT_TESTNET`.
  - `config/carry.yaml`: κατώφλια της απόφασης §13.8 (MIN_HOLD 336 h,
    smoothing 9, horizon 168 h, 5% APR). 0,01%/8h μπαίνει· στα πραγματικά
    180 ημέρες γίνεται **μία** είσοδος ανά σύμβολο (τέλη Αυγούστου) — η §13.8
    περίμενε καμία· απόφαση 13.12: σωστή συμπεριφορά, κλειδωμένη στο test. Κεφάλαιο §13.11: cap 100, buffer 15, 80 ανά σύμβολο, `SYMBOLS:
    [ETHUSDT]`, altcoin 30. Basis/spread από τη μέτρηση 24 ωρών (30/9):
    `MAX_ENTRY_BASIS_BPS: 6`, `MAX_SPREAD_BPS: 1`, και
    `MAX_FAVORABLE_BASIS_BPS: 100` (13.13). **Null:** `DEADMAN_URL` πριν το live.
  - `config/carry.testnet.yaml`: χαμηλωμένα κατώφλια για τον αναγκαστικό
    κύκλο του testnet (έξοδος με operator UNWIND).
  - `carry/state.py`: δικό του υπογεγραμμένο risk state (profile
    `hermes-carry`, `carry_risk_state.json`), ίδια λογική staleness, πίνακας
    επιτρεπόμενων ενεργειών (ό,τι μειώνει ρίσκο επιτρέπεται πάντα).
  - `heartbeat.py --system carry`, `risk_state.py --system carry verify|write`.
- ✅ **Φάση 2 — client & snapshot:**
  - `carry/client.py`: `CarryClient` — tickers, orderbook, πρόσφατο funding,
    θέσεις, account info, wallet, fee-rate, collateral-info, ανοιχτές εντολές,
    `find_order` (R23: αναζήτηση με `orderLinkId` πριν από επανάληψη),
    executions, transaction log. Σελιδοποίηση που δεν τελειώνει = σφάλμα.
    `is_region_restricted` (R1): retCode 10024 ή «from your country» —
    **ανεπιβεβαίωτο**, μόνο συντηρητικό.
  - `carry/snapshot.py`: ένα αμετάβλητο στιγμιότυπο ανά κύκλο· κάθε ενότητα
    (`market:<SYM>`, `positions:<SYM>`, `account`, `orders`, `earn`)
    διαβάζεται ολόκληρη ή λείπει με τον λόγο· κανένα default. Φρεσκάδα (R27),
    ξένες εντολές (R2), κατάσταση εντολών Earn (μόνο `Success` = ολοκληρωμένο
    redeem, απόφαση 13.4).
  - Tests κλειδωμένα στα captures του `tests/data/carry/` (σήμερα μόνο
    `SYNTHETIC_snapshot.json`). Πραγματικά: `scripts/testnet.py
    carry-capture` — **μόνο με έγκριση Giannis**.
  - `tools/carry_market_sample.py`: `MAX_ENTRY_BASIS_BPS` = ceil(p95 |basis|),
    `MAX_SPREAD_BPS` = ceil(p99 spread), από ζωντανά δημόσια δεδομένα. Το
    περιβάλλον ανάπτυξης μπλοκάρεται γεωγραφικά· τρέχει στο VPS (24 ώρες):
    `sudo -u hermes nohup /opt/hermes/venvs/yield_rotation/bin/python
    /opt/hermes/yield_rotation/tools/carry_market_sample.py --minutes 1440
    --out /opt/hermes/yield_rotation/calibration &` → commit του
    `CARRY_MARKET_SAMPLE.md` και του `carry_market_<utc>.json`, και οι δύο
    τιμές μπαίνουν στο `config/carry.yaml`. **Έγινε 30/9** (6 και 1 bps).
    Η αναφορά δίνει και το basis με πρόσημο: στις 24 ώρες ήταν **αρνητικό
    στο 100%** των δειγμάτων (perp κάτω από spot, −2 έως −8,5 bps).
  - Ο έλεγχος basis είναι μονόπλευρος (απόφαση 13.13): απόρριψη μόνο όταν
    basis < −`MAX_ENTRY_BASIS_BPS` (perp κάτω από spot) ή basis >
    `MAX_FAVORABLE_BASIS_BPS` (100, χαλασμένα δεδομένα). Με τα 6 bps θα
    απορρίπτονταν 1,9% (BTC) / 3,2% (ETH) των λεπτών της μέτρησης.
- `carry/preflight.py`: ελάχιστη θέση ανά σύμβολο από το instruments-info
  (perp minOrderQty/qtyStep/minNotionalValue, spot basePrecision/minOrderQty/
  minOrderAmt, χρέωση spot στο νόμισμα)· όσα δεν χωράνε στο όριό τους
  εξαιρούνται με `SYMBOL_BELOW_MIN_SIZE`. Με τα συνθετικά: ETH ≈ 25 USD, BTC
  ≈ 65 USD.
- ✅ **Altcoins (§13.10) — μέτρηση 29/9 (Hermes, VPS): κανένα GO.**
  `calibration/CARRY_CALIBRATION_ALTS.md`: 10 υποψήφια από το top 15 (SOL, XRP,
  NEAR, QNT, HYPE, HBAR, LINK, SUI, ONDO, DOGE)· εξαιρέθηκαν ZEC, CL, PUMPFUN,
  XAU (χωρίς spot) και SOXL (< 6 μήνες). Όλα NO-GO: υπεροχή από −2,65% έως
  +0,43%, κανένα δεν αντέχει έξοδα ×1,5. Το δημόσιο
  `/v5/spot-margin-trade/collateral` διαβάστηκε σωστά (το σχήμα επιβεβαιώθηκε).
  Η μέτρηση έτρεξε πριν από το 180ήμερο ιστορικό του layer A· στη μηνιαία
  επανάληψη θα έχει πλήρες layer A. Το config απορρίπτει κάθε altcoin στο
  `SYMBOLS`. Ξανά στο VPS: `sudo -u hermes
  /opt/hermes/venvs/yield_rotation/bin/python
  /opt/hermes/yield_rotation/tools/carry_alt_calibrate.py --out
  /opt/hermes/yield_rotation/calibration`.
- ✅ **Φάση 3 — σχέδιο κύκλου** (`carry/plan.py`, καθαρό): snapshot + config +
  risk state + book → ενέργειες, alerts, αλλαγές book.
  - Είσοδος (13.4): αν λείπουν USDT στο UTA, μόνο `EARN_REDEEM_FOR_ENTRY`· τα
    σκέλη (perp, spot) σε επόμενο κύκλο, μόνο με redeem `Success`. Αποτυχία,
    `REDEEM_TIMEOUT_HOURS` (`EARN_REDEEM_STUCK`) ή χαμένες συνθήκες → εγκατάλειψη.
  - Έξοδος (13.5): spot, μετά perp reduceOnly (margin emergency: perp πρώτα)·
    τα αδρανή USDT πίσω στο Earn στον επόμενο κύκλο, ποτέ το buffer με ανοιχτή
    θέση, ποτέ άλλο νόμισμα.
  - Κίνδυνος: ορφανά σκέλη (R14), ADL (R13), drift → εξισορρόπηση προς
    ουδετερότητα, ADL rank/MMR → TRIM, MMR emergency/UNWIND/όχι Trading/όχι
    collateral → έξοδος· margin mode, δανεισμός USDT, ξένες εντολές, περιοχή,
    CVR → καμία είσοδος. Νέοι κωδικοί που μπλοκάρουν το heartbeat:
    `UNTRACKED_POSITION`, `USDT_BORROW_LIMIT`.
  - Property tests (hypothesis): καμία αύξηση έκθεσης εκτός NORMAL, ποτέ πάνω
    από τα όρια, UNWIND πάντα βγαίνει, καμία κίνηση funding μέσα στο 15λεπτο,
    ποτέ spot χωρίς ολοκληρωμένο redeem, στο Earn μόνο USDT.
  - **Εγκρίθηκε 1/10, με διόρθωση (13.14):** το σκέλος spot μετριέται από το
    `spot_qty` του book, όχι από ολόκληρο το wallet. Υπόλοιπο πέρα από book +
    dust → `FOREIGN_BALANCE`, καμία είσοδος, ποτέ συναλλαγή σε αυτό. Ένα short
    εκτός book υιοθετείται χωρίς spot, άρα κλείνει ως orphan.
- ✅ **Φάση 4 — εκτέλεση** (`carry/execute.py`, `order_request` στο
  `carry/client.py`):
  - Είσοδος: perp Sell Limit IOC στην τιμή που καλύπτει την ποσότητα στο
    orderbook της στιγμής (μη αναγνώσιμο → καμία εντολή) → spot Buy Limit IOC
    για την ποσότητα που γέμισε, με την χρέωση στο νόμισμα (R22). Επανάληψη
    με νέο book όσο το IOC γεμίζει λιγότερο, ως `LEG_TIMEOUT_S`· reject → τέλος.
    Συμφιλίωση (R20): το perp κόβεται στο spot που ήρθε (reduceOnly Market).
    Χωρίς spot → όλο το perp κλείνει, `ORPHAN_LEG`, escalate `NO_NEW_POSITIONS`.
  - Έξοδοι (13.14): perp reduceOnly Market (χωρίς τιμή). Spot: Limit IOC στην
    τιμή από το orderbook της στιγμής· μη αναγνώσιμο, ή υπόλοιπο στο
    `LEG_TIMEOUT_S` → Market με alert `SPOT_MARKET_SELL`. Πουλιέται μόνο η
    ποσότητα του book.
  - R23/R24: ντετερμινιστικό `orderLinkId` (κύκλος, ενέργεια, σκέλος,
    προσπάθεια). Αποστολή χωρίς απάντηση → αναζήτηση με `orderLinkId`· αν δεν
    υπάρχει, ξανά με το **ίδιο** link. Spot με άγνωστη έκβαση στο deadline →
    `pending_spot` στο book, `resolve_pending()` στον επόμενο κύκλο· ως τότε
    καμία είσοδος στο σύμβολο. Άγνωστο perp → κλείσιμο reduceOnly (καλύπτει
    και τις δύο περιπτώσεις).
  - Earn: Redeem/Stake μόνο USDT, έλεγχος με `orderLinkId` πριν την αποστολή.
  - DRY_RUN: το `execute_plan` αρνείται ζωντανό exchange και το `LiveExchange`
    αρνείται ξανά κάθε εγγραφή.
  - Tests: τα 4 της §9 (orphan, partial fill, χρέωση στο νόμισμα, timeout →
    αναζήτηση) και property test (hypothesis) με τυχαία σφάλματα σε κάθε
    κλήση: ποτέ short πάνω από το spot, το book ταυτίζεται με το exchange,
    όλα μέσα στα timeouts· στις εξόδους τα ξένα νομίσματα μένουν ανέγγιχτα.
  - **Ανεπιβεβαίωτα (testnet, §12):** `cumFeeDetail` στην απάντηση spot
    (χωρίς αυτό η χρέωση θεωρείται στο νόμισμα, alert
    `FEE_CURRENCY_ASSUMED`)· retCode 110017 (reduceOnly με μηδενική θέση)·
    110072 (διπλό `orderLinkId`).
  - **Εγκρίθηκε 2/10 (13.15).** Διόρθωση: χαμένο book → `BOOK_MISMATCH`,
    καμία εντολή (εκτός από reduceOnly του short πέρα από το spot του wallet),
    `NO_NEW_POSITIONS`· ανάκτηση με Telegram `/adopt carry` (υπογεγραμμένη αίτηση
    μίας χρήσης, `carry/adopt.py`, `YIELD_CARRY_ADOPT_FILE`), που περνά στο book
    το min(short, spot) χωρίς trade. Κάθε εντολή καταγράφει `ref_price` και
    `slippage_bps`.
- ✅ **Φάση 5 — κίνδυνος, ledger, paper, κύκλος:**
  - `run_carry_cycle.py`: ο κύκλος (config → carry risk state → hold →
    book → exchange → snapshot → plan → execute → ledger → R36 → εγγραφή
    στο `LOG_DIR/<date>.jsonl` για το heartbeat → Telegram → dead-man ping).
    Εγγραφή σε κάθε περίπτωση· `CYCLE_CRASH`/`CONFIG_INCOMPLETE` blocking.
    Κλείδωμα αρχείου ενάντια σε ταυτόχρονους κύκλους.
  - `carry/paper.py`: σε DRY_RUN πάντα paper λογαριασμός — πραγματικά
    δεδομένα αγοράς, fills στο top του orderbook με τις χρεώσεις (spot buy
    στο νόμισμα), funding των settlements που πέρασαν, τόκοι Earn. Ξεκινά με
    όλο το `TOTAL_CAPITAL_CAP_USD` στο Earn (13.3).
  - `carry/book.py`: υπογεγραμμένο book· χαμένο ή αλλοιωμένο → άδειο +
    CRITICAL → `BOOK_MISMATCH` (13.15).
  - `carry/ledger.py`: JSONL ανά ημέρα (`LOG_DIR/ledger/`): εντολές (fill,
    τιμή αναφοράς, slippage, χρέωση), funding, αναμενόμενο funding, τόκοι,
    round trips (basis/execution PnL).
  - `carry/risk.py`: R36 (14 ημέρες funding κάτω από
    `UNDERPERFORMANCE_RATIO` × αναμενόμενο), R33 dead-man ping, και το latch
    `CARRY_HOLD` (blocking code): orphan protection, `BOOK_MISMATCH` ή R36
    κρατούν `NO_NEW_POSITIONS` ως ότου ο operator γράψει το carry risk state.
  - Replay tests (13.6) σε συνθετικά καθεστώτα με τον πραγματικό κύκλο:
    ανοδικό (είσοδος, παραμονή, funding, έξοδος στο αρνητικό, USDT πίσω στο
    Earn), ουδέτερο (καμία συναλλαγή), ανοδικό με τιμή +20% (ουδέτερο ως
    προς την τιμή), ασταθές funding (≤ 2 είσοδοι ανά 30 ημέρες), ADL και
    ρευστοποίηση (το ορφανό κλείνει στον ίδιο κύκλο), χαμένο book → adopt
    → release.
  - **Δύο σφάλματα που βρήκε το replay, διορθωμένα:** (1) το slack της
    τιμής ζητούνταν και στην είσοδο, οπότε άνοδος τιμής μεταξύ redeem και
    εισόδου έριχνε την είσοδο και γύριζε τα USDT στο Earn — επ' αόριστον·
    (2) η τιμή limit στρογγυλοποιούνταν στο κοντινότερο tick, όχι μακριά από
    το βιβλίο (αγορά πάνω, πώληση κάτω).
  - **Ανεπιβεβαίωτο (§12):** στο live, το funding διαβάζεται από το
    transaction log (`SETTLEMENT`, πεδίο `change`).
- ✅ **Φάση 5 εγκρίθηκε 2/10.**
- ✅ **Φάση 6 — λειτουργία** (`DEPLOY.md`, ενότητα «Carry»):
  - Units: `yield-carry-cycle` (5'), `yield-carry-heartbeat` (5', offset 3'),
    `yield-carry-summary` (06:50 UTC), `yield-carry-calibrate` (1η του μήνα).
  - `deploy.sh --system carry|yield` (χωρίς: κρατά το ενεργό σύστημα):
    **ποτέ και τα δύο**. Το carry σβήνει πρώτα τους timers του yield rotation.
    Η επιστροφή στο yield αρνείται αν το carry κρατά οτιδήποτε
    (`preflight carry-exposure`: exchange, paper, book· μη αναγνώσιμο =
    κρατά). Ούτε νέο κλειδί HMAC ούτε μετακίνηση του book όσο το carry κρατά
    οτιδήποτε (2/10). Το βήμα 7 αποτυγχάνει αν μείνει ενεργός timer του άλλου.
  - Δικό του κλειδί `BYBIT_CARRY_API_KEY/SECRET` (subaccount)· ο
    `CarryClient` δεν πέφτει ποτέ πίσω στο `BYBIT_API_KEY` του yield rotation
    (το έκανε σιωπηλά πριν).
  - Telegram: `/unwind carry`, `/resume carry` (λύνει και το `CARRY_HOLD`),
    `/unwind all`, `/status` και για τα δύο. Η ειδοποίηση του carry πάει στο
    chat του operator (το `carry.yaml` δεν έχει δικό του — πριν, δεν έστελνε).
  - Ημερήσια σύνοψη (`summary.py --system carry`): μία γραμμή ανά σύμβολο με το
    εξομαλυμένο funding έναντι του κατωφλιού (`ETH: 2,6% — χρειάζεται 6,7% (5%
    πάνω από το Earn 1,7%)`), book, ledger 24 ωρών, paper λογαριασμός, κράτημα.
  - Μηνιαία μέτρηση (13.7, `tools/carry_monthly.py`): βαθμονόμηση της Φάσης 0Β
    + τα κατώφλια του config στα ίδια δεδομένα → Telegram. Δεν αλλάζει ποτέ το
    config.
  - Διορθώσεις: το record του carry γράφει `risk_state_meta` (χωρίς αυτό το
    heartbeat δεν θα προωθούσε ποτέ το bootstrap σε `NORMAL`)· `DEADMAN_URL`
    υποχρεωτικό μόνο σε live config (αλλιώς το paper δεν ξεκινούσε)·
    `RISK_STATE_*` χωρίς διπλό πρόθεμα.
  - Λίστα για τον Giannis στο Bybit UI: `DEPLOY.md`, «Carry — τι κάνει ο
    Giannis στο Bybit UI».
  - **Εγκρίθηκε 2/10· merge στο `main`.** Πριν το testnet: με `BYBIT_TESTNET`
    όλα τα αρχεία state του carry στο `state/carry-testnet/` και config το
    `carry.testnet.yaml` (δικό του `LOG_DIR`)· testnet με config που δεν είναι
    `TESTNET_ONLY` → `CONFIG_INCOMPLETE`.
  - Testnet μόνο με έγκριση του Giannis.

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
