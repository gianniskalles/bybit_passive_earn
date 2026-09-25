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
`DECISION_VALIDATION_FAILED`. Το `CYCLE_MODEL_MISMATCH` καταργήθηκε (T1.9).

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
- ⚠️ **Το prompt v6 δεν έχει δοκιμαστεί με το πραγματικό μοντέλο.** Το LLM
  regression χρειάζεται τον agent στο VPS· τα fixtures είναι ακόμα σε
  σχήμα v5 (`amount_usd`, `from_product_id`) και ξαναγράφονται στη Φάση 4.
- ⚠️ Προς επιβεβαίωση στο testnet: ότι η Bybit επιστρέφει `precision` και
  `maxStakeAmount` στο `/v5/earn/product`. Αν λείπουν, ο wrapper δεν κάνει
  ποτέ STAKE (ασφαλής αποτυχία, φαίνεται στο `executions[].reason`).
- Εκκρεμούν οι Φάσεις 2–5 (K5, K12–K14, K16, K17 μέρος, K19, K20).

## 7. Εκκρεμότητες / TODO για την επόμενη συνεδρία

- [ ] Φάσεις 2–5 του `FINISH_PLAN.md`.
- [ ] Αν προσθέσετε νέα αρχεία Python που γράφουν/διαβάζουν HMAC,
  **βεβαιώσου ότι κάνουν import από `signing.py`** — ποτέ δεύτερη
  υλοποίηση.

## 8. Τι ΠΡΕΠΕΙ να πεις στο επόμενο «ξεκινάμε»

> Διάβασε πρώτα το `/opt/hermes/yield_rotation/HANDOFF.md` πριν προχωρήσεις.

---

_Τελευταία ενημέρωση: Φάση 1 του FINISH_PLAN (ασφάλεια)_