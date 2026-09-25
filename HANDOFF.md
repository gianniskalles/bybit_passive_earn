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
| `risk_state.py` (`/opt/hermes/tools/`) | Διαβάζει/γράφει/επαληθεύει τη risk_state. |
| `executor.py` | Dry-run vs live execution wrapper. |
| `bybit_earn_tool.py` | Bybit Earn API client (CLI). |
| `config/yield_rotation.yaml` | Όλες οι παράμετροι στρατηγικής. |
| `tests/test_heartbeat.py` | 11 pytest tests για το heartbeat. |
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

3. **Connecticut**: `risk_state` σε κατάσταση μη-NORMAL (π.χ.
   `NO_NEW_POSITIONS`) **δεν ξαναγράφεται** από το heartbeat — το
   σέβεται και απέχει (ABSTAIN). Ανανεώνει ΜΟΝΟ stale `NORMAL`.

## 4. Ποιες καταστάσεις επεξεργάζεται το heartbeat (decision table)

| Τρέχουσα risk_state | Αποτέλεσμα heartbeat |
|---|---|
| Καμία εκτέλεση ακόμα (bootstrap, φάκελος υπάρχει, 0 αρχεία) | Γράφει `NORMAL` (exit 0) |
| Φάκελος LOG_DIR δεν υπάρχει | **ERROR exit 3** + alert (NON-βootstrap) |
| `NORMAL` + φρέσκια | Δεν ξαναγράφει (OK, no write) |
| `NORMAL` + stale | Ξαναγράφει `NORMAL` με νέο ts (renew) → επόμενος κύκλος χωρίς `forced:true` |
| μη-`NORMAL` (π.χ. `NO_NEW_POSITIONS`) + stale | **ABSTAIN** — δεν υπερκαλύπτει |
| Παρόν αλλά valid HMAC δεν επαληθεύεται | **ABSTAIN** — δεν υπερκαλύπτει |
| Τελευταίος κύκλος βρέθηκε με blocking code (π.χ. `CONFIG_INCOMPLETE`) | **ABSTAIN** — δεν ανανεώνει |
| Τελευταίος κύκλος βρέθηκε με non-blocking code (π.χ. `RISK_STATE_STALE`) | Επιτρέπει renewal |

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

Το σχέδιο ολοκλήρωσης είναι το `FINISH_PLAN.md`.

- ✅ **Φάση 0 — Φορητό repo** (T0.1–T0.4): `settings.py`, ενιαίο
  `load_env`, pytest με `conftest.py`, `requirements*.txt`, GitHub Actions.
  24/24 tests σε καθαρό clone (πριν: 0/11).
- ⚠️ **`risk_state.py` ακόμα εκτός repo.** Ο wrapper το φορτώνει πλέον
  lazily: πρώτα από το repo, αλλιώς από `YIELD_RISK_STATE_DIR`
  (`/opt/hermes/tools`). Ο Hermes πρέπει να το κάνει commit στη ρίζα του repo
  **πριν τη Φάση 1** (το T1.2 αλλάζει το `verify`).
- ⚠️ Τα γνωστά ευρήματα K1–K17, K19, K20 του `FINISH_PLAN.md` **δεν έχουν
  διορθωθεί ακόμα** — η Φάση 0 δεν αλλάζει καμία συμπεριφορά απόφασης.
  Ειδικά: το test `test_bootstrap_no_logs_writes_normal` ελέγχει
  `NO_NEW_POSITIONS` (K2) — διορθώνεται στο T1.3.
- ⚠️ Deploy (Hermes): μετά το `git pull` χρειάζεται `pip install -r
  requirements.txt` στο venv (ίδιες εξαρτήσεις με πριν: PyYAML, requests).
  Το heartbeat δεν διαβάζει πλέον τίποτα άλλο από το `/opt/data/.env` εκτός
  από `TELEGRAM_BOT_TOKEN` — αν το `HERMES_RISK_HMAC_KEY` ή κλειδιά Bybit
  ζουν μόνο εκεί, πρέπει να μεταφερθούν στο `/opt/hermes/.env`.

## 7. Εκκρεμότητες / TODO για την επόμενη συνεδρία

- [ ] **Live ενσωμάτωση heartbeat με τον wrapper** (end-to-end): τρέξε
  το heartbeat και μετά 1 live cycle για να δεις ότι το 2º run γίνεται
  χωρίς `forced:true`.
- [ ] **Παρατήρηση ότι το τελευταίο πραγματικό scan** (2026-09-14) έβγαλε
  `NO_NEW_POSITIONS/RISK_STATE_STALE` — brownout. Το heartbeat είναι η
  άμυνα εναντίον αυτού.
- [ ] Αν προσθέσετε νέα αρχεία Python που γράφουν/διαβάζουν HMAC,
  **βεβαιώσου ότι κάνουν import από `signing.py`** — ποτέ ξανά δεν
  ανοσογονείται δεύτερη υλοποίηση.

## 8. Τι ΠΡΕΠΕΙ να πεις στο επόμενο «ξεκινάμε»

> Διάβασε πρώτα το `/opt/hermes/yield_rotation/HANDOFF.md` πριν προχωρήσεις.

---

_Τελευταία ενημέρωση: v5.3 (heartbeat rewrite commits)_