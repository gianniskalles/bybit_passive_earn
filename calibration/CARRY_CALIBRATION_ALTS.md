# CARRY_CALIBRATION_ALTS — altcoins (§13.10)

Πηγή: **bybit https://api.bybit.com**, λήψη 2026-09-29 13:19 UTC, 180 ημέρες. Layer A: USDT FlexibleSaving productId 1.

Κάθε altcoin κρίνεται **μόνο του** με τα κριτήρια της §2 και τα κατώφλια του `config/carry.yaml` (όχι βελτιστοποιημένα ανά νόμισμα). Κανένα altcoin στο `SYMBOLS` χωρίς δικό του GO· το config το επιβάλλει.

## Υποψήφια (top 15 USDT perps σε όγκο 24ώρου, εκτός BTC/ETH)

| Σύμβολο | Αποτέλεσμα |
|---|---|
| SOLUSDT | ✓ υποψήφιο (εισηγμένο 1811 ημ., collateral ratio 0.95) |
| ZECUSDT | ✗ no spot pair ZECUSDT on Bybit: /v5/market/instruments-info: no spot instrument ZECUSDT |
| XRPUSDT | ✓ υποψήφιο (εισηγμένο 1965 ημ., collateral ratio 0.95) |
| NEARUSDT | ✓ υποψήφιο (εισηγμένο 1815 ημ., collateral ratio 0.85) |
| QNTUSDT | ✓ υποψήφιο (εισηγμένο 1198 ημ., collateral ratio 0.7) |
| HYPEUSDT | ✓ υποψήφιο (εισηγμένο 664 ημ., collateral ratio 0.8) |
| HBARUSDT | ✓ υποψήφιο (εισηγμένο 1801 ημ., collateral ratio 0.8) |
| SOXLUSDT | ✗ listed 133 days < 6 months (183) |
| CLUSDT | ✗ no spot pair CLUSDT on Bybit: /v5/market/instruments-info: no spot instrument CLUSDT |
| PUMPFUNUSDT | ✗ no spot pair PUMPFUNUSDT on Bybit: /v5/market/instruments-info: no spot instrument PUMPFUNUSDT |
| LINKUSDT | ✓ υποψήφιο (εισηγμένο 3194 ημ., collateral ratio 0.9) |
| SUIUSDT | ✓ υποψήφιο (εισηγμένο 1246 ημ., collateral ratio 0.85) |
| XAUUSDT | ✗ no spot pair XAUUSDT on Bybit: /v5/market/instruments-info: no spot instrument XAUUSDT |
| ONDOUSDT | ✓ υποψήφιο (εισηγμένο 981 ημ., collateral ratio 0.8) |
| DOGEUSDT | ✓ υποψήφιο (εισηγμένο 1945 ημ., collateral ratio 0.9) |

Collateral από `/v5/spot-margin-trade/collateral` (δημόσιο· το σχήμα του δεν έχει επιβεβαιωθεί — αν δεν διαβαστεί, κανένα υποψήφιο).

## GO ανά altcoin (180 ημ., lagged predictor)

| Σύμβολο | Απόφαση | Υπεροχή | Χειρ. 30ήμ. | Υπεροχή ×1,5 | Είσοδοι | Χρόνος σε θέση | Αρνητικό funding | 2ο μισό: υπεροχή | Oracle |
|---|---|---|---|---|---|---|---|---|---|
| SOLUSDT | NO-GO (fails: excess, costs x1.5) | -1.04% | -0.27% | -1.51% | 2 | 10.74% | 38.15% | -2.07% | -1.01% |
| XRPUSDT | NO-GO (fails: excess, costs x1.5) | -0.54% | -0.06% | -1.01% | 2 | 17.04% | 40.00% | -1.07% | 0.29% |
| NEARUSDT | NO-GO (fails: excess, costs x1.5) | 0.42% | -0.19% | -0.37% | 3 | 59.44% | 23.33% | 2.56% | 0.15% |
| QNTUSDT | NO-GO (fails: excess, costs x1.5) | -2.65% | -0.49% | -4.85% | 7 | 77.41% | 17.04% | -1.06% | -0.25% |
| HYPEUSDT | NO-GO (fails: excess, costs x1.5) | -1.82% | -0.24% | -3.07% | 4 | 54.63% | 32.59% | -0.83% | -1.72% |
| HBARUSDT | NO-GO (fails: excess, costs x1.5) | 0.43% | 0.01% | 0.27% | 1 | 20.93% | 35.56% | 0.86% | 0.43% |
| LINKUSDT | NO-GO (fails: excess, costs x1.5) | 0.43% | 0.01% | 0.27% | 1 | 20.37% | 28.70% | 0.86% | 0.43% |
| SUIUSDT | NO-GO (fails: excess, costs x1.5) | -1.23% | -0.17% | -2.64% | 5 | 58.33% | 22.04% | -1.12% | -1.17% |
| ONDOUSDT | NO-GO (fails: excess, worst 30d, costs x1.5) | -2.55% | -0.57% | -4.28% | 6 | 61.85% | 26.94% | -0.76% | -2.47% |
| DOGEUSDT | NO-GO (fails: excess, costs x1.5) | 0.26% | 0.03% | 0.10% | 1 | 21.30% | 28.15% | 0.52% | 0.26% |

**Με GO:** κανένα.

## Κατώφλια της μέτρησης (`config/carry.yaml`)

```yaml
ENTRY_MIN_EXPECTED_APR: 0.05
ENTRY_MIN_PREDICTED_RATE: 5.0e-05
ENTRY_EV_MULTIPLE: 1
EXIT_PREDICTED_FLOOR: -0.0001
EXIT_HORIZON_HOURS: 168
SMOOTHING_SETTLEMENTS: 9
MIN_HOLD_HOURS: 336
MAX_ROUND_TRIPS_PER_30D: 2
NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN: 15
SPOT_TAKER_FEE: 0.001
PERP_TAKER_FEE: 0.00055
```

## Τι ΔΕΝ μοντελοποιείται

- Basis, spread, slippage — στα altcoins συνήθως μεγαλύτερα· η ευαισθησία ×1,5 είναι το υποκατάστατο.
- ADL και ρευστοποίηση: στα altcoins πιθανότερα (§5 R11, R13)· γι' αυτό `MAX_NOTIONAL_PER_ALT_USD` < `MAX_NOTIONAL_PER_SYMBOL_USD`.
- Delisting και αλλαγή collateral ratio (R16, R28): ελέγχονται σε κάθε κύκλο, όχι εδώ.
- Layer A όπως στο `CARRY_CALIBRATION.md` (ίδιο ιστορικό APR, ίδια περιορισμένη κάλυψη).
