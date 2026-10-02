# CARRY_MARKET_SAMPLE — basis & spread (Φάση 2)

Πηγή: **bybit https://api.bybit.com**, 23.99 ώρες δειγματοληψίας.

| Σύμβολο | Δείγματα | Απορρίφθηκαν | |basis| p50 / p95 / p99 / max (bps) | spread p50 / p95 / p99 / max (bps) |
|---|---|---|---|---|
| BTCUSDT | 1422 | 0 | 4.73 / 5.69 / 6.19 / 7.14 | 0.01 / 0.01 / 0.01 / 0.48 |
| ETHUSDT | 1422 | 0 | 4.52 / 5.76 / 6.60 / 8.47 | 0.04 / 0.04 / 0.04 / 1.04 |

basis με πρόσημο (> 0 = perp πάνω από το spot, ευνοϊκό για το short):

| Σύμβολο | min / p1 / p5 / p50 / p95 / p99 / max (bps) | Αρνητικό basis |
|---|---|---|
| BTCUSDT | -7.14 / -6.19 / -5.69 / -4.74 / -3.78 / -3.29 / -2.44 | 100.0% |
| ETHUSDT | -8.47 / -6.60 / -5.76 / -4.52 / -3.30 / -2.64 / -2.12 | 100.0% |

Κανόνας: `MAX_ENTRY_BASIS_BPS` = ceil(p95 |basis|), `MAX_SPREAD_BPS` = ceil(p99 spread), το μέγιστο των συμβόλων, ≥ 1.
Ο έλεγχος εισόδου είναι μονόπλευρος: απόρριψη μόνο όταν basis < −`MAX_ENTRY_BASIS_BPS` ή basis > `MAX_FAVORABLE_BASIS_BPS` (όριο λογικής, χαλασμένα δεδομένα).

```yaml
MAX_ENTRY_BASIS_BPS: 6
MAX_SPREAD_BPS: 1
```
