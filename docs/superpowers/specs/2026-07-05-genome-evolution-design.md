# Crucible Genome Evolution — Design Spec

**Date:** 2026-07-05 · **Status:** Approved by user, pending implementation
**Author:** Blair Khan (design collaboration with Claude)

## 1. Motivation

Crucible today can only optimize the *numbers* inside one fixed strategy structure (RSI thresholds, ATR multipliers, etc.). Phase 0 showed that structure has no edge — expectancy is negative across every year of real data. No amount of parameter tuning can fix logic that doesn't work; Optuna will only ever find "the least-losing version" of a losing idea.

This spec adds a second, higher tier of self-improvement: the ability to try a genuinely different **strategy structure** — a different entry signal, a different exit style — search across those structural options the same rigorous way parameters are searched today, and, if one clearly earns it, propose switching to it. Every existing guarantee (determinism, out-of-sample gating, auditable git history, Champion Zero as the honest baseline) carries forward unchanged. What's new sits alongside the existing parameter-optimization loop, not in place of it.

## 2. Concepts

- **Module**: a small, human-authored, independently testable function implementing one *choice* for one stage of the strategy. E.g. `ema_pullback` and `breakout` are both modules for the entry-signal stage.
- **Slot**: a stage of the strategy that has more than one module to choose from. v1 has exactly two slots:
  - `entry_signal`: `ema_pullback` (today's logic, unchanged), `breakout`, `mean_reversion`
  - `exit_style`: `atr_trail_half` (today's logic, unchanged), `fixed_r_multiple`
- **Genome**: one full strategy structure — a specific choice per slot. v1 has 2 slots × (3 × 2) = **6 total genomes**. Every other piece of logic (1h trend confirmation, RSI filter, candle-quality filter, session filter) is shared by every genome and stays exactly as it is today — these are not modules, they don't vary.
- **Genome evolution**: the process of screening and testing genomes against each other, distinct from parameter optimization (which tunes numbers *within* one genome, exactly as today).

This is intentionally bounded. Nothing here prevents adding more slots or more choices per slot later — the module registry and genome encoding are built to extend, not to be exhaustive on day one.

## 3. Data model changes

**New folder `config/genomes/`** — one JSON file per genome, e.g. `config/genomes/baseline.json`:
```json
{
  "id": "baseline",
  "entry_signal": "ema_pullback",
  "exit_style": "atr_trail_half",
  "created": "2026-07-05",
  "created_by": "phase0-seed"
}
```

**`config/regimes/*.json`** gains a `"genome"` field alongside `"params"`:
```json
{
  "name": "ranging",
  "genome": "baseline",
  "last_validated": "...",
  "params": { ... }
}
```
All four regimes default to `"genome": "baseline"` in v1 — one shared genome across regimes, matching today's shared-structure model. The field exists per-regime so a future version could give different regimes different genomes without any schema change — that door is deliberately left open, but not walked through in v1.

**`config/champion_zero.json`** gains `"genome": "baseline"` too. Champion Zero is still one frozen genome + one frozen parameter set, forever — the comparison model (system-vs-system) is unchanged.

**`config/settings.json`** gains a new `"evolution"` block:
```json
"evolution": {
  "losing_streak_runs": 5,
  "stuck_improvement_epsilon": 0.02,
  "screen_top_k": 3,
  "structural_min_relative_improvement": 0.20,
  "cooldown_quarters": 1,
  "pr_auto_merge_hours": 24,
  "pr_reminder_hours_before": 0.5
}
```

## 4. Module registry

**`src/modules/entries.py`** — one function per entry-signal choice, same signature as today's inline logic in `strategy.py`: given 15m/1h frames and params, return candidate signals. `ema_pullback` is the existing logic moved here verbatim (no behavior change for `baseline`). `breakout` and `mean_reversion` are new, small, human-written implementations.

**`src/modules/exits.py`** — one function per exit-style choice: given a filled trade's context, decide stop/target/trailing behavior bar-by-bar. `atr_trail_half` is today's logic (TP1-half + breakeven + candle-low trail) moved here verbatim. `fixed_r_multiple` is a simpler new alternative (single target at a fixed R multiple, no partial/trailing).

**`src/genome.py`** — the registry (`ENTRY_SIGNALS`, `EXIT_STYLES` dicts mapping name → function), genome load/validate (schema check mirroring `strategy.validate_params`), and `assemble(genome)` which returns the pair of functions (entry-signal generator, exit-manager) to dispatch to for that genome.

**`backtest.py` gains an optional `genome` parameter** on both `run_backtest` and `simulate_trade` (defaulting to the `baseline` genome, so omitting it reproduces today's exact behavior byte-for-bit — required since existing tests call these without a genome argument). Internally, `run_backtest` dispatches signal generation to the genome's entry-signal module instead of always calling `strategy.generate_signals`, and `simulate_trade` dispatches its exit-management (stop/target/trailing decisions) to the genome's exit-style module instead of the hardcoded TP1-half-trail logic. Cost modeling (spread/slippage/swap), intrabar-ordering resolution, and metrics computation (`compute_metrics`) are untouched, shared by every genome exactly as design rule 2 (reuse through the harness, not through copies) requires — only *which* signals and *which* exit behavior feed into that one shared cost/metrics pipeline changes per genome.

## 5. Trigger: when genome evolution runs

At the start of every normal Sunday `optimize.yml` run, a cheap check (new function in `src/evolve.py`, reading existing `results/runs/decision_*.json` history — no new data source) evaluates:

- **Losing streak**: the last `losing_streak_runs` (5) decision records each show `baseline_oos.net_profit_pips <= 0`
- **Stuck optimizer**: comparing the earliest and most recent of those same 5 decision records, the best candidate's OOS net profit has not improved by more than `stuck_improvement_epsilon` (2%, relative) — i.e., the "improvement" gate has kept failing without even close near-misses over the whole window

Both true → the check dispatches the separate `evolve.yml` workflow (via `gh workflow run evolve` triggered from within `optimize.yml`, or an equivalent `workflow_dispatch` API call). Normal parameter optimization for the week proceeds unaffected — genome evolution is additive and runs as its own job.

**Evolution cooldown**: a genome-evolution attempt is not retried within `cooldown_quarters` (1 quarter) of the previous attempt, whether that attempt found a winner or not — tracked via a small `results/runs/evolution_<date>.json` record, checked the same way the losing-streak history is. This prevents PR spam if the losing streak persists for months.

## 6. The search: screen-then-refine funnel (`src/evolve.py`)

1. **Screen** — all 6 genomes, each backtested once with sensible default parameters (the current baseline's params, reused as-is) over the same training window `optimize.py` already computes (`split_windows`). Ranked by net profit. Cheap: no Optuna involved yet.
2. **Refine** — the top `screen_top_k` (3) screened genomes each get a full Optuna search, reusing `optimize.search()` unchanged except that the objective function now assembles trades via the genome's modules instead of always calling `strategy.generate_signals` directly.
3. **Evaluate** — the single best refined candidate (genome + its optimized params) is evaluated by the *unmodified* `evaluate.py` gates on held-out out-of-sample data, against the current baseline (today's active genome + params) — same five conditions, same out-of-sample discipline, same same-feed rule. One addition specific to structural candidates: `structural_min_relative_improvement` (20%, stricter than the 10% parameter-only hurdle) replaces the normal improvement gate's threshold, and the candidate must *also* beat Champion Zero outright (not only the current baseline) — a structural change is a bigger commitment than a parameter tweak and should clear a higher bar in both directions.

If no genome passes: clean termination, current genome kept, reported to Discord in plain English exactly like today's "no upgrade found" case, and the cooldown above prevents an immediate retry.

## 7. Auto-PR with 24-hour auto-merge

If a genome passes every gate, `evolve.yml`:
1. Creates a branch (e.g. `evolve/2026-07-05-mean-reversion`)
2. Commits the winning genome's file (if new) to `config/genomes/` and the regime's updated `"genome"` pointer to `config/regimes/<regime>.json`
3. Opens a PR via `gh pr create`, labeled `crucible-evolution`, with a body containing: the screen-stage ranking table, the refine winner's OOS metrics vs. baseline vs. Champion Zero, every gate's pass/fail detail, and the plain-English explanation (see §9)
4. Sends an immediate Discord notification that the PR was opened, with a link

**New workflow `pr_watch.yml`**, on a 15-minute cron, checks any open `crucible-evolution`-labeled PR's age:
- **~23.5 hours old** (`pr_auto_merge_hours - pr_reminder_hours_before`): posts a one-time Discord reminder ("30 minutes left to review PR #x before it auto-merges") — tracked via a bot-added label or comment so it fires exactly once per PR
- **24 hours old**: auto-merges via `gh pr merge --squash --delete-branch`, but **only if the `ci` status check is green** — human-reviewed-code guarantees are never bypassed, even on the timeout path

**Branch cleanup**: the repository-level "Automatically delete head branches" setting is enabled (covers PRs merged manually through the GitHub UI); the `--delete-branch` flag on the auto-merge command is belt-and-suspenders for the 24-hour timeout path specifically.

This is the one place the design departs from "the bot never opens PRs" — deliberately. Changing *which genome* a regime points to now requires a reviewed (or time-boxed auto-approved) pull request. Changing the *parameters* within the currently active genome remains exactly as autonomous as it is today — no new friction on the loop that's already proven itself.

## 8. Guardrail updates

- **Write-permission rule, refined**: `config/regimes/*.json`'s `"params"` field — direct commit by the weekly optimize workflow, unchanged. Its `"genome"` field — PR-only, whichever way the PR is resolved (human merge or 24-hour auto-merge).
- Champion Zero remains completely untouched by any of this.
- The existing cooldown (5 trading days, parameter changes) and the Champion Zero circuit breakers (90-day warning, 180-day suspension) apply unchanged to whatever genome is currently active — genome evolution doesn't bypass them, it just adds a new *kind* of candidate that flows through the same gates.

## 9. Reporting additions

**Plain-English** (`src/report.py`) gains a new case for when a genome PR is opened: *"The system tried a completely different way of trading and thinks it found something better. I opened a pull request for you to review — you have 24 hours, after which it merges automatically if you don't respond."* And for the reminder: *"Reminder: about 30 minutes left before PR #x auto-merges."*

**Dashboard** gains an "Evolution status" card: last trigger date (if any), the screen-stage ranking (simple bar chart, reusing the existing inline-SVG approach), the refine winner vs. current genome, and a live link to any open evolution PR with a countdown to auto-merge.

## 10. Testing

- **`tests/test_genome.py`**: each entry/exit module tested in isolation on fixture candles (same rigor as `test_backtest.py` — hand-computed expected signals/exits), genome load/validate schema checks (valid, missing field, unknown module name), and `assemble()` dispatch correctness.
- **`tests/test_evolve.py`**: trigger detection with hand-built `decision_*.json` fixtures — boundary cases (exactly 5 losses triggers, 4 does not; exactly at the epsilon threshold), the screen→refine top-K selection logic on tiny synthetic data, and the cooldown check (evolution attempted within the window is skipped, outside it is not).
- Both files are picked up automatically by the existing `ci.yml` — no workflow changes needed there.

## 11. New/changed files (summary)

| File | Change |
|---|---|
| `src/modules/entries.py` | new — entry-signal modules |
| `src/modules/exits.py` | new — exit-style modules |
| `src/genome.py` | new — registry, validation, assembly |
| `src/backtest.py` | add optional `genome` param to `run_backtest`/`simulate_trade`, defaulting to today's exact behavior |
| `src/strategy.py` | `ema_pullback`/`atr_trail_half` logic moves to the new module files verbatim; `strategy.py` keeps only the fixed filters (RSI, candle quality, session, 1h trend) and `validate_params` |
| `src/evolve.py` | new — trigger detection, screen-then-refine funnel, PR creation |
| `config/genomes/*.json` | new folder — one file per genome |
| `config/regimes/*.json` | add `"genome"` field |
| `config/champion_zero.json` | add `"genome": "baseline"` |
| `config/settings.json` | add `"evolution"` block |
| `.github/workflows/evolve.yml` | new — runs the funnel, opens the PR |
| `.github/workflows/pr_watch.yml` | new — 15-min cron, reminder + auto-merge |
| `.github/workflows/optimize.yml` | add trigger-check step that dispatches `evolve.yml` |
| `src/report.py` | plain-English cases + dashboard evolution card |
| `docs/_template.html` | Evolution status card |
| `tests/test_genome.py`, `tests/test_evolve.py` | new |
| `CRUCIBLE.md`, `CRUCIBLE_STRUCTURE.md` | document the new tier |

## 12. Non-goals (explicitly out of scope for this spec)

- Per-regime genomes (each regime having its own structure) — the data model supports it later, but v1 ships one shared genome across all regimes.
- AI-proposed strategy logic (an LLM writing new modules) — modules are only ever human-authored in this design.
- A true genetic algorithm (population/crossover/mutation across generations) — the screen-then-refine funnel is a bounded, deterministic search over a curated menu, not an evolving population. Revisit only after this model is proven.
