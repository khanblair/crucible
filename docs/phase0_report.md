# Phase 0 Report — Base Strategy Proof

**Status: PENDING**

Nothing in Crucible goes live until this phase is complete. An optimizer
pointed at a strategy with no edge does not create an edge.

To produce this report:

```bash
python -m src.data --refresh          # pull ~3 years of Dukascopy EUR/USD data
python -m src.backtest --phase0       # full multi-year backtest of champion_zero
```

The command overwrites this file with the actual results. Pass criteria:

- Positive expectancy per trade after all costs across the full period
- Profit factor above 1.15
- No single year contributing more than half of total profit

If the base strategy fails, fix the strategy logic — do not hand it to the
optimizer. The automated workflows must remain disabled until this report
shows **PASS**.
