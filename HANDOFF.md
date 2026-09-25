# HANDOFF.md — v5.3

> **Αυτό το αρχείο είναι το σημείο εκκίνησης κάθε νέας συνεδρίας.**
> Αν δεν υπάρχει προηγούμενο context, **ξεκίνα ΟΠΩΣΔΗΠΟΤΕ διαβάζοντας
> αυτό το αρχείο** πριν κάνεις οτιδήποτε άλλο.

**Project:** Bybit Earn yield-rotation agent (Hermes)
**Repo:** `/opt/hermes/yield_rotation/`
**Τελευταίο commit:** v5.3 heartbeat rewrite + shared signing + tests

---

## 1. Τι είναι αυτό το σύστημα

Ένα αυτόνομο πρόγραμμα που ανιχνεύει ευκαιρίες yield στο Bybit Earn
(USDT) και — όταν ο χρήστης (Giannis) δώσει ρητό OK — εκτελεί stakes.
Αποτελείται από:

| Αρχείο | Ρόλος |
|---|---|
| `run_yield_cycle.py` | Ο **wrapper/verifier**: φορτώνει config, τραβάει snapshot, φιλτράρει προϊόντα, καλεί LLM agent, αξιολογεί αποφάσεις, εκτελεί. |
| `heartbeat.py` | **Καρδιακός παλμός**. Ανανεώνει τη risk_state ώστε ο επόμενος κύκλος να μη χρειάζεται `forced:true`. Λειτουργεί ξεχωριστά μέσω systemd timer. |
| `settings.py` | **Όλα τα paths** (env var με default για το VPS) και το ενιαίο `load_env`. |
| `signing.py` | **ΜΟΝΑΔΙΚΗ πηγή αλήθειας** για canonical JSON + HMAC-SHA256. Το εισάγουν ΚΑΙ το heartbeat.py ΚΑΙ το risk_state.py. |
| `risk_state.py` | Διαβάζει/γράφει/επαληθεύει τη risk_state (δομημένο αποτέλεσμα). CLI: `verify`, `write` (operator). |
| `executor.py` | Dry-run vs live execution wrapper. |
| `bybit_earn_tool.py` | Bybit Earn API client (CLI). |
| `config/yield_rotation.yaml` | Όλες οι παράμετροι στρατηγικής. |
| `tests/test_heartbeat.py` | Tests του heartbeat (decision table, bootstrap, ανθεκτικότητα). |
| `tests/test_cycle.py` | Tests του wrapper χωρίς LLM (πύλες, ποσά, REDEEM, prompt, JSON, εντολή agent). |
| `tests/test_risk_state.py` | Tests του `risk_state.py`. |
| `tests/test_data_integrity.py` | Tests της Φάσης 3 (πεδία, APR, κωδικοί, config, crash). |
| `tests/run_regression.py` | LLM regression (μόνο VPS) πάνω στο `run_cycle` της παραγωγής. |
| `tests/regression_fixtures.py`, `tests/replay.py`, `tests/data/` | Σενάρια regression, replay αποθηκευμένων απαντήσεων Bybit. |
| `tests/test_regression_harness.py`, `tests/test_recorded_payloads.py` | Ότι το regression είναι η διαδρομή της παραγωγής· ότι οι καταγραφές αναλύονται σωστά. |
| `scripts/testnet.py` | `capture` / `roundtrip` για το testnet (Φάση 6). |
| `tests/test_bybit_tool.py` | Tests του Bybit client (place-order, υπογραφή, σφάλματα, testnet). |
| `tests/test_portability.py` | Tests φορητότητας και προτεραιότητας `.env` (Φάση 0). |

## 2. ΓΙΑΤΙ ΥΠΑΡΧΕΙ ΤΟ heartbeat.py (το πρόβλημα που λύνει)

Το πρόβλημα: όταν η `risk_state` είναι **stale** (π.χ. ο κύκλος κράτησε
πολύ ώρα, ή ο προηγούμενος κύκλος έφυγε με `forced:true`), ο wrapper
αναγκάζει το επόμενο run σε `NO_NEW_POSITIONS`/`forced:true`. Αν αυτό
μείνει, ο agent **κολλάει** — δεν μπορεί ποτέ να ανοίξει νέες θέσεις.

Το heartbeat σπάει αυτό το αδιέξοδο: τρέχει συχνά (ανεξάρτητα από τον
wrapper), βρίσκει τη risk_state «stale NORMAL», την ανανεώνει με νέο
timestamp, και **ο επόμενος κύκλος τρέχει κανονικά χωρίς `forced:true`**.

## 3. Τρεις κανόνες ασφαλείας του heartbeat (v5.3 — ΜΗΝ τους σπάσεις)

1. **LOG_DIR διαβάζεται ΑΠΟ ΤΟ CONFIG, όχι να μαντεύεται.**
   Διαβάζεται από `config/yield_rotation.yaml` (το ίδιο που διαβάζει ο
   wrapper). Αν ο φάκελος **δεν υπάρχει** → αποτυχία (exit 3) + alert,
   **ΔΕΝ** αντιμετωπίζεται σιωπηλά ως bootstrap.
   - «**Δεν υπάρχει φάκελος**» (MISCONFIG → error/exit 3) **πρέπει να
     ξεχωρίζει ρητά** από «**καμία εκτέλεση ακόμα**» (bootstrap → NORMAL).

2. **ΜΙΑ υλοποίηση υπογραφής.** Το canonical JSON + HMAC βρίσκονται
   αποκλειστικά στο `signing.py`. Το εισάγουν και το `heartbeat.py` και
   το `risk_state.py`. Αν οι δύο σειριοποιήσεις αποκλίσουν, το λάθος
   εμφανίζεται ως `RISK_STATE_BAD_SIGNATURE` (υποψία παραποίησης) — όχι
   ως ασυμφωνία κώδικα. **Μην δημιουργήσεις δεύτερη υλοποίηση HMAC.**

3. **Ο heartbeat δεν αγγίζει ποτέ κατάσταση που δεν έγραψε ο ίδιος για
   bootstrap.** Προάγει σε `NORMAL` **μόνο** `NO_NEW_POSITIONS` με
   `source: heartbeat_bootstrap`, και μόνο αφού ο τελευταίος κύκλος είναι
   καθαρός, επαλήθευσε **αυτή ακριβώς** την εγγραφή (ίδιο `ts`) και έτρεξε
   μετά από αυτή. Καταστάσεις `source: operator` και κάθε `UNWIND` δεν
   ξαναγράφονται ποτέ. **ABSTAIN = καμία εγγραφή.**

## 4. Decision table του heartbeat (Φάση 1)

| Κατάσταση | Αποτέλεσμα |
|---|---|
| Bybit API μη προσβάσιμο | ABSTAIN |
| Τελευταίος κύκλος με εμποδιστικό κωδικό (`BLOCKING_CODES`) | ABSTAIN + alert |
| Φάκελος LOG_DIR δεν υπάρχει | **ERROR exit 3** + alert |
| Αρχείο απόν | Γράφει `NO_NEW_POSITIONS`, `source: heartbeat_bootstrap` |
| Μη αναγνώσιμο / κακοσχηματισμένο / λάθος HMAC | ABSTAIN + alert, ποτέ αντικατάσταση |
| Bootstrap `NO_NEW_POSITIONS` + επαληθευμένος καθαρός κύκλος μετά το `ts` | Γράφει `NORMAL`, `source: heartbeat_renew` |
| `NORMAL` + φρέσκο | Τίποτα |
| `NORMAL` + παλιό + scanner ζωντανός | Ανανεώνει `NORMAL` |
| `NORMAL` + παλιό + scanner νεκρός | ABSTAIN + alert |
| Οτιδήποτε άλλο (operator, `UNWIND`, bootstrap χωρίς κύκλο) | ABSTAIN (+ alert αν παλιό) |

`BLOCKING_CODES` = `CONFIG_INCOMPLETE`, `CRITICAL`, `AGENT_PARSE_ERROR`,
`DECISION_VALIDATION_FAILED`, `CYCLE_CRASH`. Το `CYCLE_MODEL_MISMATCH` καταργήθηκε (T1.9).

### Risk state στον wrapper

`risk_state.py` (ρίζα repo) — εγγραφή `profile, state, ts, reason, source,
sig`. Το `verify()` επιστρέφει `Verification(code, signature_valid, fresh,
state, source, ts, ...)`. Εγγραφές χωρίς `source` (παλιό σχήμα) είναι
`RISK_STATE_MALFORMED` — στο deploy ξεκινάμε από καινούργιο αρχείο.

| Επαληθευμένη εγγραφή | Ενεργή κατάσταση |
|---|---|
| έγκυρη + φρέσκια | ό,τι είναι υπογεγραμμένο |
| έγκυρη, παλιά (ή `ts` στο μέλλον) | NORMAL→NO_NEW_POSITIONS, NO_NEW_POSITIONS→ίδιο, UNWIND→UNWIND |
| απούσα / άκυρη / κακοσχηματισμένη / χωρίς κλειδί | NO_NEW_POSITIONS + alert |

Ο κύκλος **δεν τερματίζει** ποτέ λόγω risk state. Πύλες (ντετερμινιστικές,
μετά το LLM, ακριβώς πριν την εκτέλεση):
- `NO_NEW_POSITIONS`: κάθε STAKE αφαιρείται (`RISK_GATE_DROPPED_STAKE`,
  μη-εμποδιστικό)· το `Executor` αρνείται STAKE και μόνο του
  (`allow_new_positions`).
- `UNWIND`: **το LLM δεν καλείται**· `REDEEM_ALL` από τα positions.
- Σε κάθε κατάσταση: θέση σε προϊόν με status ≠ Available → REDEEM από τον
  wrapper (`origin: wrapper`).

### Εκτέλεση και σφάλματα Bybit (Φάση 2)

- **Μία εντολή εγγραφής:** `POST /v5/earn/place-order` με `category,
  orderType (Stake|Redeem), accountType, amount, coin, productId,
  orderLinkId`. Το request φτιάχνεται από το `place_order_request()` —
  το ίδιο dict είναι το `would_call` του dry-run και αυτό που στέλνεται live.
- **`orderLinkId`** ντετερμινιστικό: `<cycle_id>-S|R-<productId>` (≤ 36,
  αλλιώς hash).
- **Κανένα σιωπηλό `[]`:** κάθε αποτυχία (HTTP, timeout, μη-JSON,
  `retCode ≠ 0`, λείπει η λίστα) → `BybitAPIError`. Ο wrapper το γράφει στο
  `data_errors` και ως `DATA_UNAVAILABLE: <πηγή>` (μη-εμποδιστικό).
- **Fail closed:**
  - positions μη αναγνώσιμα → **κανένα LLM, καμία εντολή** στον κύκλο.
    Το ίδιο ισχύει αν **μία** θέση δεν έχει αναγνώσιμο `productId` ή
    `amount`: αλλιώς θα μετρούσε 0 στο όριο ανά προϊόν.
  - balance ή orders μη αναγνώσιμα → **καμία STAKE** (`DATA_GATE_DROPPED_STAKE`)·
    REDEEM επιτρέπεται.
  - products μη αναγνώσιμα → άδειο scan, όχι `CONFIG_INCOMPLETE`.
- **Εκκρεμείς εντολές:** κάθε κύκλος διαβάζει `GET /v5/earn/order`. Status
  εκτός `success`/`fail` (χωρίς διάκριση πεζών-κεφαλαίων) = εκκρεμής· σε
  νόμισμα με εκκρεμή εντολή δεν στέλνεται καμία νέα (ούτε STAKE ούτε
  REDEEM). Τα νομίσματα κανονικοποιούνται σε κεφαλαία παντού.
  - Εκκρεμής εντολή που **δεν αντιστοιχίζεται** σε νόμισμα του whitelist
    (χωρίς coin, άλλο νόμισμα), ή εκκρεμής Stake χωρίς αναγνώσιμο
    `productId`/`orderValue` → **μπλοκάρει όλες τις νέες εντολές**
    (`PENDING_ORDER_UNMATCHED`).
  - Τα ποσά εκκρεμών Stake αφαιρούνται από το όριο ανά προϊόν, όπως οι θέσεις.
  - Οι τελευταίες 20 εντολές μπαίνουν στο record (`orders`).
- **Testnet:** `BYBIT_TESTNET=1` → `https://api-testnet.bybit.com`.
- Το `bybit_earn_tool.py` CLI είναι πλέον μόνο για ανάγνωση (`--health`,
  `--products`, `--positions`, `--orders`, `--apr-history`, `--balance`).

### Δεδομένα και κωδικοί (Φάση 3)

- **Μόνο πραγματικά πεδία στο scan.** Αφαιρέθηκαν `apr_ma_7d`,
  `apr_p25_180d`, `apr_p75_180d`, `tier_cap_amount`,
  `marginal_apr_for_size`. Το prompt χρησιμοποιεί `estimate_apr`.
- `redemption_eta_hours = redeemProcessingMinute / 60`, `null` αν λείπει.
  Εκτός scan (για STAKE): tiered APR (`TIERED_APR_UNCERTAIN`), άγνωστο ETA
  (`REDEMPTION_ETA_UNKNOWN`), ETA > `MAX_REDEMPTION_ETA_HOURS` (`ILLIQUID`).
  Τα positions φέρουν `product_status` και `redemption_eta_hours` του
  προϊόντος τους, ώστε το LLM να βλέπει τη ρευστότητα των θέσεων.
- **APR history** ανά `productId`, ταξινομημένο κατά `timestamp`, μέσος όρος
  στο χρονικό παράθυρο 24 ωρών έως τώρα· `null` με λιγότερα από 6 σημεία.
- **Κωδικοί:**

| Κωδικός | Εμποδιστικός; | Πότε |
|---|---|---|
| `CONFIG_INCOMPLETE` | ναι | config άκυρο/μη αναγνώσιμο, prompt λείπει |
| `CRITICAL` | ναι | συνοδεύει τα παραπάνω· product id εκτός scan/positions |
| `AGENT_PARSE_ERROR` | ναι | έξοδος agent χωρίς αντικείμενο για τον κύκλο |
| `DECISION_VALIDATION_FAILED` | ναι | έξοδος agent εκτός σχήματος |
| `CYCLE_CRASH` | ναι | οποιαδήποτε απρόβλεπτη εξαίρεση (record με traceback, exit 4) |
| `AGENT_TIMEOUT` | όχι | ο agent δεν απάντησε σε 280 s |
| `DATA_UNAVAILABLE` | όχι | αποτυχία ανάγνωσης από Bybit |
| `RISK_STATE_*`, `RISK_GATE_*`, `DATA_GATE_*`, `PENDING_*`, `STALE_SCAN`, `CYCLE_LATENCY_HIGH` | όχι | — |

- **Config (T3.6):** όλα τα πεδία του `config/yield_rotation.yaml` είναι
  υποχρεωτικά και ελέγχονται σε τύπο και εύρος (`CONFIG_SCHEMA` στο
  `run_yield_cycle.py`) πριν από οτιδήποτε άλλο· `SIMULATED_IDLE_BALANCE`
  πρέπει να είναι `null` όταν `DRY_RUN: false`. Αποτυχία → record με
  `CONFIG_INCOMPLETE`, exit 3.

### Regression (Φάση 4)

- **Δύο επίπεδα:**
  - `pytest` — όλη η ντετερμινιστική συμπεριφορά (πύλες, παλαίωση, ποσά,
    REDEEM, product ids, `extract_json`, config, crash), χωρίς LLM, σε
    δευτερόλεπτα. Τα παλιά fixtures 02, 04–09 έγιναν wrapper tests· το 03
    (tier cap) καταργήθηκε μαζί με το πεδίο.
  - `tests/run_regression.py` — **μόνο ποιότητα απόφασης** με το πραγματικό
    μοντέλο (STAKE όταν πρέπει, HOLD όταν πρέπει), 5 σενάρια ×
    `--runs` (default 5). Τρέχει μόνο στο VPS.
- **Ίδια διαδρομή με την παραγωγή:** το regression καλεί το
  `run_cycle` με το `call_agent` της παραγωγής και το πραγματικό config·
  τα δεδομένα Bybit είναι αποθηκευμένες απαντήσεις που περνούν από τον
  πραγματικό `BybitEarnTool` (`tests/replay.py`). Το prompt είναι byte
  προς byte αυτό της παραγωγής (test με spy στο `compose_prompt`).
- **Δεδομένα:** σήμερα μόνο το `tests/data/SYNTHETIC_usdt_flexible.json`
  (χειρόγραφο, σε σχήμα docs). Στο testnet: `scripts/testnet.py capture`
  αποθηκεύει τις πραγματικές απαντήσεις στο `tests/data/` και το
  `tests/test_recorded_payloads.py` κλειδώνει την ανάλυση πάνω τους·
  `scripts/testnet.py roundtrip` κάνει Stake + Redeem και αποθηκεύει τα
  πάντα (το test ελέγχει `orderId` και τελική κατάσταση Success). Και τα
  δύο αρνούνται να τρέξουν χωρίς `BYBIT_TESTNET=1`.
- `NO_ELIGIBLE_PRODUCTS` (μη-εμποδιστικό): κανένα προϊόν δεν πέρασε τα
  φίλτρα (π.χ. παλιό APR history). Πριν τη Φάση 3 αυτό έγραφε
  `CONFIG_INCOMPLETE` και πάγωνε το heartbeat για κάτι παροδικό.

### Ποσά (T1.5)

Το LLM δεν δίνει ποσό (prompt v6). Ο wrapper:
`min(idle − RESERVE_USD, MAX_PER_PRODUCT_USD − ήδη_κρατούμενο_στο_προϊόν,
remaining_capacity, max_stake_amount)`, στρογγυλεμένο προς τα κάτω στο
`precision` του προϊόντος· παράλειψη αν < `max(MIN_MOVE_USD,
min_stake_amount)`. Το «ήδη κρατούμενο» προστέθηκε ώστε το
`MAX_PER_PRODUCT_USD` να είναι όριο ανά προϊόν και όχι ανά κύκλο.
Άγνωστο `precision`, `max_stake_amount` ή `min_stake_amount` → καμία STAKE
(κανόνας 7). REDEEM = πάντα ολόκληρη η θέση, χωρίς όριο `MIN_MOVE_USD`.

## 5. Πώς τρέχεις τα tests (επιβεβαίωσε ότι όλα περνάνε)

```bash
pip install -r requirements-dev.txt
pytest            # από τη ρίζα του repo — οπουδήποτε, όχι μόνο στο VPS
```

- Το `tests/conftest.py` στέλνει όλα τα paths σε `tmp_path`, σβήνει κλειδιά
  από το περιβάλλον και **απαγορεύει κάθε σύνδεση δικτύου**.
- Το `tests/run_regression.py` (LLM regression) **δεν** τρέχει από το
  `pytest`· χρειάζεται τον πραγματικό agent στο VPS.
- CI: `.github/workflows/tests.yml` τρέχει `pytest` σε κάθε push και PR.

### Paths και `.env` (Φάση 0)

Όλα τα paths βρίσκονται στο `settings.py`: env var με default για το VPS.
Το `ROOT` προκύπτει από το `__file__`. Κανένα module δεν έχει side effect
στο import (ούτε `mkdir`, ούτε ανάγνωση `.env`, ούτε `sys.path.insert`).

| Env var | Default (VPS) |
|---|---|
| `YIELD_HERMES_HOME` | `/opt/hermes` |
| `YIELD_CONFIG_FILE` | `<repo>/config/yield_rotation.yaml` |
| `YIELD_STATE_FILE` | `/opt/hermes/state/risk_state.json` |
| `YIELD_SESSION_DIR` | `/opt/hermes/state/yield_rotation_sessions` |
| `YIELD_HERMES_BIN` | `/opt/hermes/.venv/bin/hermes` |
| `YIELD_ENV_FILE` | `/opt/hermes/.env` |
| `YIELD_SHARED_ENV_FILE` | `/opt/data/.env` |
| `YIELD_RISK_STATE_DIR` | `/opt/hermes/tools` (προσωρινό, βλ. παρακάτω) |

Ενιαίο `settings.load_env()`, χωρίς εγγραφή στο `os.environ`:
**process env → `/opt/hermes/.env` → από το `/opt/data/.env` ΜΟΝΟ το
`TELEGRAM_BOT_TOKEN`**. Το κοινόχρηστο αρχείο δεν μπορεί πλέον να δώσει ή να
αντικαταστήσει κλειδιά Bybit ή το HMAC. Κανένα `.env` δεν φορτώνεται από το
τρέχον directory.

## 6. Τρέχον σημείο προόδου

Το σχέδιο ολοκλήρωσης είναι το `FINISH_PLAN.md`. Ο Hermes δεν εμπλέκεται
μέχρι το deploy· οδηγίες στο `DEPLOY.md` (γράφεται στο τέλος της Φάσης 5).

- ✅ **Φάση 0 — Φορητό repo** (T0.1–T0.4).
- ✅ **Φάση 1 — Ασφάλεια** (T1.1–T1.10), K1–K4, K6–K11, K15 (μέρος K12:
  ίδια εντολή agent σε παραγωγή και regression).
  - `risk_state.py` στη ρίζα, χωρίς fallback στο `/opt/hermes/tools`.
  - Prompt v6 (`PROMPT_VERSION: v6`)· το v4 στο `archive/`· το sha256 του
    prompt στο record.
  - `extract_json` δέχεται μόνο το **τελευταίο** αντικείμενο με το
    `cycle_id` του κύκλου.
  - Εντολή agent: `hermes chat --query-file /dev/stdin -Q --toolsets= -m
    <RESOLVED_MODEL> --reasoning <...>` — prompt από stdin, χωρίς tools.
- ⚠️ **Το prompt v6 δεν έχει δοκιμαστεί με το πραγματικό μοντέλο.** Βήμα
  του `DEPLOY.md`, πριν το επταήμερο dry-run.
- ✅ **Φάση 2 — Διαδρομή εκτέλεσης** (T2.1–T2.5), K5, K16, μέρος K20.
- ⚠️ **Προς επιβεβαίωση στο testnet** (δεν υπάρχουν καταγεγραμμένα
  payloads Bybit στο repo): ότι το `/v5/earn/product` δίνει `precision` και
  `maxStakeAmount`· ότι το `/v5/earn/position` επιστρέφει `result.list`· τα
  πεδία του `/v5/earn/order` (`status`, `orderType`, `coin`) και του
  `/v5/earn/apr-history`. Κάθε απόκλιση αποτυγχάνει **ασφαλώς** (καμία
  STAKE, `DATA_UNAVAILABLE` ή λόγος στο `executions[].reason`).
- 📝 Για το `DEPLOY.md`: LLM regression του v6 με το πραγματικό μοντέλο στο
  VPS, **πριν** το επταήμερο dry-run.
- ✅ **Φάση 3 — Ακεραιότητα δεδομένων** (T3.1–T3.7), K13, K14.
- ✅ **Φάση 4 — Regression που μετράει την παραγωγή** (T4.1–T4.4), K12.
- Εκκρεμεί η Φάση 5 (K19, K20 υπόλοιπο).

## 7. Εκκρεμότητες / TODO για την επόμενη συνεδρία

- [ ] Φάσεις 3–5 του `FINISH_PLAN.md`, μετά `DEPLOY.md`.
- [ ] Αν προσθέσετε νέα αρχεία Python που γράφουν/διαβάζουν HMAC,
  **βεβαιώσου ότι κάνουν import από `signing.py`** — ποτέ δεύτερη
  υλοποίηση.

## 8. Τι ΠΡΕΠΕΙ να πεις στο επόμενο «ξεκινάμε»

> Διάβασε πρώτα το `/opt/hermes/yield_rotation/HANDOFF.md` πριν προχωρήσεις.

---

_Τελευταία ενημέρωση: Φάση 4 του FINISH_PLAN (regression)_