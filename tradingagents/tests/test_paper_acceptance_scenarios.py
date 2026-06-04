"""Tests for acceptance abnormal scenario harness."""

import sqlite3

from paper_acceptance_scenarios import (
    SCENARIO_LIBRARY,
    ensure_paper_acceptance_scenario_schema,
    load_scenario_runs,
    run_acceptance_scenario,
    summarize_scenario_evidence,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_scenario_library_covers_key_abnormal_and_risk_sections():
    gates = {item.gate_id for item in SCENARIO_LIBRARY}
    assert {"network_abnormality", "market_abnormality", "program_abnormality", "position_risk", "loss_risk", "kill_switch", "unknown_order_state"}.issubset(gates)


def test_run_acceptance_scenario_persists_traceable_run():
    conn = _conn()
    ensure_paper_acceptance_scenario_schema(conn)

    row = run_acceptance_scenario(conn, symbol="ABAT", scenario_id="rest_timeout_unknown_state")
    rows = load_scenario_runs(conn, symbol="ABAT")
    evidence = summarize_scenario_evidence(conn, symbol="ABAT")

    assert row["scenario_id"] == "rest_timeout_unknown_state"
    assert row["status"] == "pass"
    assert rows[0]["scenario_key"] == row["scenario_key"]
    assert evidence["evidence"]["unknown_order_state"]["no_blind_resend"] is True


def test_position_limit_and_kill_switch_scenarios_reflect_control_outcome():
    conn = _conn()

    run_acceptance_scenario(conn, symbol="ABAT", scenario_id="position_limit_reject")
    run_acceptance_scenario(conn, symbol="ABAT", scenario_id="kill_switch_blocks_orders")
    evidence = summarize_scenario_evidence(conn, symbol="ABAT")

    assert evidence["evidence"]["position_risk"]["limit_rejection_tested"] is True
    assert evidence["evidence"]["kill_switch"]["new_orders_blocked_after_shutdown"] is True
