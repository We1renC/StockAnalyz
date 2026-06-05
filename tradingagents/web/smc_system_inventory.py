"""SMC × crypto-api × paper-acceptance × learning — sub-system integration audit.

Tells you, for every sub-module that lives in this repo:

  • What public primitives it exposes
  • Whether those primitives are actually called from the workflow
    layer (smc_unified_system / smc_auto_workflow / smc_training_loop /
    smc_learning_orchestrator / app.py endpoints)
  • Which Obsidian writers cover its output

The output is a single structured dict suitable for both an API
response and an Obsidian markdown export.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Static taxonomy — keep in sync as new modules land.
# ---------------------------------------------------------------------------

SUB_SYSTEMS: dict[str, dict] = {
    "smc_strategy": {
        "title": "SMC 策略引擎",
        "file": "web/smc_quant.py",
        "spec_refs": ["§3", "§4", "§5", "§6", "§17", "§18"],
        "key_primitives": [
            "build_smc_analysis", "detect_swings", "detect_order_blocks",
            "detect_fvgs", "detect_liquidity", "detect_judas_swings",
            "detect_smt_divergence",
            "detect_sweep_reversal_entries", "detect_continuation_entries",
            "detect_ote_entries", "detect_unicorn_entries",
            "detect_silver_bullet_entries", "detect_power_of_three_entries",
            "apply_risk_pipeline", "calculate_position_size",
        ],
        "consumed_by": ["smc_paper_runner", "smc_unified_system",
                          "smc_auto_workflow", "smc_training_loop",
                          "smc_learning_orchestrator"],
    },
    "smc_paper_runner": {
        "title": "Paper 交易 runner",
        "file": "web/smc_paper_runner.py",
        "spec_refs": ["§10.5"],
        "key_primitives": ["CryptoApiClient", "SmcPaperRunner.run_once",
                            "_pick_best_entry", "_build_order_payload"],
        "consumed_by": ["smc_unified_system", "smc_auto_workflow"],
    },
    "smc_unified_system": {
        "title": "統一 4-phase 編排",
        "file": "web/smc_unified_system.py",
        "spec_refs": ["§10.5", "§10.6", "§18.5"],
        "key_primitives": ["UnifiedTradingSession.run",
                            "propose_signals", "dry_run_signals",
                            "live_paper_session", "build_session_acceptance"],
        "consumed_by": ["smc_auto_workflow", "smc_training_loop", "app.py"],
    },
    "smc_auto_workflow": {
        "title": "幣種一鍵自動工作流",
        "file": "web/smc_auto_workflow.py",
        "spec_refs": ["§10.5", "§12.3", "§17.6", "§17.8"],
        "key_primitives": ["profile_for_symbol", "preflight",
                            "cooldown_remaining", "run_symbol"],
        "consumed_by": ["app.py /api/smc-crypto/auto-run"],
    },
    "smc_training_loop": {
        "title": "策略自我訓練閉環",
        "file": "web/smc_training_loop.py",
        "spec_refs": ["§10.4", "§10.6", "§18.3", "§18.5", "§18.6"],
        "key_primitives": ["auto_backtest_window", "ingest_acceptance_evidence",
                            "train_from_ledger", "run_scenarios_for_symbol",
                            "audit_learning_capability", "run_training_cycle"],
        "consumed_by": ["smc_unified_system", "smc_learning_orchestrator",
                          "app.py /api/smc-crypto/train"],
    },
    "smc_learning_orchestrator": {
        "title": "學習能力總指揮 (7 層 24 基件)",
        "file": "web/smc_learning_orchestrator.py",
        "spec_refs": ["§10.6", "§18.3", "§18.4", "§18.5", "§18.6"],
        "key_primitives": ["build_learning_report", "apply_proposed_changes",
                            "layer_statistics", "layer_attribution",
                            "layer_calibration", "layer_validation",
                            "layer_ml", "layer_proposal",
                            "layer_acceptance", "layer_adaptive"],
        "consumed_by": ["app.py /api/smc-crypto/learning-report",
                          "app.py /api/smc-crypto/learning-apply"],
    },
    "crypto_api": {
        "title": "Binance Spot 相容模擬撮合 API",
        "file": "web/crypto_api/router.py",
        "spec_refs": ["§17.8", "§17.9"],
        "key_primitives": ["create_order", "cancel_order", "get_balances",
                            "get_open_orders", "get_fills", "kill_switch",
                            "rate_limit", "validate_pre_trade_risk"],
        "consumed_by": ["smc_paper_runner.CryptoApiClient", "TestClient(app)"],
    },
    "paper_execution": {
        "title": "Paper 撮合模擬器 (slippage/fee)",
        "file": "web/paper_execution.py",
        "spec_refs": ["§10.5"],
        "key_primitives": ["simulate_market_order", "simulate_limit_order",
                            "check_order_risk", "handle_unknown_order_state"],
        "consumed_by": ["smc_unified_system.dry_run_signals"],
    },
    "paper_acceptance": {
        "title": "驗收 21-gate 評估器",
        "file": "web/paper_acceptance.py",
        "spec_refs": ["quant_paper_trading_acceptance_v1.0"],
        "key_primitives": ["build_acceptance_report", "evaluate_gate",
                            "evaluate_prohibitions", "determine_conclusion",
                            "render_acceptance_markdown", "acceptance_catalog"],
        "consumed_by": ["smc_unified_system.build_session_acceptance"],
    },
    "paper_acceptance_store": {
        "title": "驗收 SQLite 儲存層",
        "file": "web/paper_acceptance_store.py",
        "spec_refs": ["§18.2"],
        "key_primitives": ["ensure_paper_acceptance_schema",
                            "persist_acceptance_report",
                            "load_acceptance_reports",
                            "record_acceptance_event",
                            "upsert_acceptance_context_overrides"],
        "consumed_by": ["smc_unified_system.persist",
                          "smc_auto_workflow.preflight"],
    },
    "paper_acceptance_metrics": {
        "title": "驗收 runtime telemetry",
        "file": "web/paper_acceptance_metrics.py",
        "spec_refs": ["§10.4", "§18.3"],
        "key_primitives": ["record_runtime_metric", "record_order_audit",
                            "record_virtual_account_snapshot",
                            "record_alert_delivery", "record_stability_session",
                            "record_reconciliation_run",
                            "summarize_acceptance_telemetry"],
        "consumed_by": ["smc_training_loop.ingest_acceptance_evidence",
                          "smc_learning_orchestrator.layer_acceptance"],
    },
    "paper_acceptance_scenarios": {
        "title": "驗收 12 個 stress scenario",
        "file": "web/paper_acceptance_scenarios.py",
        "spec_refs": ["§10.4"],
        "key_primitives": ["scenario_catalog", "run_acceptance_scenario",
                            "summarize_scenario_evidence"],
        "consumed_by": ["smc_training_loop.run_scenarios_for_symbol",
                          "smc_learning_orchestrator.layer_acceptance"],
    },
    "paper_acceptance_policy": {
        "title": "升級階梯與門檻治理",
        "file": "web/paper_acceptance_policy.py",
        "spec_refs": ["promotion_ladder"],
        "key_primitives": ["build_acceptance_policy_snapshot"],
        "consumed_by": ["smc_learning_orchestrator.layer_acceptance"],
    },
    "paper_acceptance_security": {
        "title": "驗收 security hygiene 自動掃描",
        "file": "web/paper_acceptance_security.py",
        "spec_refs": ["security_hygiene"],
        "key_primitives": ["run_security_scan"],
        "consumed_by": ["smc_learning_orchestrator.layer_acceptance"],
    },
    "obsidian_sync": {
        "title": "Obsidian Vault 雙向同步",
        "file": "web/app.py (writer helpers)",
        "spec_refs": ["§11.3"],
        "key_primitives": [
            "_obsidian_write_position", "_obsidian_write_portfolio_index",
            "_obsidian_write_watchlist_item", "_obsidian_write_alert",
            "_obsidian_write_analysis", "_obsidian_write_smc_journal",
            "_obsidian_write_technical_matrix",
            "_obsidian_write_financials", "_obsidian_write_fundamentals",
        ],
        "consumed_by": ["app.py /api/obsidian-sync",
                          "app.py /api/obsidian-export"],
    },
}


@dataclass
class SubSystemRow:
    key: str
    title: str
    file: str
    file_exists: bool
    function_count: int        # actual `def `s in the source
    primitives_listed: int
    primitives_present: int
    consumed_by: list[str]
    integration_status: str    # "wired" / "partial" / "orphan"
    obsidian_writer: Optional[str]
    notes: list[str] = field(default_factory=list)


def _scan_module(path: Path) -> tuple[int, set[str]]:
    """Return (def count, set of public function names)."""
    if not path.exists():
        return 0, set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return 0, set()
    names: set[str] = set()
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count += 1
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}.{item.name}")
    return count, names


# Map sub-system → Obsidian writer responsible for surfacing its output
OBSIDIAN_COVERAGE: dict[str, Optional[str]] = {
    "smc_strategy":              "_obsidian_write_smc_journal",
    "smc_paper_runner":          None,
    "smc_unified_system":        None,     # ← gap
    "smc_auto_workflow":         None,     # ← gap
    "smc_training_loop":         None,     # ← gap
    "smc_learning_orchestrator": None,     # ← gap
    "crypto_api":                None,     # ← gap
    "paper_execution":           None,
    "paper_acceptance":          None,     # ← gap (acceptance reports)
    "paper_acceptance_store":    None,     # ← gap (run history)
    "paper_acceptance_metrics":  None,     # ← gap (telemetry)
    "paper_acceptance_scenarios": None,    # ← gap (scenarios)
    "paper_acceptance_policy":   None,
    "paper_acceptance_security": None,
    "obsidian_sync":             "(itself)",
}


def build_inventory(*, project_root: Optional[Path] = None) -> dict:
    """Walk SUB_SYSTEMS, scan files, compute integration status."""
    root = project_root or Path(__file__).parent.parent
    rows: list[SubSystemRow] = []
    for key, meta in SUB_SYSTEMS.items():
        file_path = root / meta["file"]
        exists = file_path.exists()
        fn_count, fn_names = _scan_module(file_path) if exists else (0, set())
        listed = meta.get("key_primitives", [])
        present = [p for p in listed if (p.split(".")[0] in fn_names) or (p in fn_names)]
        # Integration heuristic:
        consumed_by = meta.get("consumed_by", [])
        if not consumed_by:
            status = "orphan"
        elif len(present) >= 0.7 * max(1, len(listed)):
            status = "wired"
        else:
            status = "partial"
        notes: list[str] = []
        missing = [p for p in listed if p not in present]
        if missing:
            notes.append(f"primitives not found: {missing[:3]}{'…' if len(missing)>3 else ''}")
        rows.append(SubSystemRow(
            key=key, title=meta["title"], file=meta["file"],
            file_exists=exists, function_count=fn_count,
            primitives_listed=len(listed),
            primitives_present=len(present),
            consumed_by=consumed_by,
            integration_status=status,
            obsidian_writer=OBSIDIAN_COVERAGE.get(key),
            notes=notes,
        ))
    summary = {
        "total_subsystems": len(rows),
        "wired": sum(1 for r in rows if r.integration_status == "wired"),
        "partial": sum(1 for r in rows if r.integration_status == "partial"),
        "orphan": sum(1 for r in rows if r.integration_status == "orphan"),
        "with_obsidian_coverage": sum(1 for r in rows if r.obsidian_writer),
        "missing_obsidian_coverage": sum(1 for r in rows if not r.obsidian_writer),
        "total_functions": sum(r.function_count for r in rows),
    }
    return {
        "summary": summary,
        "rows": [asdict(r) for r in rows],
    }


def render_inventory_markdown(report: dict) -> str:
    """Render an inventory dict as an Obsidian-friendly markdown table."""
    s = report["summary"]
    lines = [
        "---",
        "title: SMC 大系統子模組整合盤點",
        "tags: [smc, inventory, integration]",
        "generated_at: " + __import__("datetime").datetime.utcnow().isoformat(timespec="seconds"),
        "---",
        "",
        "# SMC 大系統子模組整合盤點",
        "",
        f"## 摘要",
        f"",
        f"- 子系統總數: **{s['total_subsystems']}**",
        f"- 已串入 (wired): **{s['wired']}**",
        f"- 部分串入 (partial): **{s['partial']}**",
        f"- 孤兒 (orphan): **{s['orphan']}**",
        f"- Obsidian 已覆蓋: **{s['with_obsidian_coverage']}**",
        f"- Obsidian 待補: **{s['missing_obsidian_coverage']}**",
        f"- 函式總數: **{s['total_functions']}**",
        "",
        "## 子系統清單",
        "",
        "| 子系統 | 檔案 | 函式數 | 基件 in/listed | 整合狀態 | Obsidian writer | 被誰使用 |",
        "|---|---|---:|---|:-:|---|---|",
    ]
    for r in report["rows"]:
        status_icon = {"wired":"✅","partial":"⚠️","orphan":"❌"}.get(r["integration_status"],"?")
        ob = r["obsidian_writer"] or "❌"
        consumers = ", ".join(r["consumed_by"][:3]) + ("…" if len(r["consumed_by"]) > 3 else "")
        if not consumers: consumers = "—"
        lines.append(
            f"| **{r['title']}** | `{r['file']}` | {r['function_count']} | "
            f"{r['primitives_present']}/{r['primitives_listed']} | {status_icon} | {ob} | {consumers} |"
        )
    lines += ["", "## 整合缺口", ""]
    for r in report["rows"]:
        if r["integration_status"] != "wired" or not r["obsidian_writer"]:
            issues = []
            if r["integration_status"] != "wired":
                issues.append(f"integration={r['integration_status']}")
            if not r["obsidian_writer"]:
                issues.append("無 Obsidian writer")
            if r["notes"]:
                issues.append("; ".join(r["notes"]))
            lines.append(f"- **{r['title']}** (`{r['key']}`): " + " · ".join(issues))
    return "\n".join(lines) + "\n"
