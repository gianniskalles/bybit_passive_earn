# CARRY_MARKET_SAMPLE — basis & spread (Φάση 2)

Πηγή: **bybit https://api.bybit.com**, 23.99 ώρες δειγματοληψίας.

| Σύμβολο | Δείγματα | Απορρίφθηκαν | |basis| p50 / p95 / p99 / max (bps) | spread p50 / p95 / p99 / max (bps) |
|---|---|---|---|---|
| BTCUSDT | 1422 | 0 | 4.73 / 5.69 / 6.19 / 7.14 | 0.01 / 0.01 / 0.01 / 0.48 |
| ETHUSDT | 1422 | 0 | 4.52 / 5.76 / 6.60 / 8.47 | 0.04 / 0.04 / 0.04 / 1.04 |

Κανόνας: `MAX_ENTRY_BASIS_BPS` = ceil(p95 |basis|), `MAX_SPREAD_BPS` = ceil(p99 spread), το μέγιστο των συμβόλων, ≥ 1.

```yaml
MAX_ENTRY_BASIS_BPS: 6
MAX_SPREAD_BPS: 1
```
