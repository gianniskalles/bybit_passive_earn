# CARRY_CALIBRATION — Φάση 0Β

Πηγή δεδομένων: **bybit https://api.bybit.com**, λήψη 2026-09-27 09:03 UTC, 179.7 ημέρες. Layer A: USDT FlexibleSaving productId 1

⚠️ Layer A: το ιστορικό APR καλύπτει μόνο 6.9 από 179.7 ημέρες· για τις υπόλοιπες 172.8 χρησιμοποιήθηκε η πρώτη διαθέσιμη τιμή (η Bybit δεν δίνει μεγαλύτερο ιστορικό).

## Απόφαση: **NO-GO**

| Κριτήριο (§2) | Τιμή | Όριο | |
|---|---|---|---|
| Καθαρό APR στρατηγικής − layer A (180 ημ., lagged) | 0.00% | ≥ 3.00% | ❌ |
| Χειρότερο 30ήμερο (καθαρό, επί του κεφαλαίου) | 0.14% | ≥ -0.50% | ✅ |
| Με έξοδα × 1,5: υπεροχή / χειρότερο 30ήμερο | 0.00% / 0.14% | ίδια όρια | ❌ |

Out-of-sample (δεύτερο μισό, παράμετροι επιλεγμένες στο πρώτο): υπεροχή **0.00%**, χειρότερο 30ήμερο 0.14%. Συνδυασμοί του grid που περνούν το GO: 0/324.

## Ανά σύμβολο (180 ημ., lagged predictor)

| Σύμβολο | Καθαρό APR | Layer A | Υπεροχή | Round trips | Χρόνος σε θέση | Χειρ. 30ήμ. | Αρνητικό funding | Μεγαλύτερο αρνητικό διάστημα | Υπεροχή με έξοδα ×1,5 |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 1.73% | 1.73% | 0.00% | 0 (0 είσοδοι) | 0.00% | 0.14% | 25.56% των settlements | 4.7 ημ. | 0.00% |
| ETHUSDT | 1.73% | 1.73% | 0.00% | 0 (0 είσοδοι) | 0.00% | 0.14% | 26.11% των settlements | 4.3 ημ. | 0.00% |
| **Σύνολο** (ίση κατανομή) | 1.73% | 1.73% | 0.00% | 0 | 0.00% | 0.14% | 25.83% | 4.7 ημ. | 0.00% |

Με τον «oracle» predictor (επιτόκιο που τελικά πληρώθηκε): υπεροχή 0.00%.

## Προτεινόμενες παράμετροι (⊙ της §8)

```yaml
ENTRY_MIN_EXPECTED_APR: 0.03
ENTRY_MIN_PREDICTED_RATE: 5.0e-05
EXIT_PREDICTED_FLOOR: -0.0001
EXIT_HORIZON_HOURS: 72
SMOOTHING_SETTLEMENTS: 3
MIN_HOLD_HOURS: 72
MAX_ROUND_TRIPS_PER_30D: 2
NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN: 15
MAX_ENTRY_BASIS_BPS: null
MAX_SPREAD_BPS: null
SPOT_TAKER_FEE: 0.001
PERP_TAKER_FEE: 0.00055
```

## Τι ΔΕΝ μοντελοποιείται

- Basis, spread και slippage κατά την είσοδο/έξοδο — μόνο taker fees· η ευαισθησία ×1,5 είναι το υποκατάστατο.
- Οι χρεώσεις είναι οι fallback της §8 (οι πραγματικές θέλουν κλειδί: `/v5/account/fee-rate`).
- Αποφάσεις μόνο μία φορά ανά settlement (16′ πριν)· ο πραγματικός κύκλος είναι κάθε 5′.
- Το «predicted rate» δεν υπάρχει στο ιστορικό· ο lagged predictor (προηγούμενο settlement) κρίνει το GO.
- Layer A = ιστορικό APR του USDT FlexibleSaving· αν περιέχει μπόνους που το BYUSDT δεν παίρνει, το layer A υπερεκτιμάται (άρα η υπεροχή υποεκτιμάται).
- Κίνδυνοι margin, ADL, ρευστοποίησης, πλατφόρμας: εκτός αυτής της μέτρησης (§5).

## Τρέχοντα στοιχεία συμβόλων

- BTCUSDT: status Trading, fundingInterval 480 min, qtyStep 0.001, minOrderQty 0.001
- ETHUSDT: status Trading, fundingInterval 480 min, qtyStep 0.01, minOrderQty 0.01
