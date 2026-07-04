# Crucible — Repository Structure

**Version:** 1.1 · **Principle:** one file per responsibility, nothing duplicated, nothing decorative.

---

## Folder Tree

```
crucible/
├── .github/
│   └── workflows/
│       ├── monitor.yml            # Daily monitoring run (weekdays, ≤10 min)
│       ├── optimize.yml           # Weekly optimization run (Sundays + decay triggers, ≤60 min)
│       └── ci.yml                 # Test suite + compile on every PR and code push to main
│                                  #   (bot data/result commits are skipped)
│
├── config/
│   ├── champion_zero.json         # Frozen original parameters — NEVER modified
│   ├── active.json                # Pointer to the currently selected regime
│   └── regimes/                   # The ONLY folder the bot may write to (with active.json)
│       ├── trending.json
│       ├── ranging.json
│       ├── high_volatility.json
│       └── low_volatility.json
│
├── src/
│   ├── data.py                    # Fetch, clean, resample (Dukascopy + Twelve Data), feed
│   │                              #   reconciliation; offline mode builds the intrabar table
│   ├── strategy.py                # Fixed strategy logic: signals, entries, exits (human-edited only)
│   ├── backtest.py                # Harness: costs, slippage, swap, intrabar ordering, all metrics
│   ├── regime.py                  # Deterministic ADX/ATR precedence matrix → picks regime config
│   ├── optimize.py                # Optuna Bayesian search over the 6 parameters
│   ├── evaluate.py                # Gatekeeper: all accept/reject checks (human-edited only)
│   ├── forward.py                 # Paper-forward signal log + divergence triggers & escalation
│   └── report.py                  # DeepSeek summary, Discord embed, GitHub Pages build
│
├── data/
│   ├── candles/                   # Resampled 15m / 1h bars (decision data)
│   └── intrabar/
│       └── ordering.parquet       # Committed lookup table: which level was touched first per bar,
│                                  #   built offline from Dukascopy ticks (raw ticks NEVER committed)
│
├── results/
│   ├── runs/                      # One JSON log per run (append-only)
│   └── forward_log/               # Daily hypothetical signals + outcomes
│
├── docs/                          # GitHub Pages source (auto-rebuilt each run)
│   ├── index.html                 # Dashboard (single self-contained page)
│   └── phase0_report.md           # The base-strategy proof, committed once
│
├── tests/
│   ├── test_evaluate.py           # Every gate: passing, failing, and boundary case
│   └── test_backtest.py           # Fixture candles with hand-computed outcomes: cost application,
│                                  #   swap, and worst-case intrabar fallback when no tick record exists
│
├── .python-version                # Pinned Python version — runner defaults can never shift
├── requirements.txt               # Pinned dependency versions only
├── README.md                      # Points to CRUCIBLE.md for full design
├── CRUCIBLE.md                    # The design document (v1.0)
└── .gitignore                     # Excludes raw tick downloads, caches, .env
```

**Total: 8 source modules, 3 workflows (2 scheduled + 1 CI gate), 2 test files.** No packages-within-packages, no utils dumping ground, no abstractions until a second consumer exists.

Empty directories (`data/candles/`, `data/intrabar/`, `results/runs/`, `results/forward_log/`) hold a `.gitkeep` placeholder so they exist immediately after clone — git cannot track empty folders, and the workflows must be able to write into them on the very first run. Each `.gitkeep` becomes redundant once real files land beside it.

---

## Design Rules

1. **One module, one job.** Each file in `src/` owns exactly one stage of the loop. If a file needs a second sentence to describe, it should be split — but not before.
2. **Reuse through the harness, not through copies.** Cost modeling, intrabar ordering, and metric calculations live only in `backtest.py`. `optimize.py`, `evaluate.py`, and `forward.py` all call it — three consumers, one implementation, zero drift between training, gating, and forward results.
3. **Keep files small.** Target under ~150 lines per module. The strategy has 6 tunable parameters and fixed logic; the code should look like it. If `strategy.py` outgrows the target once indicators, signals, and exits are real, split it then — not preemptively.
4. **Configuration over code.** All thresholds (gate margins, cost assumptions, regime cutoffs, divergence triggers, cooldowns) live in config files, never hardcoded. Changing a threshold is a diff in JSON, not a code review.
5. **Ticks stay offline.** Raw Dukascopy tick data (gigabytes) is processed once, offline, by `data.py --build-intrabar` into the committed kilobyte-scale `data/intrabar/ordering.parquet`. The workflows never download, store, or touch raw ticks.
6. **Config writes are atomic.** A regime parameter file and `active.json` are always updated in the same commit, and both workflows begin by validating that the pointer targets an existing, schema-valid file. Half-updated configuration is impossible by construction.
7. **Write permissions mirror the folder tree.** The workflow may write only `config/regimes/`, `config/active.json`, `results/`, and `docs/`. Everything else is read-only to automation and changes only via human pull request.
8. **Append, never overwrite, in `results/`.** Logs are the audit trail; the only files the bot overwrites are regime parameter JSONs and the pointer.
9. **Secrets stay in GitHub Actions.** `TWELVE_DATA_KEY`, `DEEPSEEK_KEY`, `DISCORD_WEBHOOK` — never in code, never in config, never in the repo.
10. **Test what decides.** Two components can silently corrupt everything downstream, and both get dedicated tests. `evaluate.py`: every gate condition with a passing case, a failing case, and a boundary case. `backtest.py`: small constructed candle fixtures with hand-computed correct outcomes, assertions that spread/slippage/swap are applied, and proof that the worst-case intrabar fallback triggers when a bar has no tick record. A bug in the harness corrupts the optimizer, the gate, and the forward log identically — which is exactly why it cannot go untested.
11. **Two reviewers, clearly divided.** Automated parameter changes are reviewed by the Evaluator (deterministic, tested) and land directly on main; human code changes are reviewed by CI — `ci.yml` runs the full test suite and compiles every module on each pull request and each code push to main, and skips the bot's data/result commits so routine appends cost nothing. No human change to `strategy.py`, `evaluate.py`, or the workflows reaches main untested.

---

## Run Flow Through the Files

```
monitor.yml   →  data.py → regime.py (swap active.json if regime changed — routing, no cooldown)
                 → strategy.py → forward.py → report.py

optimize.yml  →  data.py → regime.py → optimize.py ⇄ backtest.py
                 → evaluate.py ⇄ backtest.py
                 → (single commit: regime file + active.json) → report.py

offline, on dataset refresh:
                 data.py --build-intrabar  →  data/intrabar/ordering.parquet

ci.yml        →  pytest (all gates + harness fixtures) → compileall
                 (every pull request; every code push to main)
```

Two scheduled entry points, one shared harness, one gate — and a CI gate on the humans. Nothing else.
