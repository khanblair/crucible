# Crucible

An autonomous trading strategy perfection engine for EUR/USD (15m + 1h).
It backtests under a realistic execution-cost model, searches for better
parameters with Optuna on a disciplined weekly cadence, gates every change
through a deterministic Evaluator, and reports through Discord and a GitHub
Pages dashboard. It does **not** place live trades.

**Full design:** [CRUCIBLE.md](CRUCIBLE.md) · **Repository layout:** [CRUCIBLE_STRUCTURE.md](CRUCIBLE_STRUCTURE.md)

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                                # gates + harness must be green
```

Configure three repository secrets (Settings → Secrets → Actions):

| Secret | Purpose |
|---|---|
| `TWELVE_DATA_KEY` | Twelve Data context candles (display only) |
| `DEEPSEEK_KEY` | DeepSeek report prose (no decision role) |
| `DISCORD_WEBHOOK` | Run notifications |

## Phase 0 — required before anything runs

```bash
python -m src.data --refresh          # ~3 years of Dukascopy EUR/USD candles
python -m src.backtest --phase0       # writes docs/phase0_report.md
```

The scheduled workflows must stay disabled until the report shows **PASS**.
Optionally build the intrabar-ordering table from locally downloaded raw
ticks (never committed): `python -m src.data --build-intrabar <ticks_dir>`.
Without it, the engine uses worst-case ordering (stop assumed hit first).

## Operation

- `monitor.yml` — weekdays 21:30 UTC: refresh data, classify regime (swap
  `active.json` if it changed), log paper-forward signals, report.
- `optimize.yml` — Sundays 22:00 UTC: Optuna search on the training window,
  Evaluator gate on rolling out-of-sample data, atomic config commit, report.

Everything the system decides is committed to this repository — the repo is
the single source of truth and the complete audit trail.
