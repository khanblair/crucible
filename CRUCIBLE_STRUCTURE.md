# Crucible — Repository Structure

**Version:** 1.5 · **Principle:** one file per responsibility, nothing duplicated, nothing decorative.

---

## Folder Tree

```
crucible/
├── .github/
│   └── workflows/
│       ├── monitor.yml            # Every 8h, 00:00/08:00/16:00 EAT, every day (≤10 min)
│       ├── optimize.yml           # Every 8h, 30 min offset from monitor.yml + decay
│       │                          #   triggers (≤60 min); also runs the evolution trigger check
│       ├── ci.yml                 # Test suite + compile on every PR and code push to main
│       │                          #   (bot data/result commits are skipped)
│       ├── evolve.yml             # Genome evolution: screen → refine → gate → open PR
│       │                          #   (dispatched by optimize.yml's trigger check, not scheduled)
│       └── pr_watch.yml           # 15-min cron: reminder at ~23.5h, auto-merge + branch delete at 24h
│                                  #   on any open `crucible-evolution` PR
│
├── config/
│   ├── champion_zero.json         # Frozen original genome + parameters — NEVER modified
│   ├── active.json                # Pointer to the currently selected regime
│   ├── genomes/                   # Human-authored strategy structures — PR-only, never direct commit
│   │   └── baseline.json          #   today's entry_signal + exit_style combination
│   └── regimes/                   # Bot may write `params` directly; `genome` field is PR-only
│       ├── trending.json
│       ├── ranging.json
│       ├── high_volatility.json
│       └── low_volatility.json
│
├── src/
│   ├── data.py                    # Fetch, clean, resample; Dukascopy primary, Twelve Data
│   │                              #   fallback (confirmed-outage only, always flagged) and
│   │                              #   context; staleness check; feed reconciliation; offline
│   │                              #   mode builds the intrabar table
│   ├── strategy.py                # Fixed filters only: RSI, candle quality, session, 1h trend
│   │                              #   confirmation, and validate_params (human-edited only)
│   ├── modules/
│   │   ├── entries.py             # Entry-signal choices: ema_pullback, breakout, mean_reversion
│   │   └── exits.py               # Exit-style choices: atr_trail_half, fixed_r_multiple
│   ├── genome.py                  # Module registry, genome load/validate, assemble() dispatch
│   ├── backtest.py                # Harness: costs, slippage, swap, intrabar ordering, all metrics;
│   │                              #   optional `genome` param dispatches entry/exit modules
│   ├── regime.py                  # Deterministic ADX/ATR precedence matrix → picks regime config
│   ├── optimize.py                # Optuna Bayesian search over the 6 parameters (genome-aware)
│   ├── evolve.py                  # Losing-streak/stuck-optimizer trigger, screen-then-refine
│   │                              #   funnel over genomes, opens the evidence-backed PR
│   ├── evaluate.py                # Gatekeeper: all accept/reject checks (human-edited only)
│   ├── forward.py                 # Paper-forward signal log + divergence triggers & escalation
│   └── report.py                  # DeepSeek summary, plain-English summary, Discord embed,
│                                  #   GitHub Pages build
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
├── docs/                          # GitHub Pages source
│   ├── index.html                 # Dashboard — auto-rebuilt from _template.html every run
│   ├── _template.html             # Dashboard template (human-edited; report.py fills in the data)
│   ├── architecture.html          # Mind map of the system — static, human-maintained
│   ├── knowledge-base.html        # Glossary + FAQ, with a client-side search filter — static
│   ├── assets/style.css           # Shared design tokens, nav bar, and components for all 3 pages
│   └── phase0_report.md           # The base-strategy proof, committed once
│
├── tests/
│   ├── test_evaluate.py           # Every gate: passing, failing, and boundary case
│   ├── test_backtest.py           # Fixture candles with hand-computed outcomes: cost application,
│   │                              #   swap, and worst-case intrabar fallback when no tick record exists
│   ├── test_genome.py             # Each entry/exit module tested in isolation on fixture candles;
│   │                              #   registry validation
│   ├── test_evolve.py             # Trigger detection boundary cases; screen→refine ranking logic;
│   │                              #   evolution cooldown
│   ├── test_data.py               # refresh() advancement detection, rejected-vs-empty diagnostics,
│   │                              #   Twelve Data fallback, tz-mismatch regression
│   └── test_report.py             # EAT conversion, data-freshness field
│
├── .python-version                # Pinned Python version — runner defaults can never shift
├── requirements.txt               # Pinned dependency versions only
├── README.md                      # Points to CRUCIBLE.md for full design
├── CRUCIBLE.md                    # The design document (v1.5)
└── .gitignore                     # Excludes raw tick downloads, caches, .env
```

**Total: 10 source modules + 1 module registry package, 5 workflows (2 scheduled + 1 CI gate + 1 evolution + 1 PR watch), 6 test files.** No packages-within-packages beyond `src/modules/` (the one deliberate exception: it exists specifically to hold interchangeable, independently swappable implementations — the reason abstraction is justified here at all), no utils dumping ground, no abstractions until a second consumer exists.

Empty directories (`data/candles/`, `data/intrabar/`, `results/runs/`, `results/forward_log/`) hold a `.gitkeep` placeholder so they exist immediately after clone — git cannot track empty folders, and the workflows must be able to write into them on the very first run. Each `.gitkeep` becomes redundant once real files land beside it.

---

## Design Rules

1. **One module, one job.** Each file in `src/` owns exactly one stage of the loop. If a file needs a second sentence to describe, it should be split — but not before.
2. **Reuse through the harness, not through copies.** Cost modeling, intrabar ordering, and metric calculations live only in `backtest.py`. `optimize.py`, `evaluate.py`, and `forward.py` all call it — three consumers, one implementation, zero drift between training, gating, and forward results.
3. **Keep files small.** Target under ~150 lines per module. The strategy has 6 tunable parameters and fixed logic; the code should look like it. If `strategy.py` outgrows the target once indicators, signals, and exits are real, split it then — not preemptively.
4. **Configuration over code.** All thresholds (gate margins, cost assumptions, regime cutoffs, divergence triggers, cooldowns) live in config files, never hardcoded. Changing a threshold is a diff in JSON, not a code review.
5. **Ticks stay offline.** Raw Dukascopy tick data (gigabytes) is processed once, offline, by `data.py --build-intrabar` into the committed kilobyte-scale `data/intrabar/ordering.parquet`. The workflows never download, store, or touch raw ticks.
6. **Config writes are atomic.** A regime parameter file and `active.json` are always updated in the same commit, and both workflows begin by validating that the pointer targets an existing, schema-valid file. Half-updated configuration is impossible by construction.
7. **Write permissions mirror the folder tree.** The workflow may write only `config/regimes/`, `config/active.json`, `results/`, and `docs/`. Everything else is read-only to automation and changes only via human pull request — with one deliberate carve-out: `evolve.yml` may open a pull request (never commit directly) touching `config/genomes/` and a regime's `genome` field, subject to the 24-hour review window in rule 12.
8. **Append, never overwrite, in `results/`.** Logs are the audit trail; the only files the bot overwrites are regime parameter JSONs and the pointer.
9. **Secrets stay in GitHub Actions.** `TWELVE_DATA_KEY`, `DEEPSEEK_KEY`, `DISCORD_WEBHOOK` — never in code, never in config, never in the repo.
10. **Test what decides.** Two components can silently corrupt everything downstream, and both get dedicated tests. `evaluate.py`: every gate condition with a passing case, a failing case, and a boundary case. `backtest.py`: small constructed candle fixtures with hand-computed correct outcomes, assertions that spread/slippage/swap are applied, and proof that the worst-case intrabar fallback triggers when a bar has no tick record. A bug in the harness corrupts the optimizer, the gate, and the forward log identically — which is exactly why it cannot go untested.
11. **Two reviewers, clearly divided.** Automated parameter changes are reviewed by the Evaluator (deterministic, tested) and land directly on main; human code changes are reviewed by CI — `ci.yml` runs the full test suite and compiles every module on each pull request and each code push to main, and skips the bot's data/result commits so routine appends cost nothing. No human change to `strategy.py`, `evaluate.py`, or the workflows reaches main untested.
12. **A third tier for structural change, time-boxed rather than open-ended.** A genome that clears every Evaluator gate — at a stricter bar than a parameter change — does not commit directly; it opens a labeled pull request with full evidence and pings Discord immediately. `pr_watch.yml` reminds at ~23.5 hours and auto-merges (CI-gated, branch auto-deleted) at 24 hours if no one has acted. This sits between full autonomy and normal human-paced review: a structural change always leaves a reviewable trail, but the system does not stall waiting on a human who may not be watching.
13. **Modules are swappable, not generative.** `src/modules/entries.py` and `src/modules/exits.py` hold every choice genome evolution can select between — a small, fixed, human-written menu. Nothing in the search ever writes new logic; it only recombines and parameter-tunes what a human already authored and tested.
14. **Docs pages are static unless the automation itself writes them.** `index.html` is the one exception — `report.py` rebuilds it from `_template.html` after every run, so hand-edit the template, never the built file. `architecture.html` and `knowledge-base.html` are ordinary committed pages with no build step; update them by hand when the system changes shape. All three share `assets/style.css` (design tokens, the nav bar, common components) so a new page only needs its own content and a small page-specific `<style>` block for anything genuinely unique to it.
15. **A stalled feed must say so, never fake progress.** `data.py`'s `refresh()` only ever reports success when the committed data's last date genuinely advanced — re-fetching an already-covered day and getting the same bytes back is reported as no progress, not success. A non-200 response is logged as `rejected` (the feed pushing back) and is never folded into the same silent bucket as a genuine `empty` (200, no ticks — an ordinary holiday); conflating the two is exactly what let a real Dukascopy outage look identical to a quiet weekend. `check_staleness()` is a pure read (no network call) so reporting code can always ask "how stale is this?" without triggering a fetch.
16. **Fallback data is real, confirmed-need-only, and never silent.** `fetch_twelve_data_range()` bridges a gap only after Dukascopy has shown a `rejected`/`unreachable` day AND a genuine trading day is still missing afterward — never merely because a day came back empty (that's routine). Every fallback-sourced bar is flagged in the refresh summary and called out by name in both the Discord report and the dashboard banner.
17. **Internal logic stays UTC; only display converts to EAT.** `report.py`'s `now_eat()` (fixed UTC+3, no DST) is used exclusively for human-facing timestamps — Discord footers, the dashboard's "Generated" time, "next run" text. Every internal calculation (walk-forward windows, the cooldown's trading-day count, regime classification, cron schedules themselves) stays in UTC, so a display-layer timezone choice can never silently shift an actual decision boundary.

---

## Run Flow Through the Files

```
monitor.yml   →  data.py --refresh (Dukascopy; Twelve Data fallback if confirmed down)
                 → regime.py (swap active.json if regime changed — routing, no cooldown)
                 → strategy.py + genome.py (active genome's modules) → forward.py → report.py
                 (report.py surfaces data staleness whenever stale_trading_days > 0)

optimize.yml  →  data.py --refresh → regime.py → evolve.py (trigger check only)
                 → optimize.py ⇄ backtest.py → evaluate.py ⇄ backtest.py
                 → (single commit: regime `params` + active.json) → report.py
                 → if triggered: dispatch evolve.yml

evolve.yml    →  data.py → evolve.py: screen (all genomes, default params) → refine (top-K via
                 optimize.py ⇄ backtest.py) → evaluate.py ⇄ backtest.py (stricter structural gate)
                 → if passed: open PR (config/genomes/*.json + regime `genome` field) → Discord ping

pr_watch.yml  →  every 15 min: check open `crucible-evolution` PR age
                 → ~23.5h: one-time Discord reminder
                 → 24h: gh pr merge (CI-gated) --delete-branch

offline, on dataset refresh:
                 data.py --build-intrabar  →  data/intrabar/ordering.parquet

ci.yml        →  pytest (all gates + harness + genome fixtures) → compileall
                 (every pull request; every code push to main)
```

Two scheduled entry points, one shared harness, one gate, one evidence-backed evolution path with a human-or-24h reviewer, and a CI gate on every human change. Nothing else.
