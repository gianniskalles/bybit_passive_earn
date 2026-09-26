# FINISH_PLAN.md — Bybit Passive Earn: σχέδιο ολοκλήρωσης

**Repo:** `github.com/gianniskalles/bybit_passive_earn`
**Βάση ανάλυσης:** commit `17c7447` (25 Σεπ 2026)
**Μέθοδος:** ανάγνωση όλων των αρχείων, εκτέλεση των heartbeat tests, και αναπαραγωγή του wrapper από άκρη σε άκρη με ψεύτικο Bybit και ψεύτικο LLM. Κάθε εύρημα με σήμανση «αναπαράχθηκε» έχει επιβεβαιωθεί με εκτέλεση του πραγματικού κώδικα, όχι με ανάγνωση.

---

## 0. Πώς χρησιμοποιείται αυτό το αρχείο

Τρεις ρόλοι, καθαρά χωρισμένοι:

| Ποιος | Τι κάνει | Πού |
|---|---|---|
| **Claude Code** | Όλη η ανάπτυξη και τα tests, σε branches με PR | Τοπικό clone του repo — **όχι στο VPS** |
| **Hermes** | Deploy: `git pull`, systemd, `.env`, testnet run | VPS |
| **Giannis** | Αποφάσεις (§2), merge των PR, Bybit UI (κλειδιά, κεφάλαια) | Κινητό / browser |

Ο Claude Code δεν χρειάζεται κανένα κλειδί. Όλα τα tests τρέχουν με ψεύτικο Bybit και ψεύτικο LLM. Αν ένα task φαίνεται να χρειάζεται πραγματικό API, είναι λάθος σχεδίαση του task.

**Πρώτο μήνυμα προς τον Claude Code:**

> Διάβασε ολόκληρο το `FINISH_PLAN.md` στη ρίζα του repo. Οι αποφάσεις της §2 είναι συμπληρωμένες. Ξεκίνα από τη Φάση 0. Για κάθε εργασία: πρώτα test που αποτυγχάνει, μετά διόρθωση, μετά `pytest`. Ένα branch και ένα PR ανά φάση, με την έξοδο του `pytest` στην περιγραφή. Στο τέλος κάθε φάσης σταμάτα και δείξε μου τι άλλαξε. Μην αλλάξεις τίποτα από την §6.

**Πριν ξεκινήσει ο Claude Code**, ο Hermes κάνει commit το `/opt/hermes/tools/risk_state.py` μέσα στο repo, ως έχει. Είναι στοιχείο ασφαλείας που σήμερα ζει εκτός version control, και ο wrapper δεν κάνει καν import χωρίς αυτό.

---

## 1. Διάγνωση

### Συμπέρασμα

**Δεν είναι έτοιμο για πραγματικά χρήματα.** Το μόνο που προστατεύει σήμερα είναι το `DRY_RUN: true` — και, κατά τύχη, το ότι τα endpoints εκτέλεσης είναι λάθος, οπότε καμία πραγματική εντολή δεν θα περνούσε ούτως ή άλλως.

Η δουλειά των τελευταίων συνεδριών επικεντρώθηκε στο heartbeat, που είναι καλοδουλεμένο. Αλλά οι έλεγχοι ασφαλείας που περιγράφουν τα docs και το HANDOFF δεν επιβάλλονται στον κώδικα του wrapper — τους εμπιστευόμαστε ακόμα στο LLM, δηλαδή ακριβώς αυτό που αποφασίσαμε να σταματήσουμε μετά τα 3/5 και 1/5 των fixtures 05/06.

### Τι στέκει καλά

- `signing.py`: μία υλοποίηση HMAC, canonical JSON, σύγκριση σταθερού χρόνου.
- Ο διαχωρισμός των δύο ελέγχων παλαίωσης (`apr_history_age_seconds` / `MAX_SCAN_AGE_SECONDS`) είναι σωστός και σωστά ονομασμένος.
- Ο πίνακας αποφάσεων του heartbeat και το στυλ των tests του.
- Atomic writes (`tmp` + `replace`), η δικλείδα `SIMULATED_IDLE_BALANCE` + live, ο έλεγχος `minStakeAmount`, τα πλούσια decision records.
- **Ασφάλεια git:** το repo είναι δημόσιο, αλλά ελέγχθηκαν και τα 3 commits — κανένα κλειδί Bybit, κανένα κλειδί HMAC, κανένα `.env`. Το `.gitignore` είναι σωστό.

### Ευρήματα

🔴 κρίσιμο · 🟠 σοβαρό · 🟡 μέτριο · ⚪ μικρό

| # | | Εύρημα | Πού | Απόδειξη |
|---|---|---|---|---|
| K1 | 🔴 | Τα `NO_NEW_POSITIONS` και `UNWIND` δεν επιβάλλονται στον κώδικα. Αν το LLM πει STAKE, εκτελείται — ακόμα και υπό UNWIND. | `run_yield_cycle.py` main, `executor.py` | Αναπαράχθηκε |
| K2 | 🔴 | Κλείδωμα στο bootstrap: το heartbeat γράφει `NO_NEW_POSITIONS` και καμία διαδρομή δεν οδηγεί ποτέ σε `NORMAL`, αφού μη-NORMAL δεν αντικαθίσταται. Έρχεται σε αντίθεση με το `HANDOFF.md` §4. Στο ίδιο commit το test άλλαξε για να ταιριάζει στον κώδικα: ο έλεγχος έγινε από `NORMAL` σε `NO_NEW_POSITIONS`, ενώ το όνομα λέει ακόμα `writes_normal`. | `heartbeat.py:376-381, 399-414` | Αναπαράχθηκε: 5 καθαροί κύκλοι, παραμένει `NO_NEW_POSITIONS` |
| K3 | 🔴 | Κατεστραμμένο αρχείο risk_state αντιμετωπίζεται ως απόν. Ένα κομμένο αρχείο που έλεγε `UNWIND` γίνεται `NORMAL`. | `heartbeat.py:166-173` | Αναπαράχθηκε |
| K4 | 🔴 | Η παραγωγή φορτώνει το `prompt_v4.md` ενώ το record γράφει `v5`. Το v4 ζητά `amount`, ο validator το απορρίπτει, άρα κάθε STAKE γίνεται `DECISION_VALIDATION_FAILED` (εμποδιστικό) και το heartbeat παγώνει. | `run_yield_cycle.py:72` | Αναπαράχθηκε |
| K5 | 🔴 | Η live εκτέλεση καλεί ανύπαρκτα endpoints (`/v5/earn/subscribe`, `/v5/earn/redeem`) χωρίς `category`, `orderType`, `accountType`, `coin`, `orderLinkId`. Δεν έχει τρέξει ποτέ, γιατί το dry-run δεν καλεί το tool. Σωστό: `POST /v5/earn/place-order` (Παράρτημα Α). | `bybit_earn_tool.py:231, 244` | Έλεγχος κώδικα + docs Bybit |
| K6 | 🔴 | Έξοδος από προϊόν που έγινε NotAvailable είναι αδύνατη: ο wrapper το αφαιρεί από το scan, και μετά το Check 4 απορρίπτει το REDEEM ως «εκτός scan». Επιπλέον το prompt ζητά `from_product_id`, ο validator ζητά `product_id`. Και οι δύο δρόμοι δίνουν εμποδιστικό κωδικό. | `run_yield_cycle.py:720`, `prompt_v5.md:141` | Αναπαράχθηκε |
| K7 | 🔴 | Το ποσό το υπολογίζει το LLM και δεν ελέγχεται. STAKE 80 με `MAX_PER_PRODUCT_USD: 5` περνάει. `amount_usd: "5"` (string) ρίχνει τον κύκλο χωρίς να γραφτεί decision record. | `executor.py:81`, `validate_decision_record` | Αναπαράχθηκε |
| K8 | 🟠 | Το `extract_json` επιστρέφει το **πρώτο** αντικείμενο με `decisions` (το docstring λέει το τελευταίο). Αν το stdout περιέχει το παράδειγμα του prompt, εκτελείται το παράδειγμα — και αφού χρησιμοποιεί `product_id: "1"`, που είναι το πραγματικό προϊόν, περνάει και το Check 4. | `run_yield_cycle.py:390-439` | Αναπαράχθηκε |
| K9 | 🟠 | Ο έλεγχος μοντέλου δεν ελέγχει τίποτα (`actual_model = resolved_model`), αλλά η λέξη «fallback» οπουδήποτε στο stdout τον ενεργοποιεί ως εμποδιστικό κωδικό. | `run_yield_cycle.py:347, 709` | Αναπαράχθηκε |
| K10 | 🟠 | Ο agent στην παραγωγή καλείται χωρίς `--toolsets=` (το regression τα απενεργοποιεί), με `HOME=/opt/hermes` όπου βρίσκεται το `.env`. Το prompt περνάει μέσω argv, ορατό στο `ps`. | `run_yield_cycle.py:338-346` | Έλεγχος κώδικα· επιβεβαίωσε τα default toolsets του CLI |
| K11 | 🟠 | Παλαιό ή άκυρο risk state τερματίζει τον κύκλο πριν μαζέψει δεδομένα. Ένα UNWIND παύει να εκτελείται 30 λεπτά μετά. Το «NO_NEW_POSITIONS από αποτυχία» σημαίνει «τίποτα», όχι «μόνο εξαργυρώσεις» όπως ορίζει το spec. | `run_yield_cycle.py:581-616` | Έλεγχος κώδικα |
| K12 | 🟠 | Το regression δεν μετράει την παραγωγή. Διαφέρουν: prompt (v5 έναντι v4), μοντέλο (χωρίς `-m`, άρα default), toolsets, σύνθεση prompt, `extract_json`, σχήμα εισόδου (`productId`/`product_id`, risk_state dict/string, `IDLE_BALANCE_USDT`/`balances`), κατώφλια (0.018/0.012 έναντι 0.001/0). Τα fixtures 04-07 και 09 δεν έγιναν wrapper tests· το 09 περιμένει από το LLM να παράγει product `999`. | `tests/run_regression.py`, `tests/fixtures.py` | Έλεγχος κώδικα |
| K13 | 🟠 | Ψεύτικα δεδομένα στο scan: `redemption_eta_hours: 0.0` σκληροκωδικοποιημένο ενώ η Bybit δίνει `redeemProcessingMinute`, `apr_ma_7d = apr_ma_24h`. Ο κανόνας εξόδου λόγω ρευστότητας δεν μπορεί ποτέ να ενεργοποιηθεί. | `run_yield_cycle.py:204-209` | Αναπαράχθηκε: 48h → 0.0 |
| K14 | 🟡 | Το APR history ζητιέται ανά coin, όχι ανά productId: με δεύτερο προϊόν USDT, λάθος ιστορικό σε λάθος προϊόν. Το `hist[-24:]` υποθέτει αύξουσα σειρά χωρίς να ταξινομεί. | `run_yield_cycle.py:148, 516`, `bybit_earn_tool.py:180` | Έλεγχος κώδικα |
| K15 | 🟡 | Το ABSTAIN του heartbeat γράφει στον δίσκο (ξαναϋπογράφει και χάνει πεδία). Crash σε αρχείο χωρίς `profile` (KeyError) ή με `sig: null` (TypeError). | `heartbeat.py:427-436, 385` | Αναπαράχθηκαν |
| K16 | 🟡 | Κανένα `orderLinkId`, καμία παρακολούθηση εκκρεμών εντολών. Οι εντολές Bybit είναι ασύγχρονες και η εξαργύρωση μπορεί να πάρει 48 ώρες — ο επόμενος κύκλος μπορεί να ξαναστείλει την ίδια κίνηση. | — | Έλεγχος κώδικα |
| K17 | 🟡 | Στο heartbeat το κοινόχρηστο `/opt/data/.env` υπερισχύει του `/opt/hermes/.env`. Το `bybit_earn_tool` φορτώνει `.env` από το τρέχον directory και γράφει στο `os.environ` κατά το import. | `heartbeat.py:310`, `bybit_earn_tool.py:27-32` | Έλεγχος κώδικα |
| K18 | 🟡 | Τα tests δεν τρέχουν εκτός VPS: 0/11 σε καθαρό clone. Με προσομοίωση των paths του VPS: 11/11. | παντού | Αναπαράχθηκε |
| K19 | 🟡 | Κάθε ABSTAIN στέλνει Telegram σε κάθε εκτέλεση του heartbeat, χωρίς dedup — εκατοντάδες ειδοποιήσεις την ημέρα σε κολλημένη κατάσταση. | `heartbeat.py` | Έλεγχος κώδικα |
| K20 | ⚪ | Το `BYBIT_TESTNET` δεν χρησιμοποιείται (mainnet σκληροκωδικοποιημένο)· το `backfill` αγνοεί το `days`· `return` αντί `continue` στο `executor.py:60`· έλεγχος μόνο 3 από ~15 υποχρεωτικές παραμέτρους· το `HANDOFF.md` έχει αλλοιωμένες λέξεις, άσχετη αναφορά σε betting scanner και λείπουν οι αποφάσεις στρατηγικής· το README περιγράφει v4 και ανύπαρκτο `--prompt-version`· δεν υπάρχουν systemd units στο repo· τα raw session files δεν διαγράφονται ποτέ· το ROTATE υπάρχει στο prompt αλλά όχι σε validator ή executor. | διάφορα | Έλεγχος κώδικα |

---

## 2. Αποφάσεις πριν ξεκινήσει ο agent

Συμπληρωμένες με την προτεινόμενη επιλογή. Άλλαξε ό,τι διαφωνείς πριν δώσεις το αρχείο στον Claude Code.

**Α1 — Πολιτική bootstrap (K2)**
- Α: το heartbeat γράφει αμέσως `NORMAL` (ο αρχικός εγκεκριμένος πίνακας).
- **Β (προτεινόμενο):** ξεκινά με `NO_NEW_POSITIONS` και προάγεται μόνο του σε `NORMAL` μετά τον πρώτο επαληθευμένο καθαρό κύκλο. Κρατά την πρόθεση του τελευταίου commit και προσθέτει την έξοδο που λείπει.

**Α2 — Ποιος υπολογίζει το ποσό (K7)**
- **Wrapper (προτεινόμενο):** το LLM λέει μόνο «STAKE στο προϊόν Χ», ο κώδικας υπολογίζει το ποσό. Εξαφανίζει όλη την κατηγορία σφαλμάτων τύπου και ορίου, και συμφωνεί με την αρχή «το LLM δεν υπολογίζει».
- LLM: κρατάμε το `amount_usd` με αυστηρό έλεγχο.

**Α3 — ROTATE**
- **Αφαίρεση μέχρι να υπάρξει δεύτερο προϊόν USDT (προτεινόμενο).** Σήμερα είναι νεκρός κώδικας που το prompt προσφέρει αλλά κανείς δεν υλοποιεί.

**Α4 — Ορατότητα repo**
- **Private (προτεινόμενο).** Δεν έχει διαρρεύσει τίποτα, αλλά εκθέτει paths υποδομής, Telegram chat id και τη στρατηγική — και ένα ξεχασμένο `.env` σε δημόσιο repo είναι μη αναστρέψιμο.

---

## 3. Κανόνες για τον agent

1. **Πρώτα το test, μετά η διόρθωση.** Κάθε εύρημα ξεκινά με test που αποτυγχάνει στο `17c7447`.
2. **Τα tests ακολουθούν το spec, όχι τον κώδικα.** Αν ένα test αποτυγχάνει, διόρθωσε τον κώδικα. Ποτέ μην αλλάξεις test για να περάσει — έτσι δημιουργήθηκε το K2.
3. **Καμία κλήση δικτύου στα tests.** Bybit και `hermes chat` πάντα mocked.
4. **Κανένα κλειδί, κανένα `.env`.** Αν χρειάζεται, `.env.example` με κενές τιμές.
5. **Ποτέ `DRY_RUN: false`**, ούτε σε default, ούτε σε test fixture που γράφεται σε αρχείο.
6. **Μία υλοποίηση HMAC:** μόνο το `signing.py`.
7. **Άγνωστο = `null`.** Ποτέ «εύλογη» τιμή στη θέση ενός δεδομένου που δεν έχουμε — αυτό έφτιαξε το K13.
8. **Κάθε αλλαγή στους εμποδιστικούς κωδικούς** αλλάζει στο ίδιο PR και στο `BLOCKING_CODES` του heartbeat, με test.
9. **Τίποτα από την §6** χωρίς ρητή εντολή του Giannis.
10. **Στο τέλος κάθε φάσης:** ενημέρωσε το `HANDOFF.md`, και σταμάτα.

---

## 4. Εργασίες

### Φάση 0 — Φορητό repo (προαπαιτούμενο για όλα)

| ID | Εργασία | Κριτήριο αποδοχής |
|---|---|---|
| T0.1 | Όλα τα paths σε ένα module ρυθμίσεων: από env, με defaults για το VPS. Το `ROOT` από το `__file__`. Κανένα side effect στο import (`mkdir`, `load_env`, `sys.path.insert` σε απόλυτα paths). | `import run_yield_cycle` σε καθαρό μηχάνημα δεν αγγίζει το `/opt` |
| T0.2 | Ενιαίο `load_env`: ανέχεται απόν αρχείο· δεν φορτώνει από το τρέχον directory· δεν γράφει στο `os.environ`. Προτεραιότητα: process env → `/opt/hermes/.env` → από το `/opt/data/.env` **μόνο** το `TELEGRAM_BOT_TOKEN`. | Test προτεραιότητας· test ότι το `/opt/data/.env` δεν αντικαθιστά κλειδιά Bybit ή HMAC |
| T0.3 | `pytest` με `conftest.py` και fixtures σε `tmp_path`. Το `test_heartbeat.py` σε pytest. `requirements-dev.txt`. | `git clone && pip install -r requirements-dev.txt && pytest` πράσινο χωρίς `/opt` |
| T0.4 | GitHub Actions: `pytest` σε κάθε push και PR, χωρίς δίκτυο. | Πράσινο badge |

### Φάση 1 — Ασφάλεια (P0)

| ID | Εργασία | Κριτήριο αποδοχής |
|---|---|---|
| T1.1 | **Ντετερμινιστική πύλη risk state (K1).** `NORMAL`: το LLM αποφασίζει. `NO_NEW_POSITIONS`: το LLM αποφασίζει, ο wrapper αφαιρεί κάθε STAKE με μη-εμποδιστικό alert `RISK_GATE_DROPPED_STAKE`. `UNWIND`: **χωρίς LLM** — ο wrapper φτιάχνει `REDEEM_ALL` από τα positions. Ο διακόπτης κινδύνου δεν εξαρτάται από μοντέλο. | STAKE από το LLM υπό NO_NEW_POSITIONS ή UNWIND δεν φτάνει ποτέ στον executor |
| T1.2 | **Η παλαίωση κάνει το σύστημα μόνο πιο συντηρητικό (K11).** Το `risk_state.verify` επιστρέφει ξεχωριστά «υπογραφή έγκυρη» και «φρέσκο». Έγκυρο αλλά παλιό: NORMAL→NO_NEW_POSITIONS, NO_NEW_POSITIONS→ίδιο, UNWIND→UNWIND. Άκυρο, μη αναγνώσιμο ή απόν: NO_NEW_POSITIONS + alert. Ο κύκλος **συνεχίζει** με τη διαδρομή εξαργυρώσεων, δεν τερματίζει. | Παλιό UNWIND συνεχίζει να παράγει REDEEM_ALL· άκυρη υπογραφή επιτρέπει εξαργύρωση, όχι stake |
| T1.3 | **Bootstrap (K2), κατά την απόφαση Α1.** Για το Β: υπογεγραμμένο πεδίο `source` (`heartbeat_bootstrap`, `heartbeat_renew`, `operator`). Το heartbeat προάγει σε NORMAL **μόνο** κατάσταση με `source: heartbeat_bootstrap`, και μόνο αφού ολοκληρωθεί επαληθευμένος καθαρός κύκλος **μετά** το `ts` της. Καταστάσεις `operator` δεν τις αγγίζει ποτέ. Το όνομα κάθε test περιγράφει αυτό που ελέγχει. | Σενάριο: bootstrap → κύκλος → heartbeat → NORMAL. Και: operator NO_NEW_POSITIONS μένει για πάντα |
| T1.4 | **Heartbeat ανθεκτικό (K3, K15).** Το «απόν» διακρίνεται από το «μη αναγνώσιμο»· μη αναγνώσιμο → ABSTAIN + alert, ποτέ αντικατάσταση. ABSTAIN σημαίνει **καμία** εγγραφή. Κανένα crash σε λείπον κλειδί ή λάθος τύπο. | Κομμένο UNWIND μένει ανέγγιχτο· `mtime` αμετάβλητο μετά από ABSTAIN· `sig: null` και λείπον `profile` δεν ρίχνουν το heartbeat |
| T1.5 | **Ποσό από τον wrapper (K7, απόφαση Α2).** Prompt v6 χωρίς υπολογισμό ποσού. Ο wrapper υπολογίζει `min(idle − RESERVE_USD, MAX_PER_PRODUCT_USD, remaining_capacity, max_stake_amount)`, στρογγυλεύει προς τα κάτω στο precision του προϊόντος, και παραλείπει αν το αποτέλεσμα είναι κάτω από `max(MIN_MOVE_USD, min_stake_amount)`. | Κανένα ποσό δεν ξεπερνά ποτέ το `MAX_PER_PRODUCT_USD`· κανένας τύπος από το LLM δεν ρίχνει τον κύκλο |
| T1.6 | **REDEEM (K6).** Ένα πεδίο, `product_id`, παντού. Ο έλεγχος product id για REDEEM γίνεται έναντι των **positions**, όχι του scan. Ο wrapper παράγει ντετερμινιστικά REDEEM για θέσεις σε προϊόν με status ≠ Available — ο κανόνας είναι μηχανικός. | Θέση σε NotAvailable προϊόν → REDEEM χωρίς εμποδιστικό κωδικό |
| T1.7 | **Επιλογή prompt (K4).** Από το `PROMPT_VERSION` του config· αν λείπει το αρχείο, hard fail. Το sha256 του prompt στο decision record. Το `prompt_v4.md` σε `archive/`. | Το record αποδεικνύει ποιο αρχείο έτρεξε |
| T1.8 | **Εξαγωγή JSON (K8).** Η έξοδος πρέπει να περιέχει το `cycle_id` του τρέχοντος κύκλου· αλλιώς `AGENT_PARSE_ERROR`. Λαμβάνεται το τελευταίο έγκυρο αντικείμενο. Τα παραδείγματα του prompt χρησιμοποιούν ids που δεν μπορούν να υπάρξουν (`"EXAMPLE"`). | stdout με το παράδειγμα πριν την απάντηση → εκτελείται η απάντηση |
| T1.9 | **Έλεγχος μοντέλου (K9).** Αφαίρεση του ελέγχου για τη λέξη «fallback». Είτε πραγματικός έλεγχος (αν το CLI αναφέρει το μοντέλο που απάντησε) είτε αφαίρεση του ελέγχου, με το πεδίο να μετονομάζεται σε `model_requested_on_cli`. | Η λέξη «fallback» σε ένα reason δεν παράγει κανέναν κωδικό |
| T1.10 | **Απομόνωση agent (K10).** `--toolsets=` στην κλήση παραγωγής· prompt μέσω stdin ή `--query-file`, όχι argv· ακριβώς τα ίδια flags με το regression. | Test που επιβεβαιώνει την ακριβή εντολή |

### Φάση 2 — Διαδρομή εκτέλεσης (P0 για live)

| ID | Εργασία | Κριτήριο αποδοχής |
|---|---|---|
| T2.1 | `place_order()` στο `POST /v5/earn/place-order` με όλα τα υποχρεωτικά πεδία (Παράρτημα Α). `orderLinkId` ντετερμινιστικό από `cycle_id` + ενέργεια + προϊόν (≤ 36 χαρακτήρες, `[A-Za-z0-9_-]`). Διαγραφή των `subscribe_earn_product` / `redeem_earn_product`. | Test με mocked HTTP: ακριβές σώμα και υπογραφή |
| T2.2 | Παρακολούθηση εντολών μέσω `GET /v5/earn/order`. Καμία νέα εντολή σε νόμισμα με εκκρεμή. Η κατάσταση κάθε εντολής στο record. | Εκκρεμής εξαργύρωση → επόμενος κύκλος δεν ξαναστέλνει |
| T2.3 | `BASE_URL` από το `BYBIT_TESTNET`. | Test και για τα δύο |
| T2.4 | Χειρισμός `retCode ≠ 0` και HTTP σφαλμάτων: καταγραφή, ποτέ σιωπηλά `[]`. | Test με mocked αποτυχία |
| T2.5 | Το `would_call` του dry-run είναι το ακριβές request που θα έφευγε, από την ίδια συνάρτηση. | Test ισότητας dry-run / live request |

### Φάση 3 — Ακεραιότητα δεδομένων (P1)

| ID | Εργασία | Κριτήριο αποδοχής |
|---|---|---|
| T3.1 | `redemption_eta_hours = redeemProcessingMinute / 60`· `null` αν λείπει. | 2880 → 48.0 |
| T3.2 | APR history ανά `productId`· ρητή ταξινόμηση κατά `timestamp`· μέσος όρος σε χρονικό παράθυρο 24 ωρών, όχι στις τελευταίες 24 εγγραφές· `null` με λιγότερα από 6 σημεία. | Test με ιστορικό σε φθίνουσα σειρά |
| T3.3 | Αφαίρεση των ψεύτικων πεδίων (`apr_ma_7d`, `apr_p25_180d`, `apr_p75_180d`, `tier_cap_amount`) από scan και prompt, ή πραγματικός υπολογισμός τους. | Κανένα σκληροκωδικοποιημένο πεδίο στο scan |
| T3.4 | Positions με αριθμητικούς τύπους και `snake_case`, όπως το υπόλοιπο σχήμα. | — |
| T3.5 | Νέοι κωδικοί: `DATA_UNAVAILABLE` (αποτυχία δικτύου, μη-εμποδιστικό) διακριτό από το `CONFIG_INCOMPLETE`· `AGENT_TIMEOUT` διακριτό από το `AGENT_PARSE_ERROR` (μη-εμποδιστικό). Ενημέρωση του `BLOCKING_CODES`. | Προσωρινό πρόβλημα δικτύου δεν παγώνει το heartbeat |
| T3.6 | Έλεγχος ολόκληρου του config στην εκκίνηση: υποχρεωτικά πεδία, τύποι, εύρη. | Λείπει οποιοδήποτε → hard fail με record |
| T3.7 | Κάθε μη αναμενόμενη εξαίρεση στο `main` γράφει record με `CYCLE_CRASH` (εμποδιστικό). Ο κύκλος δεν πεθαίνει ποτέ σιωπηλά. | Εξαίρεση σε οποιοδήποτε σημείο → record στο log |

### Φάση 4 — Regression που μετράει την παραγωγή (P1)

| ID | Εργασία | Κριτήριο αποδοχής |
|---|---|---|
| T4.1 | Κοινές συναρτήσεις: `compose_prompt`, `call_agent`, `extract_json`, `validate`, `apply_gates`. Το `run_regression.py` τις εισάγει αντί να τις ξαναγράφει. | Μία υλοποίηση για καθεμιά |
| T4.2 | Fixtures σε σχήμα παραγωγής, που παράγονται από το `collect_inputs` πάνω σε καταγεγραμμένα payloads Bybit (`tests/data/*.json`). Κατώφλια από το πραγματικό config. | Το prompt του regression = byte προς byte το prompt της παραγωγής για τα ίδια δεδομένα |
| T4.3 | Wrapper unit tests χωρίς LLM για: πύλες risk state, παλαίωση, ποσό, REDEEM, product id, `extract_json`, config, crash record. Το fixture 09 μετακινείται εδώ. | Τρέχουν σε δευτερόλεπτα |
| T4.4 | Το LLM regression κρατά μόνο ερωτήματα ποιότητας απόφασης (STAKE όταν πρέπει, HOLD όταν πρέπει), με το pinned μοντέλο και τα ίδια flags με την παραγωγή, 5 εκτελέσεις. | Αναφορά ανά fixture |

### Φάση 5 — Λειτουργία (P1/P2)

| ID | Εργασία |
|---|---|
| T5.1 | `deploy/`: systemd units (κύκλος ανά 10', heartbeat ανά 5'), `User=hermes`, script εγκατάστασης για τον Hermes. |
| T5.2 | Telegram: αποστολή μόνο σε αλλαγή κατάστασης, επανάληψη το πολύ ανά 6 ώρες. Ημερήσια σύνοψη: κύκλοι, ηλικία risk state, πλήθος NO_NEW_POSITIONS, πλήθος ανά κωδικό, εκτελέσεις. |
| T5.3 | Εντολές Telegram `/unwind` και `/resume`: επιβεβαίωση σε δεύτερο μήνυμα, γράφουν `source: operator`, δέχονται μόνο το επιτρεπόμενο chat id. |
| T5.4 | Διαγραφή raw session files μετά από 7 ημέρες. |
| T5.5 | `HANDOFF.md`: καθάρισμα, προσθήκη της §6, ενημέρωση ανά φάση. Ευθυγράμμιση του README με τον κώδικα. |

### Φάση 6 — Πύλη πριν τα χρήματα (Hermes + Giannis)

Με αυτή τη σειρά, κανένα βήμα δεν παραλείπεται:

1. Όλες οι εργασίες P0 και P1 merged, CI πράσινο.
2. Deploy από τον Hermes: `git pull`, μετάπτωση risk state στο νέο σχήμα, systemd από το `deploy/`, επαλήθευση ότι ο agent τρέχει χωρίς tools.
3. Πραγματικό κλειδί HMAC στη θέση του `smoke-test-only-do-not-use-in-prod`.
4. **Testnet:** πλήρης κύκλος Stake + Redeem μέσω `place-order` στο `api-testnet.bybit.com`, με record που δείχνει `orderId` και τελική κατάσταση Success.
5. Επτά ημέρες dry-run στο VPS, και audit.
6. Giannis: νέο κλειδί Bybit (μόνο Earn, χωρίς Withdraw, IP whitelist στο VPS), κεφάλαια στο UNIFIED, ποσό 2-3 φορές το `minStakeAmount`.
7. `DRY_RUN: false` — **μόνο από τον Giannis.**

---

## 5. Tests που γράφονται πρώτα

Όλα πρέπει να **αποτυγχάνουν** στο `17c7447` και να περνάνε μετά τη διόρθωση.

| Test | Είσοδος | Αναμενόμενο |
|---|---|---|
| `test_stake_blocked_under_no_new_positions` | risk NO_NEW_POSITIONS, το LLM λέει STAKE | Καμία STAKE εκτέλεση, alert `RISK_GATE_DROPPED_STAKE` |
| `test_unwind_redeems_without_llm` | risk UNWIND, ανοιχτή θέση | REDEEM_ALL, το LLM δεν καλείται |
| `test_stale_unwind_still_redeems` | UNWIND με έγκυρη υπογραφή, 2 ώρες παλιό | REDEEM_ALL |
| `test_bootstrap_promotes_after_clean_cycle` | Κανένα αρχείο → heartbeat → καθαρός κύκλος → heartbeat | NORMAL |
| `test_operator_state_never_promoted` | `source: operator`, NO_NEW_POSITIONS, καθαροί κύκλοι | Παραμένει NO_NEW_POSITIONS |
| `test_corrupt_state_not_overwritten` | `{"state":"UNWIND","ts":17` | Αρχείο ανέγγιχτο, alert |
| `test_abstain_does_not_write` | Παλιό NORMAL, κανένας κύκλος | `mtime` αμετάβλητο |
| `test_heartbeat_survives_malformed_state` | `sig: null`· χωρίς `profile` | Κανένα crash |
| `test_production_uses_configured_prompt` | `PROMPT_VERSION: v5` | Φορτώνεται το `prompt_v5.md`, sha256 στο record |
| `test_amount_never_exceeds_cap` | Υπόλοιπο 100, `MAX_PER_PRODUCT_USD` 5 | Ποσό ≤ 5 |
| `test_llm_type_slip_does_not_crash` | `amount_usd: "5"` | Record γράφεται, κανένα crash |
| `test_redeem_unavailable_product` | Θέση σε NotAvailable προϊόν | REDEEM χωρίς εμποδιστικό κωδικό |
| `test_prompt_example_not_executed` | stdout = παράδειγμα + πραγματική απάντηση HOLD | Εκτελείται το HOLD |
| `test_fallback_word_is_harmless` | Reason που περιέχει «fallback» | Κανένας κωδικός |
| `test_redemption_eta_from_bybit` | `redeemProcessingMinute: "2880"` | `redemption_eta_hours == 48.0` |
| `test_apr_history_order_independent` | Ιστορικό σε φθίνουσα σειρά | Σωστός μέσος 24 ωρών |
| `test_place_order_request_shape` | STAKE 5 USDT | `POST /v5/earn/place-order` με όλα τα πεδία του Παραρτήματος Α |
| `test_shared_env_cannot_override_keys` | `/opt/data/.env` με `BYBIT_API_KEY=` | Χρησιμοποιείται το κλειδί του profile |

---

## 6. Κλειδωμένες αποφάσεις στρατηγικής — δεν αλλάζουν

Calibration 180 ημερών, USDT: p25 0,70% · median 1,23% · p75 1,62% · max 2,89%.

| Παράμετρος | Τιμή | Γιατί |
|---|---|---|
| `ENTRY_APR` | 0.001 | Οτιδήποτε θετικό κερδίζει το αδρανές υπόλοιπο |
| `EXIT_APR` | 0 | Ένα προϊόν USDT: η εξαργύρωση λόγω πτώσης επιτοκίου στέλνει τα χρήματα στο 0% — χειρότερα από το να μείνουν |
| `MIN_APR_EDGE` | 0.009 | p75 − p25· αφορά μόνο δεύτερο προϊόν |
| `MAX_REDEMPTION_ETA_HOURS` | 2 | Όχι 0 — το 0 ενεργοποιεί έξοδο με την παραμικρή καθυστέρηση |
| `MAX_SCAN_AGE_SECONDS` | 900 | Ηλικία του ζωντανού scan |
| `MAX_APR_HISTORY_GAP_HOURS` | 4 | Ηλικία του APR history (ωριαία ανανέωση) |
| `COIN_WHITELIST` | `[USDT]` | Ποτέ μεταφορά μεταξύ διαφορετικών νομισμάτων |
| `RESOLVED_MODEL` | `google/gemini-2.5-flash` | Στέλνεται αυτούσιο στο CLI, όχι μέσω alias |
| `ACCOUNT_TYPE` | `UNIFIED` | — |
| `DRY_RUN` | `true` | Αλλάζει μόνο από τον Giannis, στη Φάση 6 |

---

## 7. Πότε θεωρείται ολοκληρωμένο

- Κάθε test της §5 περνά, στο CI, σε καθαρό clone.
- Ο διακόπτης κινδύνου (UNWIND) λειτουργεί χωρίς LLM και χωρίς να λήγει.
- Κανένα νούμερο που φτάνει στην εκτέλεση δεν έχει υπολογιστεί από το LLM.
- Το regression χρησιμοποιεί την ίδια διαδρομή κώδικα με την παραγωγή.
- Ένας πλήρης κύκλος Stake + Redeem πέτυχε στο testnet.
- Το `HANDOFF.md` περιγράφει την πραγματική κατάσταση του κώδικα.

---

## Παράρτημα Α — `POST /v5/earn/place-order` (επαληθευμένο στα docs της Bybit)

Χρειάζεται δικαίωμα **Earn** στο κλειδί.

| Πεδίο | Υποχρεωτικό | Τιμή για εμάς |
|---|---|---|
| `category` | ναι | `FlexibleSaving` |
| `orderType` | ναι | `Stake` ή `Redeem` |
| `accountType` | ναι | `UNIFIED` (από config) |
| `amount` | ναι | string |
| `coin` | ναι | `USDT` |
| `productId` | ναι | από το scan |
| `orderLinkId` | ναι | μοναδικό, προστασία από επανάληψη |

Η απόκριση περιέχει `orderId` και `orderLinkId`. Η εντολή εκτελείται ασύγχρονα — η επιτυχής απόκριση σημαίνει «έγινε δεκτή», όχι «ολοκληρώθηκε». Σε περιόδους υψηλής ζήτησης δανεισμού η εξαργύρωση μπορεί να πάρει έως 48 ώρες και δεν ακυρώνεται μετά την υποβολή.

## Παράρτημα Β — Χάρτης αρχείων (όπως στο `17c7447`)

| Αρχείο | Γραμμές | Κατάσταση |
|---|---|---|
| `run_yield_cycle.py` | 802 | K1, K4, K6-K14 |
| `heartbeat.py` | 455 | K2, K3, K15, K17, K19 |
| `bybit_earn_tool.py` | 446 | K5, K14, K16, K17, K20 |
| `tests/test_heartbeat.py` | 350 | K2 (όνομα ≠ έλεγχος), K18 |
| `tests/run_regression.py` | 299 | K12 |
| `tests/fixtures.py` | 258 | K12 |
| `executor.py` | 218 | K1, K7, K20 |
| `signing.py` | 26 | ✅ |
| `risk_state.py` | — | **Εκτός repo** — Hermes το ανεβάζει πρώτα |
