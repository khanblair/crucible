"""Evaluator tests: every gate with a passing, a failing and a boundary case.

The gate numbers mirror config/settings.json but are passed explicitly so
each expected outcome is verifiable by hand.
"""
from src.evaluate import (evaluate_candidate, gate_consistency, gate_drawdown,
                          gate_improvement, gate_trades, gate_win_rate)

GATES = {"min_relative_improvement": 0.10, "min_oos_trades": 30,
         "max_drawdown": 0.12, "min_win_rate": 0.40,
         "max_consistency_dropoff": 0.50}


# --------------------------------------------------------------- improvement
def test_improvement_passes_above_ten_percent():
    assert gate_improvement(111.0, 100.0, 0.10)


def test_improvement_boundary_exact_ten_percent_goes_to_incumbent():
    assert not gate_improvement(110.0, 100.0, 0.10)


def test_improvement_fails_marginal_gain():
    assert not gate_improvement(109.0, 100.0, 0.10)


def test_improvement_negative_baseline():
    # must beat -10 by 10% of |baseline| => strictly above -9
    assert gate_improvement(-8.9, -10.0, 0.10)
    assert not gate_improvement(-9.0, -10.0, 0.10)   # boundary
    assert not gate_improvement(-9.5, -10.0, 0.10)


def test_improvement_zero_baseline_requires_positive():
    assert gate_improvement(0.1, 0.0, 0.10)
    assert not gate_improvement(0.0, 0.0, 0.10)      # tie goes to the incumbent


# --------------------------------------------------------------- trade floor
def test_trade_floor():
    assert gate_trades(31, 30)
    assert gate_trades(30, 30)          # boundary: "at least 30" passes
    assert not gate_trades(29, 30)


# ----------------------------------------------------------------- drawdown
def test_drawdown_ceiling():
    assert gate_drawdown(0.119, 0.12)
    assert not gate_drawdown(0.12, 0.12)   # boundary: must stay BELOW 12%
    assert not gate_drawdown(0.121, 0.12)


# ----------------------------------------------------------------- win rate
def test_win_rate_floor():
    assert gate_win_rate(0.401, 0.40)
    assert not gate_win_rate(0.40, 0.40)   # boundary: must stay ABOVE the floor
    assert not gate_win_rate(0.399, 0.40)


# -------------------------------------------------------------- consistency
def test_consistency():
    assert gate_consistency(60.0, 100.0, 0.50)
    assert gate_consistency(50.0, 100.0, 0.50)       # boundary: exactly half retained
    assert not gate_consistency(49.9, 100.0, 0.50)   # collapse = overfitting signature
    assert gate_consistency(5.0, -10.0, 0.50)        # nothing to collapse from


# --------------------------------------------------------------- full gate
def _metrics(net=120.0, n=40, dd=0.05, wr=0.55):
    return {"net_profit_pips": net, "n_trades": n, "max_drawdown": dd, "win_rate": wr}


def test_all_gates_pass_accepts():
    verdict = evaluate_candidate(_metrics(), _metrics(net=100.0),
                                 _metrics(net=150.0), {"gates": GATES})
    assert verdict["accepted"]
    assert all(g["passed"] for g in verdict["gates"])


def test_single_failing_gate_rejects():
    # everything excellent except the trade floor: still rejected
    verdict = evaluate_candidate(_metrics(n=29), _metrics(net=100.0),
                                 _metrics(net=150.0), {"gates": GATES})
    assert not verdict["accepted"]
    failed = [g["name"] for g in verdict["gates"] if not g["passed"]]
    assert failed == ["trade_floor"]


def test_overfit_candidate_rejected_despite_beating_baseline():
    # beats baseline OOS but collapsed from a huge training result
    verdict = evaluate_candidate(_metrics(net=120.0), _metrics(net=100.0),
                                 _metrics(net=300.0), {"gates": GATES})
    assert not verdict["accepted"]
    failed = [g["name"] for g in verdict["gates"] if not g["passed"]]
    assert failed == ["consistency"]
