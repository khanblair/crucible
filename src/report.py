"""Reporting: DeepSeek summary, Discord embed, GitHub Pages dashboard build.

DeepSeek's single role is prose — it reads raw metrics and writes the
human-readable summary. It touches no decision, no configuration, nothing
in the accept/reject path. Without a key, a deterministic fallback is used.
"""
import datetime as dt
import json
import os
import pathlib

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "results" / "runs"

DEEPSEEK_SYSTEM_PROMPT = """\
You are the report writer for Crucible, an autonomous EUR/USD trading-strategy
optimization engine. Crucible runs backtests on historical data, searches for
better strategy parameters with Optuna, and accepts a change only if it passes
every deterministic Evaluator gate on out-of-sample data. It places no live
trades. You are its only non-deterministic component, and your single role is
prose: turn the raw metrics JSON you receive into a summary a human engineer
reads in Discord.

How to read the payload you receive:
- "kind": "optimization" (weekly run: a candidate was gated) or "daily"
  (weekday monitoring run: no optimization happened).
- All profit figures are NET of execution costs (spread, slippage, swap),
  measured in pips. "candidate_oos" / "baseline_oos" are out-of-sample results
  for the new candidate vs the currently active parameters ON THE SAME DATA —
  this comparison is what decided acceptance.
- "gates" lists every accept/reject condition with pass/fail and detail.
  A single failed gate rejects the candidate; rejection is a healthy, expected
  outcome, not a failure of the system.
- "params" vs "baseline_params": the specific numeric values that would change
  (RSI bounds, ATR multipliers, candle-body limit, entry buffer).
- "champion": the evolved system replayed against the frozen day-one baseline
  (Champion Zero). "warning_90d"/"suspend_180d" true means the adaptive system
  is losing to its own frozen starting point — say so plainly.
- "regime": which market-condition parameter set (trending / ranging /
  high_volatility / low_volatility) was optimized or is active.
- Daily payloads carry "status" with the regime classification, whether the
  active set was swapped (routing, not optimization), and decay-alert state.

Write 80-150 words of plain text (no markdown headers, no bullet-point walls).
Lead with the outcome. State the specific before-and-after values, the exact
reason for acceptance or rejection (name the failed gates), the champion
standing, and call out any anomaly (warnings, suspensions, decay triggers,
unusually few trades). Use only numbers present in the payload — never invent,
extrapolate, or round beyond one decimal. Do not give trading advice, predict
markets, or suggest changes to the system."""


def deepseek_summary(payload: dict) -> str:
    key = os.environ.get("DEEPSEEK_KEY")
    if not key:
        return _fallback_summary(payload)
    try:
        r = requests.post("https://api.deepseek.com/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": "deepseek-chat", "temperature": 0.3,
                                "messages": [
                                    {"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT},
                                    {"role": "user", "content": json.dumps(payload)}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # prose must never break the run
        return _fallback_summary(payload) + f"\n(DeepSeek unavailable: {exc})"


def _fallback_summary(payload: dict) -> str:
    kind = payload.get("kind", "run")
    if kind == "optimization":
        d = payload["decision"]
        verdict = "ACCEPTED" if d["accepted"] else "REJECTED"
        failed = [g["name"] for g in d["gates"] if not g["passed"]]
        return (f"Optimization {verdict} for regime '{d['regime']}'. Candidate OOS net "
                f"{d['candidate_oos']['net_profit_pips']} pips vs baseline "
                f"{d['baseline_oos']['net_profit_pips']} pips."
                + (f" Failed gates: {', '.join(failed)}." if failed else ""))
    return f"Daily monitoring run complete: {json.dumps(payload.get('status', {}))}"


def discord_notify(title: str, description: str, fields: list[dict],
                   color: int = 0x2B6CB0) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        print(f"[discord skipped] {title}\n{description}")
        return
    embed = {"title": title, "description": description[:4000], "color": color,
             "fields": fields[:25],
             "footer": {"text": f"Crucible · {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC"}}
    requests.post(webhook, json={"embeds": [embed]}, timeout=30).raise_for_status()


def _latest(pattern: str) -> dict | None:
    files = sorted(RUNS.glob(pattern))
    return json.loads(files[-1].read_text()) if files else None


# ------------------------------------------------- plain-English reporting
# Deterministic on purpose: money figures must come from arithmetic, never
# from a language model. Estimates use the small starter position size in
# config/settings.json -> reporting (0.1 lot = $1 per pip on EUR/USD).
REGIME_PLAIN = {
    "trending": "a trending market (prices moving steadily in one direction)",
    "ranging": "a sideways market (prices bouncing inside a band)",
    "high_volatility": "a very choppy market (big, fast swings)",
    "low_volatility": "a quiet market (small, slow moves)",
}


def _usd(pips: float, settings: dict) -> float:
    r = settings["reporting"]
    return pips * r["pip_value_usd_per_standard_lot"] * r["lot_size"]


def _made_or_lost(usd: float) -> str:
    return f"made about ${usd:,.2f}" if usd >= 0 else f"lost about ${abs(usd):,.2f}"


def _forward_money(settings: dict) -> dict | None:
    """USD view of the paper-forward log: net result and its daily/weekly/
    yearly pace over the calendar span the signals actually cover."""
    path = ROOT / "results" / "forward_log" / "signals.jsonl"
    if not path.exists():
        return None
    records = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    closed = [r for r in records
              if r.get("kind") == "resolution" and r.get("outcome") == "closed"]
    if not closed:
        return None
    dates = sorted(r["signal_time"][:10] for r in closed)
    span = max((dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days, 1)
    net = _usd(sum(r["pnl_pips"] for r in closed), settings)
    per_day = net / span
    return {"n": len(closed), "span_days": span, "net_usd": round(net, 2),
            "per_day": round(per_day, 2), "per_week": round(per_day * 7, 2),
            "per_year": round(per_day * 365)}


def plain_summary(settings: dict, decision: dict | None = None) -> str:
    """Non-technical summary anyone can read. No jargon, no invented numbers,
    and no promises — practice results at the recent pace, clearly labeled."""
    parts = []
    if decision:
        base = _usd(decision["baseline_oos"]["net_profit_pips"], settings)
        cand = _usd(decision["candidate_oos"]["net_profit_pips"], settings)
        days = settings["walk_forward"]["oos_days"]
        if decision["accepted"]:
            parts.append(
                f"This week the system found better settings and switched to them. "
                f"On the {days}-day test period, the old settings would have "
                f"{_made_or_lost(base)} and the new ones {_made_or_lost(cand)} — "
                f"an improvement of ${cand - base:,.2f}.")
        else:
            parts.append(
                f"This week the system tried new settings but kept the old ones, "
                f"because the new ones were not clearly better. On the {days}-day "
                f"test period the old settings would have {_made_or_lost(base)} "
                f"and the new ones {_made_or_lost(cand)}.")
    m = _forward_money(settings)
    if m:
        pace = "earning" if m["per_day"] >= 0 else "losing"
        parts.append(
            f"In practice mode — no real money is traded — the last {m['n']} signals "
            f"over roughly {m['span_days']} day(s) would have {_made_or_lost(m['net_usd'])} "
            f"with a small starter position (0.1 lot). That pace means {pace} about "
            f"${abs(m['per_day']):,.2f} a day, ${abs(m['per_week']):,.2f} a week, or "
            f"${abs(m['per_year']):,.0f} a year — IF the market kept behaving the same "
            f"way, which is never guaranteed.")
    else:
        parts.append("Not enough practice signals have been recorded yet to "
                     "estimate results in dollars.")
    regime_log = _latest("regime_*.json")
    if regime_log:
        label = regime_log["decision"]["regime"]
        parts.append(f"Right now the market looks like {REGIME_PLAIN.get(label, label)}, "
                     f"so the system is using its settings made for that condition.")
    parts.append("These are simulated results, not financial advice.")
    return " ".join(parts)


def plain_summary_evolution(settings: dict, record: dict) -> str:
    """Non-technical summary for a genome-evolution PR — a structural change,
    not a parameter tweak, so it gets its own plain-English framing."""
    base = _usd(record["baseline_oos"]["net_profit_pips"], settings)
    cand = _usd(record["candidate_oos"]["net_profit_pips"], settings)
    champ = _usd(record["champion_oos"]["net_profit_pips"], settings)
    days = settings["walk_forward"]["oos_days"]
    parts = [
        f"The system tried a completely different way of trading and thinks it "
        f"found something better. On the {days}-day test period, the current way "
        f"would have {_made_or_lost(base)} and the new way {_made_or_lost(cand)} "
        f"— it also beat the original day-one strategy, which would have "
        f"{_made_or_lost(champ)} over the same period.",
        "I opened a pull request for you to review — you have "
        f"{settings['evolution']['pr_auto_merge_hours']:.0f} hours, after which it "
        "merges automatically if you don't respond.",
        "These are simulated results, not financial advice.",
    ]
    return " ".join(parts)


def plain_reminder_evolution(hours_left: float) -> str:
    if hours_left < 1:
        return (f"Reminder: about {round(hours_left * 60)} minutes left before this "
                "pull request auto-merges.")
    return f"Reminder: about {hours_left:.1f} hours left before this pull request auto-merges."


def evolution_pr_opened_report(record: dict, pr_url: str, settings: dict) -> None:
    """Immediate Discord notification the moment a genome-evolution PR opens."""
    fields = [
        {"name": "Proposed genome", "value": f"{record['winner_genome']['entry_signal']} + "
                                             f"{record['winner_genome']['exit_style']}", "inline": True},
        {"name": "Review window", "value": f"{settings['evolution']['pr_auto_merge_hours']:.0f} hours",
         "inline": True},
        {"name": "Pull request", "value": pr_url, "inline": False},
        {"name": "In plain English", "value": plain_summary_evolution(settings, record)[:1024],
         "inline": False},
    ]
    discord_notify("Crucible · genome evolution proposal", "A new strategy structure "
                   "passed every gate and is waiting for your review.", fields, 0x9F7AEA)


def evolution_pr_reminder_report(pr_url: str, hours_left: float) -> None:
    """Discord reminder fired once, ~pr_reminder_hours_before the auto-merge deadline."""
    discord_notify("Crucible · genome evolution reminder", plain_reminder_evolution(hours_left),
                   [{"name": "Pull request", "value": pr_url, "inline": False}], 0xD29922)


def build_dashboard() -> None:
    """Rebuild docs/index.html — a single self-contained page — from results."""
    active = json.loads((ROOT / "config" / "active.json").read_text())
    decision = _latest("decision_*.json")
    regime_log = _latest("regime_*.json")
    changes = []
    for path in sorted(RUNS.glob("decision_*.json")):
        rec = json.loads(path.read_text())
        changes.append({"date": rec["date"], "regime": rec["regime"],
                        "accepted": rec["accepted"],
                        "failed": [g["name"] for g in rec["gates"] if not g["passed"]]})
    divergence = []
    div_path = ROOT / "results" / "forward_log" / "divergence.jsonl"
    if div_path.exists():
        divergence = [json.loads(l) for l in div_path.read_text().splitlines() if l.strip()]
    price = None
    try:
        from src.data import load_candles
        closes = load_candles()[1]["close"].iloc[-240:]  # ~10 trading days of 1h bars
        price = {"labels": [str(t)[5:16] for t in closes.index],
                 "values": [round(float(v), 5) for v in closes]}
    except FileNotFoundError:
        pass
    forward = {"cum": [], "n": 0, "net": 0.0, "win_rate": None}
    sig_path = ROOT / "results" / "forward_log" / "signals.jsonl"
    if sig_path.exists():
        records = [json.loads(l) for l in sig_path.read_text().splitlines() if l.strip()]
        pnls = [r["pnl_pips"] for r in records
                if r.get("kind") == "resolution" and r.get("outcome") == "closed"]
        total, cum = 0.0, []
        for p in pnls:
            total += p
            cum.append(round(total, 1))
        forward = {"cum": cum, "n": len(pnls), "net": round(total, 1),
                   "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4)
                   if pnls else None}
    regime_history = [{"date": r["date"], "regime": r["decision"]["regime"],
                       "swapped": r["swapped"]}
                      for r in (json.loads(p.read_text())
                                for p in sorted(RUNS.glob("regime_*.json")))]
    from src.backtest import load_settings
    settings = load_settings()

    latest_evolution = _latest("evolution_*.json")
    evolution = None
    if latest_evolution:
        evolution = {
            "date": latest_evolution["date"],
            "attempted": True,
            "screened": [{"entry_signal": s["genome"]["entry_signal"],
                         "exit_style": s["genome"]["exit_style"],
                         "net_profit_pips": s["net_profit_pips"]}
                        for s in latest_evolution.get("screened", [])],
            "winner_genome": latest_evolution.get("winner_genome"),
            "accepted": latest_evolution.get("accepted"),
            "pr_url": latest_evolution.get("pr_url"),
            "pr_auto_merge_hours": settings["evolution"]["pr_auto_merge_hours"],
        }

    data = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "active": active, "regime": regime_log, "changes": changes,
            "divergence": divergence[-12:], "price": price, "forward": forward,
            "regime_history": regime_history[-30:], "evolution": evolution,
            "plain": {"text": plain_summary(settings, decision),
                      "money": _forward_money(settings),
                      "lot_size": settings["reporting"]["lot_size"]},
            "champion": (decision or {}).get("champion")}
    template = (ROOT / "docs" / "_template.html").read_text()
    page = template.replace("/*__DATA__*/null", json.dumps(data))
    (ROOT / "docs" / "index.html").write_text(page)
    print("dashboard rebuilt: docs/index.html")


def daily_report() -> None:
    from src.backtest import load_settings
    from src.forward import decay_check
    settings = load_settings()
    regime_log = _latest("regime_*.json") or {}
    decay = decay_check(settings)
    status = {"regime": regime_log.get("decision", {}), "swapped": regime_log.get("swapped"),
              "decay": decay}
    summary = deepseek_summary({"kind": "daily", "status": status})
    fields = [{"name": "Regime", "value": str(status["regime"].get("regime", "n/a")), "inline": True},
              {"name": "Decay trigger", "value": str(decay["decay_trigger"]), "inline": True}]
    if regime_log.get("swapped"):
        fields.append({"name": "Regime swap", "value": "active.json switched (routing)", "inline": False})
    fields.append({"name": "In plain English",
                   "value": plain_summary(settings)[:1024], "inline": False})
    color = 0xC53030 if decay["decay_trigger"] else 0x2F855A
    discord_notify("Crucible · daily monitor", summary, fields, color)
    build_dashboard()


def optimization_report() -> None:
    decision = _latest("decision_*.json")
    if not decision:
        print("no decision to report")
        return
    champ = decision["champion"]
    summary = deepseek_summary({"kind": "optimization", "decision": {
        **{k: decision[k] for k in ("date", "regime", "accepted", "gates", "params",
                                    "baseline_params", "candidate_oos", "baseline_oos")},
        "champion": {k: v for k, v in champ.items() if not k.endswith("_equity")}}})
    fields = [
        {"name": "Outcome", "value": "ACCEPTED" if decision["accepted"] else "REJECTED", "inline": True},
        {"name": "Regime", "value": decision["regime"], "inline": True},
        {"name": "OOS net (candidate vs baseline)",
         "value": f"{decision['candidate_oos']['net_profit_pips']} vs "
                  f"{decision['baseline_oos']['net_profit_pips']} pips", "inline": False},
        {"name": "Champion vs Challenger",
         "value": f"evolved {'leads' if champ['evolved_leads'] else 'TRAILS'} "
                  f"({champ['evolved']['net_profit_pips']} vs "
                  f"{champ['champion_zero']['net_profit_pips']} pips)", "inline": False},
        {"name": "Next run", "value": "Sunday 22:00 UTC (weekly cadence)", "inline": True},
    ]
    if champ.get("warning_90d"):
        fields.append({"name": "⚠ Champion warning",
                       "value": "evolved system trails Champion Zero over 90 days", "inline": False})
    from src.backtest import load_settings
    fields.append({"name": "In plain English",
                   "value": plain_summary(load_settings(), decision)[:1024], "inline": False})
    color = 0x2F855A if decision["accepted"] else 0x718096
    discord_notify("Crucible · optimization run", summary, fields, color)
    build_dashboard()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Crucible reporting")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--optimization", action="store_true")
    ap.add_argument("--dashboard-only", action="store_true")
    args = ap.parse_args()
    if args.daily:
        daily_report()
    elif args.optimization:
        optimization_report()
    elif args.dashboard_only:
        build_dashboard()
