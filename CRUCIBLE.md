# Crucible
## An Autonomous Trading Strategy Perfection Engine

**Target Market:** EUR/USD (15-Minute and 1-Hour charts)
**Built By:** Blair Khan
**Version:** 1.1 — July 2026
**Stack:** GitHub Actions · Optuna · DeepSeek · Discord · GitHub Pages · Twelve Data API · Dukascopy

---

## What Is Crucible?

A crucible is a container used to apply extreme heat to raw material until only the purest, strongest form survives. That is exactly what this system does to trading strategy parameters.

Crucible is a self-running software engine built around a defined EUR/USD trading strategy. It automatically monitors that strategy's performance, tests new parameter configurations on a controlled schedule, keeps only the ones that are genuinely and measurably better, and throws away everything else. No human needs to sit at a computer adjusting numbers. The system does the work on schedule, documents every decision, and reports back through Discord.

It is not a trading bot that places live orders. It is a strategy improvement engine that runs backtests on real market data, finds better parameter combinations, and updates its own configuration only when the evidence clearly supports doing so.

Crucible is built on a testable claim, not an assumption: that adaptive parameters beat static ones. It does not take that claim on faith — it measures it continuously against a frozen baseline (see Champion vs. Challenger Tracking below) and is designed to admit it if the claim turns out to be false.

---

## The Core Problem It Solves

Most trading strategies are written once and left unchanged. Markets shift over time. A strategy tuned for a trending market will underperform in a ranging market. RSI thresholds that worked six months ago may let in too many bad trades today. ATR multipliers that sized stop-losses correctly in low-volatility conditions will be too tight in high-volatility periods.

The usual response is for a human trader to sit down periodically and manually adjust the strategy parameters. This is slow, inconsistent, and dependent on the trader having the time and discipline to do it properly.

Crucible removes that dependency. The system notices performance decay, generates candidate improvements, tests them rigorously against real historical data with realistic execution costs, and only accepts a change if it passes a strict series of checks. The human engineer reviews a summary in Discord rather than doing the optimization work manually.

---

## Phase 0 — Proving the Base Strategy First

**Nothing in Crucible goes live until this phase is complete.** An optimizer pointed at a strategy with no edge does not create an edge — it simply finds the least-losing configuration in-sample, with impressive-looking rigor. Phase 0 exists to prevent that.

Before the automated loop is ever enabled, the base strategy with its initial hand-set parameters must be run through a single, full multi-year backtest under the realistic execution model described below (spread, slippage, swap, intrabar ordering). The backtest must report, at minimum:

- **Expectancy per trade** (average profit per trade after all costs)
- **Profit factor** (gross profit divided by gross loss)
- **Maximum drawdown** over the full period
- **Total trade count**, broken down by year and by market regime
- **Equity curve** across at least three distinct market environments (trending, ranging, high-volatility)

The pass criteria are simple: positive expectancy after costs across the full period, a profit factor above 1.15, and no single year contributing more than half of total profit. If the base strategy fails Phase 0, the correct response is to fix the strategy logic — not to hand it to the optimizer. This result, whatever it is, gets committed to the repository as `docs/phase0_report.md` so the foundation of the whole system is on record.

---

## The Trading Strategy at the Core

Before the loop runs, there is a clearly defined trading strategy. Crucible does not invent trades from scratch — it optimizes the numerical boundaries within a fixed logical framework.

**Timeframes used:** The strategy looks at two timeframes simultaneously. The 15-minute chart is used to find precise entry points. The 1-hour chart is used to confirm the overall market direction before any trade is considered.

**Trend confirmation on the 1-hour chart:** Before entering any trade, the system checks whether the 1-hour chart shows a clear directional trend. For a buy trade, the price must be above a medium-term moving average, and that moving average must be above a longer-term one. For a sell trade, the opposite must be true. If the 1-hour chart shows no clear direction, no trade is taken.

**Entry signals on the 15-minute chart:** The system uses three exponential moving averages of different lengths. When they line up in order from fast to slow, it signals that momentum is in one direction. A trade is only considered after the price pulls back slightly toward the moving averages, suggesting a temporary pause in the trend rather than a reversal.

**RSI filter:** The Relative Strength Index is checked to confirm momentum is healthy. Buy trades are only taken when RSI is in a moderate bullish zone, not when the market is already overbought. Sell trades require RSI to be in a moderate bearish zone, not already oversold.

**Candle quality filter:** The bar where the signal appears must have a relatively small body compared to its volatility range. A large-bodied candle suggests aggressive momentum that has already moved too far. Small-bodied pullback candles suggest a calm pause that is likely to continue in the original direction.

**Session filter:** Trades are only taken during the London and New York sessions, roughly 7am to 4pm UTC. Liquidity is highest during these hours, which means tighter spreads and more reliable price behavior. The Asian session is ignored.

**Stop-loss placement:** Stop-losses are placed using the Average True Range indicator, which measures how much the price typically moves in a given period. This makes the stop-loss adaptive to current volatility rather than fixed at an arbitrary distance.

**Take profit and trailing stop:** The first take profit target is placed at the same distance from entry as the stop-loss. When that target is hit, half the profit is locked in and the stop-loss is moved to the entry price. The remaining half of the position is then trailed using the lows of recent candles, allowing the trade to capture extended moves when they occur.

---

## The Parameters That Crucible Optimizes

Not every part of the strategy is adjusted by the system. The core logic — the rules described above — stays fixed. Only the numerical boundaries within that logic are open for optimization. These include:

- The lower and upper RSI thresholds for buy signals
- The lower and upper RSI thresholds for sell signals
- The ATR multiplier used to set the stop-loss distance
- The ATR multiplier used to set the take profit distance
- The maximum candle body size relative to ATR
- The number of pips used as a buffer on entry orders

Changing these numbers changes how strict or relaxed the strategy is without changing the underlying logic. Crucible finds the combination of these numbers that performed best on real historical data without overfitting to any one specific period.

---

## Execution Realism in Backtesting

Real price data is not the same as realistic execution. A backtest that ignores costs on a 15-minute timeframe — where the first take-profit sits only one stop-distance away — will systematically overstate results. Crucible's backtest engine therefore models execution explicitly, and every metric anywhere in the system is net of these costs.

**Spread:** Every simulated trade pays a spread. The default assumption is 1.0 pip round-trip on EUR/USD during the London and New York sessions, configurable and versioned in the engine settings. No zero-spread results are ever reported.

**Slippage:** A fixed adverse slippage of 0.2 pips is applied to every entry and every stop-loss exit, as a conservative stand-in for real-world fill quality.

**Swap/rollover:** Positions held past the daily rollover are charged swap. The stated default assumption is −0.6 pips per day on long EUR/USD positions and +0.2 pips per day on short positions, with the standard triple swap applied at the Wednesday rollover. These figures are explicit assumptions, versioned in the engine settings, and reviewed against published broker rates quarterly — a vague "representative cost" would hide an input that materially affects any strategy holding overnight.

**Intrabar ordering:** With OHLC candles alone, it is impossible to know whether the stop-loss or the take-profit was touched first inside a bar when both fall within its range. Crucible resolves this honestly using an **intrabar-ordering table** built offline from Dukascopy tick data. The raw ticks — gigabytes in size — never enter the repository or the workflows. A one-time preprocessing pass scans them and records, for every historical bar where both a plausible stop and target fall inside the bar's range, which side of the bar was touched first. The resulting table is kilobytes, committed to the repository, and refreshed whenever the historical dataset is extended. Where no tick record exists for a bar, the engine falls back to worst-case ordering — the stop-loss is assumed to have been hit first. Backtests never assume the favorable outcome.

---

## The Four-Layer Architecture

Crucible is organized into four layers that sit on top of each other. Each layer does a specific job.

**Layer 1 — Instructions Layer:** This is where the AI model receives its instructions. DeepSeek is given a descriptive system prompt that explains what Crucible is, defines its single role — report writing — and then removes every source of ambiguity: a field-by-field glossary of the metrics payload it will receive (what out-of-sample-versus-baseline means, how the gates decide, what the champion standing signifies, that rejection is a healthy outcome), the audience and length of the output, and hard rules against inventing numbers, predicting markets, or suggesting changes to the system. A model cannot misread context it has been explicitly given. It has no other role in the system.

**Layer 2 — Context Layer:** This layer controls what information each component sees during a run. It packages together the current strategy parameters, the recent results of the strategy's signals evaluated against live market data (no live trades are placed — these are hypothetical signals from the paper-forward log), and the current market volatility statistics. Garbage in, garbage out.

**Layer 3 — Harness Layer:** This is the actual backtesting engine. It takes a set of parameters, applies them to real historical price data under the execution realism model above, and produces a performance report. This layer is purely mechanical — it does not think, it just calculates. It outputs the win rate, net profit after costs, drawdown, number of trades, and other metrics for any given parameter set.

**Layer 4 — Loop Layer:** This is the orchestration layer that runs everything on a schedule. GitHub Actions fires up a fresh environment at set times, runs the other layers in sequence, makes the pass or fail decision, writes the result to disk, and sends the report to Discord. This layer is what makes the system autonomous.

---

## The Optimization Cadence

Crucible deliberately does not run a full optimization every day, for a statistical reason: every accept/reject decision is a hypothesis test, and running roughly 250 of them per year against overlapping data guarantees that some parameter sets will eventually pass by luck alone. Fewer, better-spaced tests keep the gate meaningful. Slower is safer, and it also keeps the system comfortably inside GitHub's free Actions budget.

**Daily monitoring run (every weekday, lightweight, under 10 minutes):** Pulls the latest candles, appends to the paper-forward signal log, updates the performance log and dashboard, checks for performance decay, and posts a brief Discord status. It also runs the deterministic regime classification: if the prevailing market regime has changed, the monitor swaps the active pointer to the matching pre-validated parameter set the same day. Switching among pre-validated sets is routing, not optimization — it involves no new hypothesis test — so it happens outside the weekly cadence and the cooldown, and every swap is reported to Discord. No optimization happens in this run.

**Optimization run (every Sunday after the weekly close, or when a decay trigger fires):** The full five-move loop described below, including Optuna search and Evaluator gating. The decay trigger fires when the active parameters' rolling 20-trade expectancy drops below zero or drawdown crosses the 8% monitoring alert level — in which case an optimization run is scheduled for the next weekend rather than executed impulsively mid-week. Note that this 8% alert is deliberately tighter than the Evaluator's 12% rejection ceiling: the monitor is meant to react before the situation is bad enough to fail the gate.

**Cooldown:** After any accepted parameter change, no further changes may be accepted for five trading days, regardless of what the optimizer finds. This prevents churn and gives every change enough forward data to be judged.

---

## The Five Moves of the Optimization Loop

When an optimization run triggers, the workflow executes five moves in sequence.

### Move 1: Discovery

The system wakes up and checks the current state of things. It loads the accumulated paper-forward log and the most recent performance data, confirms the historical dataset is current, validates that the active configuration pointer targets an existing, schema-valid parameter file, and verifies the cooldown window is clear. Discovery sets the context for everything that follows. If the cooldown is active, the run reports this to Discord and exits cleanly.

### Move 2: Handoff

The system opens an isolated workspace and begins the optimization process. It uses Optuna, a specialized numerical optimization library, to systematically search through combinations of parameter values. Optuna does not guess randomly — it uses Bayesian optimization, which means each trial informs the next one. Over up to 200 trials within a 30-minute time limit, it efficiently maps out which combinations of RSI bounds, ATR multipliers, and other parameters produce the best results on the training portion of the historical data — always under the full execution-cost model. DeepSeek is not involved in this step. Math does the math.

### Move 3: Verification

This is the most important step and the one most optimization systems skip. A new parameter set that performed well on the training data is not immediately trusted. It is handed to the Evaluator, a separate, deterministic set of Python checks, which runs the candidate on out-of-sample data the optimizer never saw. Every condition must pass; if even one fails, the new parameters are discarded and the existing ones remain active. The conditions are:

- **Meaningful improvement, not marginal:** out-of-sample net profit must beat the current baseline by at least 10% relative, not merely exceed it. A point-estimate tie goes to the incumbent.
- **Statistical floor:** at least 30 trades in the out-of-sample window. Results built on a handful of trades are noise and are rejected regardless of how good they look.
- **Drawdown ceiling:** out-of-sample maximum drawdown must stay below 12%.
- **Win-rate floor:** the out-of-sample win rate must stay above the configured minimum.
- **Consistency check:** performance must not collapse between training and test data. A sharp drop-off is the signature of overfitting and is an automatic rejection.
- **Same-feed comparison:** the candidate and the baseline are always compared on the same data feed. Cross-feed comparisons are never used for accept/reject decisions.

A note on the 10% hurdle and the 30-trade floor: these numbers are deliberate, not scientific constants. At a 30-trade sample, the point estimate of net profit carries wide error bars, and a bare "greater than" comparison would be won by luck roughly as often as not when a truly equivalent candidate is tested. Requiring a double-digit relative margin, a minimum sample, and only around 52 tests per year compounds into a low probability that a no-better candidate is ever accepted. Both numbers are versioned thresholds — conservative starting values scheduled for review after six months of accumulated forward data, not laws.

This is the crucible itself — only what survives the heat gets through.

### Move 4: Persistence

If and only if the Evaluator issues a clean pass on all conditions, the system saves the new parameters permanently. It overwrites the relevant regime configuration file with the updated values and records the full details of the change — the old values, the new values, the performance difference, and the date. The regime parameter file and the active-pointer file are always written in a **single commit**, so the configuration can never be observed in a half-updated state. Everything is committed to the Git repository, giving a permanent, auditable record of every parameter change the system has ever made. Nothing is stored in temporary memory that disappears when the container shuts down. The repository is the single source of truth.

### Move 5: Reporting and Notification

The system rebuilds the performance report as a static HTML page and pushes it to GitHub Pages, updating the live dashboard automatically. It then fires a detailed embed message to the configured Discord channel. This message is written by DeepSeek from the raw metrics and contains the specific values that changed, the before-and-after performance figures net of costs, the reason for acceptance or rejection, the current champion-vs-challenger standing, and the next scheduled run time. The container then shuts down and waits for the next trigger.

---

## Data Pipeline

The data layer is the foundation of the entire system. Crucible uses two data sources with strictly separated jobs, and a firm rule about which one is allowed to feed decisions.

**Decision data — Dukascopy:** All backtesting, all optimization, all Evaluator comparisons, and the paper-forward signal log are built exclusively on Dukascopy data. Dukascopy provides free tick-level historical EUR/USD data going back several years with no account registration, downloaded from https://www.dukascopy.com/swiss/english/marketwatch/historical/, cleaned, and resampled into 15-minute and 1-hour bars — with the tick history distilled offline into the compact intrabar-ordering table described in the execution realism section. Dukascopy publishes with roughly a one-day delay, which is fully compatible with a system that evaluates at and after market close. The dataset is refreshed weekly as part of the Sunday run.

**Context data — Twelve Data API:** The daily monitoring run pulls the most recent EUR/USD candles from the Twelve Data REST API to give same-day context for the dashboard and Discord status. Twelve Data requires only an email address to register — no broker account, no geographic restrictions, no credit card — and its free tier of 800 requests per day far exceeds Crucible's needs. The API key is stored as a GitHub Actions secret under `TWELVE_DATA_KEY` and never appears in code. Twelve Data candles are for display and context only; they never feed an accept/reject decision.

**Feed reconciliation:** Because the two feeds aggregate prices differently, a monthly reconciliation job compares them over their overlap and logs the mean absolute difference. If divergence exceeds 0.5 pips on average, an alert is raised in Discord — this catches silent data-quality drift before it can distort the performance picture.

No synthetic data is used anywhere. No random number generators are used to simulate price. Every number the system works with comes from real market prices.

---

## Walk-Forward Validation

One of the most common mistakes in strategy optimization is testing a strategy on the same data used to tune it. This always produces impressive-looking results that collapse on contact with real market conditions. The technical name for this is overfitting — and it has a slower, subtler cousin: reusing the same "unseen" test data across many optimization runs until it has effectively been trained on too. Crucible defends against both.

**Rolling, never-reused test windows:** The historical dataset is split into training and out-of-sample test chunks. The optimizer only ever sees the training chunks. Critically, the out-of-sample window rolls forward with each optimization run — each weekly run is judged primarily on market data that did not exist at the time of the previous run. No test window is reused across runs for accept/reject decisions.

**Embargo gap:** A buffer of several days is left between the end of the training window and the start of the test window, so information cannot leak across the boundary through overlapping indicator lookbacks.

**Final untouched holdout:** The most recent six months of history are permanently quarantined. Neither Optuna nor the weekly Evaluator ever touches this data. Once per quarter, an audit run evaluates the full parameter-change history against the holdout. The audit has consequences: if it shows the evolved system underperforming Champion Zero on the quarantined data, acceptance of new parameter changes is paused automatically until a human engineer reviews the audit and re-enables the loop through a pull request. An examiner whose verdict carries no consequence is not an examiner.

**Improvement margin and trade floor:** As specified in the Verification move, a candidate must beat the baseline by a real margin with a real sample size. This is the practical defense against the multiple-testing problem: with roughly 52 optimization runs per year instead of 250, and a 10% relative-improvement hurdle instead of a bare ">", lucky passes become rare rather than inevitable.

This combination — fresh test data, embargoed boundaries, an untouched holdout with teeth, and a meaningful hurdle — is the single most important structural feature of the system.

---

## Champion vs. Challenger Tracking

This is the mechanism that keeps Crucible honest about its own reason for existing.

On day one, the original Phase 0 parameters are frozen forever as **Champion Zero** in `config/champion_zero.json`. That file is never modified by anyone or anything. From then on, every optimization run replays both parameter sets — the frozen originals and the current evolved parameters — across all market data accumulated since launch, under identical execution costs.

One deliberate asymmetry is worth stating plainly: Champion Zero is a single universal parameter set, while the evolved system runs four regime-specific sets with daily switching. The comparison is therefore not set-versus-set but **system-versus-system** — it tests whether the entire adaptive apparatus, optimization plus regime routing combined, beats the simple static strategy it started from. That asymmetry is the point, not an oversight, and it should not be "fixed" by creating four champions.

The dashboard displays both equity curves side by side. Two rules follow from the comparison:

- If the evolved system underperforms Champion Zero over a rolling 90-day window, a warning is raised in Discord and the next optimization run is required to consider reverting to Champion Zero as a candidate.
- If the evolved system underperforms Champion Zero over a rolling 180-day window, the adaptive loop is suspended pending human review. A system that cannot beat its own frozen starting point has no business continuing to change things.

---

## Paper-Forward Signal Log

Backtests, however careful, only ever look backward. The paper-forward log is the system's forward-looking truth.

Every daily monitoring run evaluates the active strategy against the latest completed candles and records any hypothetical signals — entry, stop, targets, and eventual simulated outcome under the standard cost model — to `results/forward_log/`. No orders are placed anywhere; this is signal logging only.

Once a month, the system compares the forward log's realized statistics against what the backtest predicted for the same parameters over the same period. The check is numeric, not impressionistic. Each monthly comparison computes the expectancy and win rate of the most recent 30 logged forward signals and tests them against the backtest's prediction:

- **Expectancy trigger:** an alert fires in Discord when forward expectancy falls outside the backtest's expected range, defined as the backtest's mean per-trade profit plus or minus two standard errors of its per-trade profit distribution.
- **Win-rate trigger:** an alert fires when the forward win rate deviates from the backtest win rate by more than 10 percentage points.
- **Escalation:** two consecutive monthly alerts pause acceptance of new parameter changes until a human has reviewed the execution assumptions. If the backtest is no longer predicting reality, its verdicts should not be trusted with decisions.

The gap between prediction and forward reality — the **live-vs-backtest divergence metric** — is published on the dashboard. A small, stable divergence means the backtest engine is modeling reality well. A growing one means something is wrong with the execution assumptions or the data, and the system says so before it can silently corrupt the accept/reject decisions that depend on backtest accuracy.

---

## Market Regime Classification

Markets behave differently in different conditions, and a single universal parameter set is a compromise across all of them. Crucible therefore maintains four pre-validated parameter sets — one per regime — and selects among them deterministically.

The classification is pure arithmetic, computed fresh in every daily monitoring run from the 1-hour Dukascopy data, using ADX(14) and the current ATR's percentile within its trailing 90-day distribution. The full precedence matrix is:

| | ATR > 80th pct | ATR 20th–80th pct | ATR < 20th pct |
|---|---|---|---|
| **ADX > 25** | High Volatility | Trending | Trending |
| **ADX 20–25** | High Volatility | Ranging | Low Volatility |
| **ADX < 20** | High Volatility | Ranging | Low Volatility |

Three rules generate this matrix, in strict order: **High Volatility outranks everything** — when ATR is above the 80th percentile, risk control wins regardless of trend. **A quiet trend is still a trend** — when ADX exceeds 25 and volatility is not extreme-high, the Trending set applies even if ATR is below the 20th percentile. **Low Volatility requires the absence of trend** — the Low Volatility set applies only when ATR is below the 20th percentile and ADX is 25 or below; the neutral ADX zone of 20–25 with mid-range volatility defaults to Ranging. Every classification's inputs, output, and selected rule are logged, so any decision can be reproduced exactly.

When the regime changes, the daily monitor swaps `active.json` to the matching pre-validated set the same day rather than waiting for Sunday. This is routing, not optimization — no new hypothesis is tested — so it lives outside the weekly cadence and the cooldown, and every swap is announced in Discord.

**Staleness rule:** a regime parameter set that has not been re-validated within 90 days is flagged stale on the dashboard. When the monitor switches into a stale regime, two things happen: the next optimization run targets that regime's set first, regardless of which regime prevails on Sunday, and until that revalidation completes, Champion Zero's parameters are used as the safe fallback for that regime. Stale, unexamined parameters are treated as untrusted.

When the Sunday optimization run executes normally, it optimizes the parameter set belonging to the currently prevailing regime, tested on historical windows filtered to that same regime.

No AI model is involved anywhere in this section. The inputs are numbers and the output is one of four labels — a job for thresholds, not for a language model. Determinism and reproducibility matter more here than sophistication.

---

## Safety Guardrails

Several hard limits are built into the system to prevent runaway behavior.

**Runtime and budget ceiling:** The optimization workflow has a strict 60-minute maximum runtime, and the daily monitoring workflow a 10-minute maximum. With one heavy run per week and five light runs, total consumption stays around 500 Actions minutes per month — a quarter of GitHub's 2,000-minute free allowance for private repositories, leaving ample headroom. If any workflow exceeds its limit, GitHub terminates it and the Discord notification includes an alert that the run did not complete normally.

**Write permissions are minimal:** The automated workflow may modify only the files inside the `config/regimes/` folder plus the `config/active.json` pointer, and append to the `results/` folder. It cannot touch the backtest engine, the evaluation logic, the workflow files, or `config/champion_zero.json`. This prevents any bad output from corrupting the core system architecture.

**Two drawdown numbers, two jobs:** the monitoring decay alert fires at 8% drawdown (or when rolling 20-trade expectancy turns negative) and schedules an early optimization run; the Evaluator's rejection ceiling sits at 12%. The alert is deliberately tighter than the gate so the system reacts before the situation is bad enough to fail.

**Configuration atomicity:** the regime parameter file and the `active.json` pointer are always updated in a single commit, and every run — monitoring or optimization — begins by validating that the pointer targets an existing, schema-valid parameter file. The system can never run on a half-written configuration.

**No-upgrade termination:** If Optuna exhausts its 200 trials without producing a candidate that clears every Evaluator condition, the loop terminates cleanly, keeps the existing parameters, and reports this to Discord. The system is designed to recognize when no upgrade is available and do nothing, rather than forcing a change that is not clearly better. Doing nothing is an expected, healthy outcome — not a failure.

**Change cooldown:** No parameter change may be accepted within five trading days of the previous accepted change.

**Champion Zero circuit breaker:** Sustained underperformance against the frozen baseline — 90 days for a warning, 180 days for suspension — halts the adaptive loop automatically, as described above.

**Holdout audit circuit breaker:** A failed quarterly holdout audit pauses acceptance of new parameter changes until a human reviews and re-enables the loop through a pull request.

**Explicit comparison in Discord reports:** The Discord notification never just says "optimization complete." It always shows the specific before-and-after values and the exact delta in net-of-cost performance metrics. This forces the human engineer to remain aware of what is changing and why, rather than blindly accepting automated outputs.

**CI gate on human changes:** Every pull request — the only path by which the strategy logic, the Evaluator, or the workflows can change — must pass the full test suite and module compilation before merging. The automated loop is reviewed by the Evaluator; the humans are reviewed by CI.

---

## The Role of DeepSeek

DeepSeek's role is deliberately narrow: it writes the reports. After the numerical evaluation is complete, it reads the raw metrics and produces the human-readable summary for the Discord notification — the specific values that changed, the performance deltas, the reason for acceptance or rejection, and any anomalies. Turning a table of numbers into a clear, concise explanation is where a language model genuinely earns its place.

Because a report is only as good as the context behind it, the prompt and payload are deliberately rich. The system prompt describes the whole system, defines every field the model will receive — net-of-cost figures, the same-data candidate-versus-baseline comparison, each gate's pass/fail detail, the champion-vs-challenger standing, regime routing state, decay alerts — and pins the output to 80–150 words of plain text that leads with the outcome and names any failed gates. The payload itself carries the run date, both parameter sets, both out-of-sample results, and the champion standing, so the summary is grounded in the actual decision rather than a fragment of it. The model may only use numbers present in the payload; if the DeepSeek API is unavailable, a deterministic fallback summary is generated from the same payload so reporting never silently stops.

DeepSeek does not optimize parameters, does not classify market regimes, does not touch any decision in the accept/reject path, and cannot write to any configuration file. Everything that affects a decision is handled by deterministic code, so every decision the system makes can be reproduced bit-for-bit. The `DEEPSEEK_KEY` secret grants it exactly one capability: prose.

---

## Repository Structure

The Crucible repository is organized into clear sections, each with a specific purpose. The companion document `CRUCIBLE_STRUCTURE.md` specifies the full tree; in summary:

The `src` folder contains one Python module per stage: data fetching, strategy logic, backtesting, regime classification, optimization, evaluation, forward logging, and reporting. The `config` folder contains `champion_zero.json` (frozen, never modified), the `regimes/` subfolder with the four regime parameter files the bot is allowed to modify, and `active.json`, the pointer recording which regime set is currently selected. The `data` folder holds the Dukascopy candle files and the committed intrabar-ordering table — raw ticks are never stored. The `results` folder stores the performance logs and the `forward_log/` of daily paper signals. The `docs` folder is the GitHub Pages source, including the Phase 0 report, rebuilt automatically after each run. The `tests` folder covers the two components whose silent failure would corrupt everything: the Evaluator gates and the backtest harness.

The strategy logic file and the evaluation gatekeeper file are both committed and version-controlled but are never modified by the automated workflow. Any changes to these files must be made by a human engineer through a normal pull request — and every pull request is gated by a CI workflow that runs the full test suite (every Evaluator gate, the backtest cost model) and compiles every module before the change can merge. The same CI runs on any code push to main, while skipping the bot's routine data and result commits so it costs no Actions minutes on appends. The Python version is pinned explicitly so runner defaults can never shift underneath the system.

The division of labor for review is explicit. Automated parameter changes do not go through pull requests: their reviewer is the Evaluator itself — deterministic, tested, and stricter than a human eyeballing JSON diffs — with the Discord report keeping the engineer aware and `git revert` as the undo. Human code changes get the opposite treatment: no code reaches main without passing CI.

Three secrets are stored in GitHub Actions and never appear in any file: `TWELVE_DATA_KEY` for context candle fetching, `DEEPSEEK_KEY` for report writing, and `DISCORD_WEBHOOK` for notifications. Rotating any of these requires only updating the secret value in the repository settings — no code changes needed.

---

## Monitoring and Observability

**GitHub Pages Dashboard:** A live webpage hosted from the repository shows the current active regime and parameters, staleness flags for any regime set not validated within 90 days, the equity curve over time, the Champion Zero vs. evolved-system comparison curves, the live-vs-backtest divergence metric, the win rate trend, the drawdown history, and a log of every parameter change ever made with dates and reasons. This updates automatically after every run.

**Discord Channel:** A dedicated channel receives a structured embed after every run — a brief daily status from monitoring runs (including any regime swaps), and a full report from optimization runs including the outcome (accepted or rejected), the specific net-of-cost metrics before and after, which parameters changed and by how much, the champion-vs-challenger standing, and any warnings or anomalies.

**Plain-English section:** Every Discord report and the dashboard also carry a non-technical summary generated by deterministic code (never by the language model — money figures must come from arithmetic). It translates pips into dollars at a configurable small position size, states what the recent practice signals would have made or lost, projects the pace per day, week and year with an explicit "if the market kept behaving the same way" caveat, and explains parameter decisions as "found better settings and switched" or "tried new settings but kept the old ones." Simulated results are always labeled as such.

**Git History:** Because every parameter change is a Git commit, the full history of every decision the system has ever made is permanently recorded. Any change can be reversed by reverting a commit.

**Quarterly Holdout Audit:** Once per quarter, the untouched six-month holdout grades the entire parameter-change history — and a failed grade pauses the loop pending human review.

---

## What Crucible Does Not Do

To be clear about scope:

Crucible does not place live trades. It is a strategy perfection engine, not an execution engine. Connecting it to a live trading account requires a separate execution layer that is outside the scope of this system.

Crucible does not invent new strategies. It optimizes the numerical boundaries of the existing strategy logic. The core rules — which indicators to use, how entries and exits work, which sessions to trade — are set by the human engineer and do not change.

Crucible does not assume that adaptive beats static. It measures that claim continuously against a frozen baseline, and it suspends itself if the measurement says otherwise.

Crucible does not guarantee profitability. Better parameters in backtesting improve the probability of better forward performance, but past performance on historical data does not guarantee future results. The system reduces the risk of running a degraded strategy by keeping parameters current, but market conditions can change in ways historical data did not anticipate.

---

## Summary

Crucible is built on one core idea: a trading strategy should never be static. Markets evolve, volatility changes, and the numerical thresholds that define a good trade shift over time. Manually keeping up with these changes is slow and inconsistent. An automated system that tests, validates, and updates parameters on a disciplined schedule — using real data, realistic costs, deterministic evaluation, and a strict rejection policy — is more consistent than any human process.

But consistency alone is not enough. Crucible holds itself to the same standard it holds every candidate parameter set: it must prove its foundation before it starts (Phase 0), it must model reality honestly (execution costs, forward divergence checks), and it must continuously beat its own frozen starting point or stand down (Champion vs. Challenger).

The system's value is not in being clever. It is in being consistent, rigorous, and honest about what the data actually shows — including about itself.

---

*Crucible — Built by Blair Khan, July 2026*
