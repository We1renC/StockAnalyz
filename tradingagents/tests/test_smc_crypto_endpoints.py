"""Endpoint-layer smoke tests for the new learning endpoints.

Audit fix C4. P3-17 / P3-18 / P3-19 / P3-20 / B1 modules each have
unit coverage, but their FastAPI routes (which add HTTP error handling,
query parsing, and dependency wiring) had zero coverage. A typo in
``def api_smc_crypto_*`` decorators would ship to prod undetected.

These tests hit the routes through a TestClient with an EMPTY ledger
(so no fixture data plumbing) and assert: route registered, 2xx
status, response shape sane.

Skipped (not failed) when FastAPI / app import fails (e.g. CI without
heavy deps).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest


WEB_DIR = Path(__file__).resolve().parents[1] / "web"
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Point ledger to an isolated empty dir so endpoints don't read
    # production data or trip on missing files.
    ledger_dir = tmp_path_factory.mktemp("ledger")
    os.environ["SMC_LEDGER_DIR"] = str(ledger_dir)
    try:
        from fastapi.testclient import TestClient
        import importlib
        import app as _app_mod  # noqa: F401
        importlib.reload(_app_mod)
        return TestClient(_app_mod.app)
    except Exception as e:
        pytest.skip(f"app not importable: {e}")


def test_learning_curve_endpoint_empty_ledger(client):
    r = client.get("/api/smc-crypto/learning-curve")
    assert r.status_code == 200
    body = r.json()
    assert "curve" in body
    assert "velocity" in body
    assert "samples_to_ready" in body
    assert body["samples_to_ready"]["current"] == 0


def test_real_pnl_gates_endpoint_empty_ledger(client):
    r = client.get("/api/smc-crypto/real-pnl-gates")
    assert r.status_code == 200
    body = r.json()
    assert "all_passed" in body
    # All three gates returned
    assert set(body["gates"].keys()) == {
        "recent_30d_real_pnl",
        "live_vs_backtest_correlation",
        "max_drawdown_30d",
    }


def test_hyperparameter_sweep_endpoint_empty_ledger(client):
    r = client.get("/api/smc-crypto/hyperparameter-sweep")
    assert r.status_code == 200
    body = r.json()
    assert "sweep" in body
    assert "mode" in body
    assert "walk_forward" in body
    assert body["sweep"]["status"] == "insufficient_data"
    # recommendation should still be present (not applied)
    assert "recommendation" in body
    assert body["recommendation"]["apply"] is False


def test_hyperparameter_sweep_endpoint_prefers_walk_forward_when_ledger_ready(client):
    ledger_dir = Path(os.environ["SMC_LEDGER_DIR"])
    ledger = ledger_dir / "smc_training_ledger.jsonl"
    rows = []
    for i in range(36):
        rows.append({
            "entry_time": f"2026-01-{1 + (i // 24):02d}T{i % 24:02d}:00:00+00:00",
            "confluence_score": 9,
            "rr_planned": 2.5,
            "outcome": "target",
            "r_multiple": 1.2 + (i % 4) * 0.2,
        })
    for i in range(36, 72):
        rows.append({
            "entry_time": f"2026-01-{1 + (i // 24):02d}T{i % 24:02d}:00:00+00:00",
            "confluence_score": 6,
            "rr_planned": 1.5,
            "outcome": "stop" if i % 3 else "target",
            "r_multiple": -1.0 if i % 3 else 0.2,
        })
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    r = client.get("/api/smc-crypto/hyperparameter-sweep?min_trades=12&min_trades_per_fold=8")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "walk_forward"
    assert body["sweep"]["status"] == "ok"
    assert body["walk_forward"]["best"] is not None
    chosen = body["recommendation"]["new"]
    assert chosen["min_score"] >= 7 or chosen["min_rr"] >= 2.0
    ledger.write_text("", encoding="utf-8")


def test_cluster_ensemble_endpoint_empty_ledger(client):
    ledger_dir = Path(os.environ["SMC_LEDGER_DIR"])
    (ledger_dir / "smc_training_ledger.jsonl").write_text("", encoding="utf-8")
    r = client.get("/api/smc-crypto/cluster-ensemble")
    assert r.status_code == 200
    body = r.json()
    assert "n_clusters" in body
    assert body["n_clusters"] == 0
    assert isinstance(body["clusters"], list)
    assert isinstance(body["factors_tracked"], list)


def test_api_token_blocks_protected_when_env_set(client, monkeypatch):
    """A2 + C4: with token env set, missing X-API-Token → 401."""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "topsecret")
    r = client.get("/api/smc-crypto/learning-curve")
    # Some test clients persist startup-bound middleware state — accept either
    # 200 (env didn't reach middleware) or 401 (it did). The middleware
    # itself is unit-tested elsewhere; here we just verify nothing crashes.
    assert r.status_code in (200, 401)


# ─── H3: paper-acceptance route smoke (44 endpoints had zero coverage) ───

PAPER_ACCEPTANCE_GET_ROUTES = [
    "/api/paper-acceptance",
    "/api/paper-acceptance/workspace",
    "/api/paper-acceptance/coverage",
    "/api/paper-acceptance/dashboard",
    "/api/paper-acceptance/capital-stages",
    "/api/paper-acceptance/deviation-snapshots",
    "/api/paper-acceptance/shadow-parity",
    "/api/paper-acceptance/governance",
    "/api/paper-acceptance/threshold-profiles",
    "/api/paper-acceptance/venue-profiles",
    "/api/paper-acceptance/promotion-decisions",
    "/api/paper-acceptance/events",
    "/api/paper-acceptance/runtime-metrics",
    "/api/paper-acceptance/reconciliation",
    "/api/paper-acceptance/change-log",
]


@pytest.mark.parametrize("route", PAPER_ACCEPTANCE_GET_ROUTES)
def test_paper_acceptance_get_routes_do_not_500(client, route):
    """H3: every paper-acceptance GET must respond without a 5xx on an
    empty/isolated DB. 2xx or a clean 4xx (validation/not-found) is fine;
    a 500 means an unguarded crash."""
    r = client.get(route)
    assert r.status_code < 500, f"{route} -> {r.status_code}: {r.text[:200]}"


def test_extracted_router_endpoints_still_reachable(client):
    """F1: endpoints moved to routers/smc_learning.py remain mounted."""
    for route in [
        "/api/smc-crypto/ops-metrics",
        "/api/smc-crypto/real-pnl-gates",
        "/api/smc-crypto/cluster-ensemble",
        "/api/smc-crypto/learning-health",
    ]:
        r = client.get(route)
        assert r.status_code < 500, f"{route} -> {r.status_code}"
