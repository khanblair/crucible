"""Genome evolution: losing-streak/stuck-optimizer trigger, a screen-then-refine
funnel over strategy structures, a stricter structural gate, and an evidence-
backed pull request rather than a direct commit.

Nothing here writes new logic — it only recombines and parameter-tunes the
human-authored modules in src/modules/, exactly like the rest of Crucible's
automation never touches strategy.py or evaluate.py directly.
"""
import datetime as dt
import json
import pathlib
import subprocess

from src.backtest import load_intrabar, load_settings, phase0_passed, run_backtest
from src.evaluate import evaluate_candidate
from src.genome import all_combinations, list_genomes, load_genome
from src.optimize import regime_dates, search, split_windows, target_regime
from src.regime import validate_active

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _relative_change(first: float, last: float) -> float:
    if first == 0:
        return 1.0 if last > 0 else 0.0
    return (last - first) / abs(first)


def losing_streak_and_stuck(settings: dict) -> dict:
    """Both conditions true => genome evolution should be attempted. Reads
    existing results/runs/decision_*.json history — no new data source."""
    cfg = settings["evolution"]
    n = cfg["losing_streak_runs"]
    decisions = sorted((ROOT / "results" / "runs").glob("decision_*.json"))
    if len(decisions) < n:
        return {"triggered": False, "reason": f"fewer than {n} optimization runs recorded",
                "window": n}
    recent = [json.loads(p.read_text()) for p in decisions[-n:]]
    losing_streak = all(d["baseline_oos"]["net_profit_pips"] <= 0 for d in recent)
    first_best = recent[0]["candidate_oos"]["net_profit_pips"]
    last_best = recent[-1]["candidate_oos"]["net_profit_pips"]
    stuck = _relative_change(first_best, last_best) <= cfg["stuck_improvement_epsilon"]
    return {"triggered": losing_streak and stuck, "losing_streak": losing_streak,
            "stuck": stuck, "window": n}


def evolution_cooldown_clear(settings: dict, today: dt.date | None = None) -> bool:
    """An evolution attempt is not retried within cooldown_quarters of the
    previous one, whether that attempt found a winner or not."""
    today = today or dt.date.today()
    attempts = sorted((ROOT / "results" / "runs").glob("evolution_*.json"))
    if not attempts:
        return True
    last = dt.date.fromisoformat(json.loads(attempts[-1].read_text())["date"])
    months_elapsed = (today.year - last.year) * 12 + (today.month - last.month)
    return months_elapsed >= settings["evolution"]["cooldown_quarters"] * 3


def screen(df15, df1h, settings: dict, intrabar: dict, allowed_dates: set | None,
          params: dict) -> list[dict]:
    """One backtest per genome combination with sensible default parameters,
    ranked by net profit. Cheap: no Optuna involved yet."""
    results = []
    for genome in all_combinations():
        m = run_backtest(df15, df1h, params, settings, intrabar, allowed_dates, genome)["metrics"]
        results.append({"genome": genome, "net_profit_pips": m["net_profit_pips"],
                        "n_trades": m["n_trades"]})
    return sorted(results, key=lambda r: r["net_profit_pips"], reverse=True)


def refine(screened: list[dict], df15, df1h, settings: dict, intrabar: dict,
          allowed_dates: set | None, top_k: int, n_trials: int, timeout_s: float) -> dict:
    """Full Optuna search on each of the top-K screened genomes; returns the
    single best by training-window net profit. A genome that can't clear the
    trade-count floor within the search budget is skipped, not fatal — this
    mirrors the existing 'no-upgrade termination' guardrail one level down."""
    candidates = []
    for entry in screened[:top_k]:
        genome = entry["genome"]
        try:
            params, train_metrics = search(df15, df1h, settings, intrabar, allowed_dates,
                                           n_trials, timeout_s, genome)
        except RuntimeError:
            continue
        candidates.append({"genome": genome, "params": params, "train_metrics": train_metrics})
    if not candidates:
        raise RuntimeError("no screened genome produced a viable candidate")
    return max(candidates, key=lambda c: c["train_metrics"]["net_profit_pips"])


def genome_id_for(genome: dict) -> str:
    """Reuse an existing genome file's id if this combination already has
    one (e.g. it matches baseline); otherwise mint a new id from the choices."""
    for existing_id in list_genomes():
        existing = load_genome(existing_id)
        if (existing["entry_signal"], existing["exit_style"]) == \
                (genome["entry_signal"], genome["exit_style"]):
            return existing_id
    return f"{genome['entry_signal']}__{genome['exit_style']}"


def run_evolution() -> dict:
    """Full trigger-check-then-funnel flow. Returns the evolution record
    written to results/runs/evolution_<date>.json (or a not-attempted stub).
    Phase 0 no longer gates this — it's informational (see docs/phase0_report.md);
    every candidate still has to clear the stricter structural gate below."""
    settings = load_settings()
    validate_active()
    trigger = losing_streak_and_stuck(settings)
    if not trigger["triggered"]:
        return {"attempted": False, "trigger": trigger, "phase0_passed": phase0_passed()}
    if not evolution_cooldown_clear(settings):
        return {"attempted": False, "trigger": trigger, "reason": "cooldown active"}

    from src.data import load_candles
    df15, df1h = load_candles()
    intrabar = load_intrabar()
    windows = split_windows(df15, settings)
    regime = target_regime(df1h, settings)
    train15 = df15[df15.index <= windows["train_end"]]
    train1h = df1h[df1h.index <= windows["train_end"]]
    allowed = regime_dates(train1h, settings, regime) or None

    baseline_regime = json.loads((ROOT / "config" / "regimes" / f"{regime}.json").read_text())
    screened = screen(train15, train1h, settings, intrabar, allowed, baseline_regime["params"])

    cfg = settings["evolution"]
    optuna_cfg = settings["optuna"]
    winner = refine(screened, train15, train1h, settings, intrabar, allowed,
                    cfg["screen_top_k"], optuna_cfg["n_trials"], optuna_cfg["timeout_minutes"] * 60)

    m15 = df15[(df15.index >= windows["oos_start"]) & (df15.index <= windows["oos_end"])]
    m1h = df1h[df1h.index <= windows["oos_end"]]
    baseline_genome = load_genome(baseline_regime["genome"])
    cand_oos = run_backtest(m15, m1h, winner["params"], settings, intrabar,
                            genome=winner["genome"])["metrics"]
    base_oos = run_backtest(m15, m1h, baseline_regime["params"], settings, intrabar,
                            genome=baseline_genome)["metrics"]
    verdict = evaluate_candidate(cand_oos, base_oos, winner["train_metrics"], settings,
                                 min_relative_improvement=cfg["structural_min_relative_improvement"])

    champion = json.loads((ROOT / "config" / "champion_zero.json").read_text())
    champ_genome = load_genome(champion["genome"])
    champ_oos = run_backtest(m15, m1h, champion["params"], settings, intrabar,
                             genome=champ_genome)["metrics"]
    beats_champion = cand_oos["net_profit_pips"] > champ_oos["net_profit_pips"]
    if not beats_champion:
        verdict = {"accepted": False, "gates": verdict["gates"] + [
            {"name": "beats_champion_zero", "passed": False,
             "detail": f"candidate {cand_oos['net_profit_pips']} vs "
                      f"champion zero {champ_oos['net_profit_pips']} (must strictly exceed)"}]}

    record = {
        "date": dt.date.today().isoformat(), "type": "evolution", "regime": regime,
        "window": trigger["window"], "screened": screened,
        "winner_genome": winner["genome"], "winner_params": winner["params"],
        "winner_train_metrics": winner["train_metrics"],
        "candidate_oos": {k: v for k, v in cand_oos.items() if k != "equity_curve"},
        "baseline_oos": {k: v for k, v in base_oos.items() if k != "equity_curve"},
        "champion_oos": {k: v for k, v in champ_oos.items() if k != "equity_curve"},
        **verdict,
    }
    out = ROOT / "results" / "runs" / f"evolution_{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")

    if record["accepted"]:
        pr_url = open_pr(record)
        record["pr_url"] = pr_url
        out.write_text(json.dumps(record, indent=2) + "\n")
        from src.report import evolution_pr_opened_report
        evolution_pr_opened_report(record, pr_url, settings)
    return record


def _pr_body(record: dict, gid: str, settings: dict) -> str:
    from src.report import plain_summary_evolution
    lines = [f"## Genome evolution: `{gid}`", "",
             f"Triggered by a losing streak with a stuck optimizer over the last "
             f"{record['window']} optimization runs.", "",
             "### Screen stage (all combinations, current baseline parameters)", "",
             "| entry_signal | exit_style | net profit (pips) | trades |",
             "|---|---|---|---|"]
    for s in record["screened"]:
        g = s["genome"]
        lines.append(f"| {g['entry_signal']} | {g['exit_style']} | "
                     f"{s['net_profit_pips']} | {s['n_trades']} |")
    lines += ["", "### Refine stage winner (full Optuna search on the top "
             f"{settings['evolution']['screen_top_k']} screened genomes)", "",
             f"- Entry signal: `{record['winner_genome']['entry_signal']}`",
             f"- Exit style: `{record['winner_genome']['exit_style']}`",
             f"- Parameters: `{json.dumps(record['winner_params'])}`", "",
             "### Out-of-sample evaluation (same feed, same window)", "",
             f"- Candidate: {record['candidate_oos']['net_profit_pips']} pips net, "
             f"{record['candidate_oos']['n_trades']} trades, "
             f"{record['candidate_oos']['win_rate']:.2%} win rate, "
             f"{record['candidate_oos']['max_drawdown']:.2%} max drawdown",
             f"- Current baseline: {record['baseline_oos']['net_profit_pips']} pips net",
             f"- Champion Zero (frozen day-one strategy): "
             f"{record['champion_oos']['net_profit_pips']} pips net", "",
             "### Gates", ""]
    for g in record["gates"]:
        lines.append(f"- {'PASS' if g['passed'] else 'FAIL'} — **{g['name']}**: {g['detail']}")
    lines += ["", "### In plain English", "", plain_summary_evolution(settings, record), "",
             "---", f"_This PR auto-merges in "
             f"{settings['evolution']['pr_auto_merge_hours']:.0f} hours if not reviewed sooner, "
             f"provided CI is green. A reminder posts to Discord "
             f"{settings['evolution']['pr_reminder_hours_before']} hour(s) before that._"]
    return "\n".join(lines) + "\n"


def open_pr(record: dict) -> str:
    """Commit the winning genome + updated regime pointers to a new branch and
    open a labeled pull request with full evidence. Requires `git` and `gh` on
    PATH, authenticated — this only runs inside evolve.yml. Returns the PR URL."""
    settings = load_settings()
    gid = genome_id_for(record["winner_genome"])

    # Create and switch to the branch FIRST — if this fails, nothing has
    # written to the working tree yet, so main's checkout stays untouched.
    branch = f"evolve/{record['date']}-{gid}"
    subprocess.run(["git", "checkout", "-b", branch], check=True, cwd=ROOT)

    genome_path = ROOT / "config" / "genomes" / f"{gid}.json"
    if not genome_path.exists():
        genome_path.write_text(json.dumps({
            "id": gid, "entry_signal": record["winner_genome"]["entry_signal"],
            "exit_style": record["winner_genome"]["exit_style"],
            "created": record["date"], "created_by": f"evolution {record['date']}",
        }, indent=2) + "\n")

    # One shared genome across all regimes (v1): every regime's pointer moves
    # together. Only the regime actually tested gets its params updated here —
    # the others re-tune under the new structure on their own weekly cadence.
    for regime_file in sorted((ROOT / "config" / "regimes").glob("*.json")):
        data = json.loads(regime_file.read_text())
        data["genome"] = gid
        if data["name"] == record["regime"]:
            data["params"] = record["winner_params"]
            data["last_validated"] = record["date"]
            data["validated_by"] = f"evolution {record['date']}"
        regime_file.write_text(json.dumps(data, indent=2) + "\n")

    subprocess.run(["git", "add", "config/genomes", "config/regimes"], check=True, cwd=ROOT)
    subprocess.run(["git", "-c", "user.name=crucible-bot",
                   "-c", "user.email=crucible-bot@users.noreply.github.com",
                   "commit", "-m", f"evolve: propose genome '{gid}' ({record['date']})"],
                  check=True, cwd=ROOT)
    subprocess.run(["git", "push", "-u", "origin", branch], check=True, cwd=ROOT)

    body_path = ROOT / f"_evolution_pr_body_{record['date']}.md"
    body_path.write_text(_pr_body(record, gid, settings))
    try:
        result = subprocess.run(
            ["gh", "pr", "create", "--title", f"Genome evolution: propose '{gid}' ({record['date']})",
             "--body-file", str(body_path), "--label", "crucible-evolution"],
            check=True, cwd=ROOT, capture_output=True, text=True)
    finally:
        body_path.unlink(missing_ok=True)
    return result.stdout.strip().splitlines()[-1]


def watch_prs() -> None:
    """Checks every open `crucible-evolution` PR's age: sends a one-time
    Discord reminder near the deadline, and auto-merges (CI-gated, branch
    deleted) once the deadline passes. Meant to run on a frequent cron
    (pr_watch.yml) — safe to call repeatedly, nothing here is destructive
    beyond the merge itself."""
    settings = load_settings()
    cfg = settings["evolution"]
    listing = subprocess.run(
        ["gh", "pr", "list", "--label", "crucible-evolution", "--state", "open",
         "--json", "number,createdAt,url"], check=True, cwd=ROOT, capture_output=True, text=True)
    for pr in json.loads(listing.stdout):
        created = dt.datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        age_hours = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600
        reminder_at = cfg["pr_auto_merge_hours"] - cfg["pr_reminder_hours_before"]

        view = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--json", "labels"],
                              check=True, cwd=ROOT, capture_output=True, text=True)
        labels = {l["name"] for l in json.loads(view.stdout)["labels"]}

        if reminder_at <= age_hours < cfg["pr_auto_merge_hours"] \
                and "evolution-reminder-sent" not in labels:
            from src.report import evolution_pr_reminder_report
            evolution_pr_reminder_report(pr["url"], cfg["pr_auto_merge_hours"] - age_hours)
            subprocess.run(["gh", "pr", "edit", str(pr["number"]),
                           "--add-label", "evolution-reminder-sent"], check=True, cwd=ROOT)

        if age_hours >= cfg["pr_auto_merge_hours"]:
            checks = subprocess.run(["gh", "pr", "checks", str(pr["number"])],
                                    cwd=ROOT, capture_output=True, text=True)
            if checks.returncode == 0:   # gh exits 0 only when every required check passed
                subprocess.run(["gh", "pr", "merge", str(pr["number"]),
                               "--squash", "--delete-branch"], check=True, cwd=ROOT)


def _check_trigger() -> bool:
    """Prints exactly 'true' or 'false' — used by optimize.yml to decide
    whether to dispatch evolve.yml, without embedding Python in YAML. Runs
    daily now that optimize.yml itself runs daily (see settings.evolution)."""
    settings = load_settings()
    trigger = losing_streak_and_stuck(settings)
    return trigger["triggered"] and evolution_cooldown_clear(settings)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Crucible genome evolution")
    ap.add_argument("--check-trigger", action="store_true",
                    help="print 'true'/'false': should evolve.yml be dispatched?")
    ap.add_argument("--watch-prs", action="store_true",
                    help="check open crucible-evolution PRs: remind, then auto-merge at deadline")
    args = ap.parse_args()
    if args.check_trigger:
        print(str(_check_trigger()).lower())
    elif args.watch_prs:
        watch_prs()
    else:
        print(json.dumps({k: v for k, v in run_evolution().items() if k != "screened"}, indent=2))
