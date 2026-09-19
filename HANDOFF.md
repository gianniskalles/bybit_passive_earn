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
| `signing.py` | **ΜΟΝΑΔΙΚΗ πηγή αλήθειας** για canonical JSON + HMAC-SHA256. Το εισάγουν ΚΑΙ το heartbeat.py ΚΑΙ το risk_state.py. |
| `risk_state.py` (`/opt/hermes/tools/`) | Διαβάζει/γράφει/επαληθεύει τη risk_state. |
| `executor.py` | Dry-run vs live execution wrapper. |
| `bybit_earn_tool.py` | Bybit Earn API client (CLI). |
| `config/yield_rotation.yaml` | Όλες οι παράμετροι στρατηγικής. |
| `tests/test_heartbeat.py` | **9 regression tests** για το heartbeat (v5.3). |

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
# Δύο σετ tests:
# 1) Οι 9 νέοι heartbeat tests (repo)
python3 /opt/hermes/yield_rotation/tests/test_heartbeat.py

# 2) Το παλιό 40-case regression suite
cd /opt/hermes/yield_rotation && python3 -m pytest tests/run_regression.py -q
```

Το `tests/test_heartbeat.py` έχει ειδικά escapes (env vars):
- `YIELD_LOG_DIR`, `YIELD_STATE_FILE` — override διαδρομών
- `YIELD_SKIP_API_CHECK=1` — δεν καλεί Bybit
- `YIELD_SKIP_CONFIG_DCHECK` — `1` στο δοκιμαστικό path, `0` στο
  ελεγχόμενο (missing LOG_DIR dir → exit 3)

## 6. Τρέχον σημείο προόδου

- ✅ **v5.3 heartbeat rewrite ολοκληρωμένο** — 9/9 tests περνάνε
  (συμπ. bootstrap-no-logs→NORMAL και το «κολλημένο» σενάριο
  stale-NORMAL→renew→next cycle χωρίς forced:true).
- ✅ `signing.py` κοινό (heartbeat + risk_state + tests το εισάγουν).
- ✅ LOG_DIR ως misconfig-error (exit 3) αντί για σιωπηλό boot.
- ⚠️ **ΕΚΚΡΕΜΕΙ:** η risk_state τρέχων κατάσταση είναι `NORMAL`
  (ts 1789342994399, «fresh for staleness test»). Αναμένεται η heartbeat
  να τη διατηρεί προσεγμένα.
- ⚠️ **ΕΚΚΡΕΜΕΙ:** το `overwrites` με το wrapper/agent — το v5.3 heartbeat
  υπάρχει και δοκιμάζεται **μεμονωμένα**· δεν έχει ακόμα ενσωματωθεί
  πλήρως σε end-to-end live κύκλο μετά το rewrite.
- ⚠️ **Πίσω στο μυαλό:** Cron daily scanner (betting) paused pending
  acceptance test — ΔΕΝ σχετίζεται με αυτό το repo.

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