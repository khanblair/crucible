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
    data = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "active": active, "regime": regime_log, "changes": changes,
            "divergence": divergence[-12:],
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
