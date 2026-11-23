# Marshal e2e B

Crucible keeps one file per responsibility: `src/` holds the single-purpose pipeline modules (with swappable entry and exit choices under `src/modules/`), `config/` holds the genome and regime JSON, and `tests/` covers the evaluator and backtest harness. Automation runs through the workflows in `.github/workflows/` and may write only `config/regimes/`, `config/active.json`, `results/` and `docs/`, so everything else changes through human pull requests.
