"""Paper-acceptance endpoints (audit fix F1 / Round L).

The 46-endpoint paper-acceptance group — the largest remaining slice of
the app.py monolith (S5) — extracted to its own router. Depends only on
deps.get_db / deps.sanitize_float_values + the paper_acceptance* modules,
so no circular import with app.

Mounted via app.include_router(paper_acceptance.router).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from deps import get_db, sanitize_float_values
from paper_acceptance import build_acceptance_report, render_acceptance_markdown
from paper_acceptance_store import (
    build_acceptance_workspace,
    build_and_persist_smc_acceptance_report,
    build_smc_acceptance_context,
    delete_acceptance_check,
    ensure_paper_acceptance_schema,
    load_alert_deliveries,
    load_acceptance_context_overrides,
    load_acceptance_change_log,
    load_acceptance_events,
    load_governance_events,
    load_acceptance_reports,
    load_acceptance_review,
    load_capital_stages,
    load_deviation_snapshots,
    load_order_audit_rows,
    load_promotion_decisions,
    load_reconciliation_runs,
    load_runtime_metrics,
    load_scenario_runs,
    load_shadow_parity_traces,
    load_stability_sessions,
    load_threshold_profiles,
    load_venue_profiles,
    load_virtual_account_snapshots,
    record_alert_delivery,
    record_acceptance_change,
    record_acceptance_event,
    record_capital_stage,
    record_deviation_snapshot,
    record_governance_event,
    record_promotion_decision,
    record_threshold_profile,
    record_venue_profile,
    record_shadow_parity_trace,
    record_stability_session,
    record_order_audit,
    record_reconciliation_run,
    record_runtime_metric,
    record_virtual_account_snapshot,
    refresh_acceptance_reports_for_symbols,
    run_acceptance_scenario,
    summarize_governance_events,
    summarize_promotion_decisions,
    summarize_shadow_parity_traces,
    summarize_threshold_profiles,
    summarize_venue_profiles,
    upsert_acceptance_review,
    upsert_acceptance_check,
    upsert_acceptance_context_overrides,
)

router = APIRouter()


@router.get("/api/paper-acceptance")
def api_get_paper_acceptance_reports(symbol: Optional[str] = None, limit: int = 50):
    conn = get_db()
    reports = load_acceptance_reports(conn, symbol=symbol, limit=limit)
    conn.close()
    return sanitize_float_values({"reports": reports, "count": len(reports)})


@router.get("/api/paper-acceptance/workspace")
def api_get_paper_acceptance_workspace(symbol: str, stage: str = "paper", limit_reports: int = 5):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=limit_reports)
        return sanitize_float_values(payload)
    finally:
        conn.close()


@router.put("/api/paper-acceptance/workspace")
def api_update_paper_acceptance_workspace(req: PaperAcceptanceWorkspaceUpdate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        current = load_acceptance_context_overrides(conn, req.symbol.strip().upper(), stage=req.stage)
        updated = upsert_acceptance_context_overrides(
            conn,
            symbol=req.symbol.strip().upper(),
            stage=req.stage,
            strategy=req.strategy if req.strategy is not None else current["strategy"],
            metrics=req.metrics if req.metrics is not None else current["metrics"],
            prohibitions=req.prohibitions if req.prohibitions is not None else current["prohibitions"],
        )
        return sanitize_float_values({"ok": True, "workspace": updated})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/review")
def api_get_paper_acceptance_review(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = load_acceptance_review(conn, symbol.strip().upper(), stage=stage)
        return sanitize_float_values(payload)
    finally:
        conn.close()


@router.get("/api/paper-acceptance/promotion")
def api_get_paper_acceptance_promotion(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=5)
        return sanitize_float_values({
            "symbol": payload["symbol"],
            "stage": payload["stage"],
            "policy": payload.get("policy") or {},
            "review": payload.get("review") or {},
        })
    finally:
        conn.close()


@router.get("/api/paper-acceptance/coverage")
def api_get_paper_acceptance_coverage(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=5)
        return sanitize_float_values({
            "symbol": payload["symbol"],
            "stage": payload["stage"],
            "coverage": payload.get("coverage") or {},
        })
    finally:
        conn.close()


@router.get("/api/paper-acceptance/closure")
def api_get_paper_acceptance_closure(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=5)
        return sanitize_float_values({
            "symbol": payload["symbol"],
            "stage": payload["stage"],
            "closure_summary": payload.get("closure_summary") or {},
            "policy": payload.get("policy") or {},
            "review": payload.get("review") or {},
            "production_checklist": payload.get("production_checklist") or [],
        })
    finally:
        conn.close()


@router.get("/api/paper-acceptance/change-log")
def api_get_paper_acceptance_change_log(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_acceptance_change_log(conn, symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/promotion-check")
def api_get_paper_acceptance_promotion_check(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=5)
        policy = payload.get("policy") or {}
        blockers = list(policy.get("blockers") or [])
        promotion_summary = payload.get("promotion_summary") or {}
        if policy.get("can_promote"):
            decision = "allow"
        elif blockers:
            decision = "deny"
        else:
            decision = "conditional"
        return sanitize_float_values({
            "symbol": payload["symbol"],
            "stage": payload["stage"],
            "decision": decision,
            "recorded_decision": promotion_summary.get("latest_decision") or "missing",
            "policy": policy,
            "review": payload.get("review") or {},
            "promotion_summary": promotion_summary,
            "production_checklist": payload.get("production_checklist") or [],
        })
    finally:
        conn.close()


@router.get("/api/paper-acceptance/security-scan")
def api_get_paper_acceptance_security_scan(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=1)
        return sanitize_float_values({
            "symbol": payload["symbol"],
            "stage": payload["stage"],
            "security_scan": payload.get("security_scan") or {},
        })
    finally:
        conn.close()


@router.get("/api/paper-acceptance/dashboard")
def api_get_paper_acceptance_dashboard(symbol: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = build_acceptance_workspace(conn, symbol=symbol.strip().upper(), stage=stage, limit_reports=5)
        report = payload.get("report") or {}
        summary = report.get("summary") or {}
        events = payload.get("events") or []
        warning_event_count = sum(1 for row in events if str(row.get("severity") or "").lower() in {"warning", "error", "critical"})
        reconciliation_runs = payload.get("reconciliation_runs") or []
        unresolved_reconciliation_count = sum(
            1
            for row in reconciliation_runs
            if str(row.get("status") or "").lower() not in {"ok", "resolved", "pass"}
            or str(row.get("severity") or "").lower() in {"warning", "error", "critical"}
        )
        return sanitize_float_values({
            "symbol": payload["symbol"],
            "stage": payload["stage"],
            "overview": {
                "conclusion": summary.get("conclusion"),
                "blocking_issue_count": summary.get("blocking_issue_count"),
                "recommendation": (payload.get("policy") or {}).get("recommendation"),
                "review_status": (payload.get("review") or {}).get("review_status"),
            },
            "monitoring_dashboard": ((payload.get("policy") or {}).get("evidence") or {}).get("monitoring_dashboard") or {},
            "virtual_account": {
                "snapshots": payload.get("virtual_account_snapshots") or [],
                "latest": (payload.get("virtual_account_snapshots") or [{}])[0] if payload.get("virtual_account_snapshots") else {},
            },
            "stability": {
                "sessions": payload.get("stability_sessions") or [],
                "latest": (payload.get("stability_sessions") or [{}])[0] if payload.get("stability_sessions") else {},
            },
            "venue_profile": payload.get("venue_summary") or {},
            "threshold_profile": payload.get("threshold_summary") or {},
            "promotion_summary": payload.get("promotion_summary") or {},
            "closure_summary": payload.get("closure_summary") or {},
            "event_summary": {
                "total": len(events),
                "warning_or_higher": warning_event_count,
                "latest": events[0] if events else {},
            },
            "reconciliation_summary": {
                "total": len(reconciliation_runs),
                "unresolved_count": unresolved_reconciliation_count,
                "latest": reconciliation_runs[0] if reconciliation_runs else {},
            },
            "runtime_summary": {
                "count": len(payload.get("runtime_metrics") or []),
                "latest": (payload.get("runtime_metrics") or [{}])[0] if payload.get("runtime_metrics") else {},
            },
            "production_checklist": payload.get("production_checklist") or [],
        })
    finally:
        conn.close()


@router.get("/api/paper-acceptance/capital-stages")
def api_get_paper_acceptance_capital_stages(symbol: str, stage: str = "paper", limit: int = 50):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_capital_stages(conn, symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/capital-stages")
def api_record_paper_acceptance_capital_stage(req: PaperAcceptanceCapitalStageCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.stage_name.strip():
        raise HTTPException(400, "stage_name is required")
    conn = get_db()
    try:
        row = record_capital_stage(
            conn,
            symbol=req.symbol.strip().upper(),
            stage_name=req.stage_name.strip(),
            capital_ratio=req.capital_ratio,
            capital_range_label=req.capital_range_label,
            trade_count=req.trade_count,
            observation_days=req.observation_days,
            slippage_bps=req.slippage_bps,
            fill_rate=req.fill_rate,
            drawdown=req.drawdown,
            note=req.note,
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/deviation-snapshots")
def api_get_paper_acceptance_deviation_snapshots(symbol: str, stage: str = "paper", limit: int = 50):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_deviation_snapshots(conn, symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/deviation-snapshots")
def api_record_paper_acceptance_deviation_snapshot(req: PaperAcceptanceDeviationSnapshotCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_deviation_snapshot(
            conn,
            symbol=req.symbol.strip().upper(),
            baseline_source=req.baseline_source,
            comparison_source=req.comparison_source,
            win_rate_delta=req.win_rate_delta,
            fill_rate_delta=req.fill_rate_delta,
            slippage_delta_bps=req.slippage_delta_bps,
            drawdown_delta=req.drawdown_delta,
            holding_time_delta_minutes=req.holding_time_delta_minutes,
            trade_frequency_delta=req.trade_frequency_delta,
            deviation_score=req.deviation_score,
            detail=req.detail or {},
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/shadow-parity")
def api_get_paper_acceptance_shadow_parity(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_shadow_parity_traces(conn, symbol.strip().upper(), stage=stage, limit=limit)
        summary = summarize_shadow_parity_traces(conn, symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "summary": summary, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/shadow-parity")
def api_record_paper_acceptance_shadow_parity(req: PaperAcceptanceShadowParityCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_shadow_parity_trace(
            conn,
            symbol=req.symbol.strip().upper(),
            runtime_stage=req.runtime_stage,
            market_timestamp=req.market_timestamp,
            signal_timestamp=req.signal_timestamp,
            risk_timestamp=req.risk_timestamp,
            order_intent_timestamp=req.order_intent_timestamp,
            adapter_timestamp=req.adapter_timestamp,
            adapter_name=req.adapter_name,
            side=req.side,
            order_type=req.order_type,
            requested_qty=req.requested_qty,
            signal_price=req.signal_price,
            expected_price=req.expected_price,
            execution_latency_ms=req.execution_latency_ms,
            market_data_source_shared=req.market_data_source_shared,
            signal_process_shared=req.signal_process_shared,
            risk_module_shared=req.risk_module_shared,
            order_generation_shared=req.order_generation_shared,
            logging_alerting_shared=req.logging_alerting_shared,
            no_exchange_submission=req.no_exchange_submission,
            order_book_snapshot_recorded=req.order_book_snapshot_recorded,
            likely_execution_price_recorded=req.likely_execution_price_recorded,
            post_order_price_behavior_recorded=req.post_order_price_behavior_recorded,
            parity_score=req.parity_score,
            detail=req.detail or {},
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/governance")
def api_get_paper_acceptance_governance(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_governance_events(conn, symbol.strip().upper(), stage=stage, limit=limit)
        summary = summarize_governance_events(conn, symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "summary": summary, "count": len(rows)})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/threshold-profiles")
def api_get_paper_acceptance_threshold_profiles(symbol: str, stage: str = "paper", limit: int = 50):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        key = symbol.strip().upper()
        rows = load_threshold_profiles(conn, key, stage=stage, limit=limit)
        summary = summarize_threshold_profiles(conn, key, stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "summary": summary, "count": len(rows)})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/venue-profiles")
def api_get_paper_acceptance_venue_profiles(symbol: str, stage: str = "paper", limit: int = 50):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        key = symbol.strip().upper()
        rows = load_venue_profiles(conn, key, stage=stage, limit=limit)
        summary = summarize_venue_profiles(conn, key, stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "summary": summary, "count": len(rows)})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/promotion-decisions")
def api_get_paper_acceptance_promotion_decisions(symbol: str, stage: str = "paper", limit: int = 50):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        key = symbol.strip().upper()
        rows = load_promotion_decisions(conn, key, stage=stage, limit=limit)
        summary = summarize_promotion_decisions(conn, key, stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "summary": summary, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/threshold-profiles")
def api_record_paper_acceptance_threshold_profile(req: PaperAcceptanceThresholdProfileCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_threshold_profile(
            conn,
            symbol=req.symbol.strip().upper(),
            strategy_type=req.strategy_type,
            profile_name=req.profile_name,
            status=req.status,
            thresholds=req.thresholds or {},
            source_summary=req.source_summary or {},
            approved_by=req.approved_by,
            version_tag=req.version_tag,
            note=req.note,
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/venue-profiles")
def api_record_paper_acceptance_venue_profile(req: PaperAcceptanceVenueProfileCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.venue_name.strip():
        raise HTTPException(400, "venue_name is required")
    conn = get_db()
    try:
        row = record_venue_profile(
            conn,
            symbol=req.symbol.strip().upper(),
            venue_name=req.venue_name.strip(),
            broker_name=req.broker_name,
            market_type=req.market_type,
            status=req.status,
            maker_fee_bps=req.maker_fee_bps,
            taker_fee_bps=req.taker_fee_bps,
            transaction_tax_bps=req.transaction_tax_bps,
            min_notional=req.min_notional,
            tick_size=req.tick_size,
            lot_size=req.lot_size,
            quantity_precision=req.quantity_precision,
            price_precision=req.price_precision,
            rate_limit_per_minute=req.rate_limit_per_minute,
            rate_limit_burst=req.rate_limit_burst,
            reject_taxonomy=req.reject_taxonomy or {},
            source_summary=req.source_summary or {},
            approved_by=req.approved_by,
            version_tag=req.version_tag,
            note=req.note,
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/promotion-decisions")
def api_record_paper_acceptance_promotion_decision(req: PaperAcceptancePromotionDecisionCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        key = req.symbol.strip().upper()
        workspace = build_acceptance_workspace(conn, symbol=key, stage=req.stage, limit_reports=5)
        closure = workspace.get("closure_summary") or {}
        policy = workspace.get("policy") or {}
        ladder = policy.get("promotion_ladder") or {}
        row = record_promotion_decision(
            conn,
            symbol=key,
            from_stage_name=req.from_stage_name or (ladder.get("current_stage") or {}).get("stage_name") or "",
            target_stage_name=req.target_stage_name or (ladder.get("next_stage") or {}).get("stage_name") or "",
            decision=req.decision,
            approved_by=req.approved_by or workspace.get("review", {}).get("reviewer") or "",
            review_status=req.review_status or workspace.get("review", {}).get("review_status") or "",
            threshold_profile_version_tag=(
                req.threshold_profile_version_tag
                or (workspace.get("threshold_summary") or {}).get("active_version_tag")
                or ""
            ),
            blocker_snapshot=req.blocker_snapshot if req.blocker_snapshot is not None else list(policy.get("blockers") or []),
            threshold_snapshot=req.threshold_snapshot if req.threshold_snapshot is not None else dict(policy.get("thresholds") or {}),
            rationale=req.rationale if req.rationale is not None else list(closure.get("rationale") or []),
            required_actions=(
                req.required_actions
                if req.required_actions is not None
                else list(closure.get("required_actions") or [])
            ),
            note=req.note,
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/governance")
def api_record_paper_acceptance_governance(req: PaperAcceptanceGovernanceEventCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_governance_event(
            conn,
            symbol=req.symbol.strip().upper(),
            change_scope=req.change_scope,
            change_class=req.change_class,
            version_tag=req.version_tag,
            approved_by=req.approved_by,
            requires_restart_stats=req.requires_restart_stats,
            stats_restarted=req.stats_restarted,
            freeze_window_started_at=req.freeze_window_started_at,
            freeze_window_ended_at=req.freeze_window_ended_at,
            event_timestamp=req.event_timestamp,
            reason=req.reason,
            detail=req.detail or {},
            stage=req.stage,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.put("/api/paper-acceptance/review")
def api_update_paper_acceptance_review(req: PaperAcceptanceReviewUpdate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        payload = upsert_acceptance_review(
            conn,
            symbol=req.symbol.strip().upper(),
            stage=req.stage,
            reviewer=req.reviewer,
            review_status=req.review_status,
            fixed_in_version=req.fixed_in_version,
            retest_required=req.retest_required,
            can_promote_to_live=req.can_promote_to_live,
            note=req.note,
            run_key=req.run_key,
        )
        return {"ok": True, "review": payload}
    finally:
        conn.close()


@router.put("/api/paper-acceptance/check")
def api_update_paper_acceptance_check(req: PaperAcceptanceCheckUpdate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.gate_id.strip() or not req.check_key.strip():
        raise HTTPException(400, "gate_id and check_key are required")
    conn = get_db()
    try:
        payload = upsert_acceptance_check(
            conn,
            symbol=req.symbol.strip().upper(),
            gate_id=req.gate_id.strip(),
            check_key=req.check_key.strip(),
            value=req.value,
            note=req.note or "",
            source=req.source or "manual",
            stage=req.stage,
        )
        return {"ok": True, "check": payload}
    finally:
        conn.close()


@router.delete("/api/paper-acceptance/check")
def api_delete_paper_acceptance_check(symbol: str, gate_id: str, check_key: str, stage: str = "paper"):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        delete_acceptance_check(
            conn,
            symbol=symbol.strip().upper(),
            gate_id=gate_id.strip(),
            check_key=check_key.strip(),
            stage=stage,
        )
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/paper-acceptance/smc")
def api_generate_smc_paper_acceptance(req: PaperAcceptanceGenerateRequest):
    conn = get_db()
    try:
        symbol = req.symbol.upper() if req.symbol else None
        if req.persist:
            payload = build_and_persist_smc_acceptance_report(conn, symbol=symbol, strategy=req.strategy)
        else:
            context = build_smc_acceptance_context(conn, symbol=symbol, strategy=req.strategy)
            report = build_acceptance_report(context)
            payload = {
                "run_key": None,
                "report": report,
                "markdown": render_acceptance_markdown(report),
            }
        return sanitize_float_values(payload)
    finally:
        conn.close()


@router.get("/api/paper-acceptance/events")
def api_get_paper_acceptance_events(symbol: Optional[str] = None, limit: int = 100):
    conn = get_db()
    events = load_acceptance_events(conn, symbol=symbol, limit=limit)
    conn.close()
    return sanitize_float_values({"events": events, "count": len(events)})


@router.post("/api/paper-acceptance/events")
def api_record_paper_acceptance_event(event: PaperAcceptanceEventCreate):
    if not event.event_type.strip():
        raise HTTPException(400, "event_type is required")
    conn = get_db()
    event_key = record_acceptance_event(
        conn,
        event_type=event.event_type.strip(),
        symbol=event.symbol,
        severity=event.severity,
        status=event.status,
        detail=event.detail or {},
        run_key=event.run_key,
    )
    conn.close()
    return {"ok": True, "event_key": event_key}


@router.get("/api/paper-acceptance/runtime-metrics")
def api_get_paper_acceptance_runtime_metrics(symbol: str, stage: str = "paper", limit: int = 200):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_runtime_metrics(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/runtime-metrics")
def api_record_paper_acceptance_runtime_metric(req: PaperAcceptanceRuntimeMetricCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.metric_name.strip():
        raise HTTPException(400, "metric_name is required")
    conn = get_db()
    try:
        row = record_runtime_metric(
            conn,
            symbol=req.symbol.strip().upper(),
            metric_name=req.metric_name.strip(),
            value=req.value,
            severity=req.severity,
            detail=req.detail or {},
            stage=req.stage,
            recorded_at=req.recorded_at,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/reconciliation")
def api_get_paper_acceptance_reconciliation(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_reconciliation_runs(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/reconciliation")
def api_record_paper_acceptance_reconciliation(req: PaperAcceptanceReconciliationCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_reconciliation_run(
            conn,
            symbol=req.symbol.strip().upper(),
            status=req.status,
            severity=req.severity,
            order_diff_count=req.order_diff_count,
            position_diff_count=req.position_diff_count,
            balance_diff_count=req.balance_diff_count,
            trade_diff_count=req.trade_diff_count,
            auto_suspend_recommended=req.auto_suspend_recommended,
            restoration_result=req.restoration_result,
            detail=req.detail or {},
            stage=req.stage,
            created_at=req.created_at,
        )
        return {"ok": True, "row": row}
    finally:
        conn.close()


@router.get("/api/paper-acceptance/order-audit")
def api_get_paper_acceptance_order_audit(symbol: str, stage: str = "paper", limit: int = 200):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_order_audit_rows(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/order-audit")
def api_record_paper_acceptance_order_audit(req: PaperAcceptanceOrderAuditCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_order_audit(
            conn,
            symbol=req.symbol.strip().upper(),
            side=req.side,
            order_type=req.order_type,
            state=req.state,
            requested_qty=req.requested_qty,
            filled_qty=req.filled_qty,
            unfilled_qty=req.unfilled_qty,
            signal_price=req.signal_price,
            limit_price=req.limit_price,
            avg_price=req.avg_price,
            notional=req.notional,
            fee=req.fee,
            slippage_bps=req.slippage_bps,
            market_impact_bps=req.market_impact_bps,
            execution_latency_ms=req.execution_latency_ms,
            client_order_id=req.client_order_id,
            exchange_order_id=req.exchange_order_id,
            strategy_version=req.strategy_version,
            parameter_version=req.parameter_version,
            signal_source=req.signal_source,
            submitted_at=req.submitted_at,
            ack_at=req.ack_at,
            fill_at=req.fill_at,
            cancel_at=req.cancel_at,
            reject_reason=req.reject_reason,
            detail=req.detail or {},
            stage=req.stage,
            created_at=req.created_at,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/alert-deliveries")
def api_get_paper_acceptance_alert_deliveries(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_alert_deliveries(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/virtual-account-snapshots")
def api_get_paper_acceptance_virtual_account_snapshots(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_virtual_account_snapshots(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/virtual-account-snapshots")
def api_record_paper_acceptance_virtual_account_snapshot(req: PaperAcceptanceVirtualAccountSnapshotCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_virtual_account_snapshot(
            conn,
            symbol=req.symbol.strip().upper(),
            account_currency=req.account_currency,
            equity=req.equity,
            available_balance=req.available_balance,
            frozen_balance=req.frozen_balance,
            margin_used=req.margin_used,
            unrealized_pnl=req.unrealized_pnl,
            realized_pnl=req.realized_pnl,
            open_position_count=req.open_position_count,
            open_order_count=req.open_order_count,
            detail=req.detail or {},
            stage=req.stage,
            created_at=req.created_at,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/stability-sessions")
def api_get_paper_acceptance_stability_sessions(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_stability_sessions(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/stability-sessions")
def api_record_paper_acceptance_stability_session(req: PaperAcceptanceStabilitySessionCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        row = record_stability_session(
            conn,
            symbol=req.symbol.strip().upper(),
            session_name=req.session_name,
            started_at=req.started_at,
            ended_at=req.ended_at,
            runtime_hours=req.runtime_hours,
            restart_count=req.restart_count,
            reconnect_count=req.reconnect_count,
            max_memory_pct=req.max_memory_pct,
            max_cpu_pct=req.max_cpu_pct,
            max_api_latency_ms=req.max_api_latency_ms,
            result=req.result,
            detail=req.detail or {},
            stage=req.stage,
            created_at=req.created_at,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.post("/api/paper-acceptance/alert-deliveries")
def api_record_paper_acceptance_alert_deliveries(req: PaperAcceptanceAlertDeliveryCreate):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.event_type.strip():
        raise HTTPException(400, "event_type is required")
    conn = get_db()
    try:
        row = record_alert_delivery(
            conn,
            symbol=req.symbol.strip().upper(),
            event_type=req.event_type.strip(),
            severity=req.severity,
            channel=req.channel,
            delivered=req.delivered,
            acknowledged=req.acknowledged,
            latency_ms=req.latency_ms,
            payload_complete=req.payload_complete,
            detail=req.detail or {},
            stage=req.stage,
            created_at=req.created_at,
        )
        return sanitize_float_values({"ok": True, "row": row})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/scenarios")
def api_get_paper_acceptance_scenarios(symbol: str, stage: str = "paper", limit: int = 100):
    if not symbol.strip():
        raise HTTPException(400, "symbol is required")
    conn = get_db()
    try:
        rows = load_scenario_runs(conn, symbol=symbol.strip().upper(), stage=stage, limit=limit)
        return sanitize_float_values({"rows": rows, "count": len(rows)})
    finally:
        conn.close()


@router.get("/api/paper-acceptance/scenario-library")
def api_get_paper_acceptance_scenario_library(symbol: Optional[str] = None, stage: str = "paper"):
    conn = get_db()
    try:
        workspace = build_acceptance_workspace(
            conn,
            symbol=(symbol.strip().upper() if symbol else "ALL"),
            stage=stage,
            limit_reports=1,
        )
        return sanitize_float_values({
            "rows": workspace.get("scenario_catalog") or [],
            "count": len(workspace.get("scenario_catalog") or []),
        })
    finally:
        conn.close()


@router.post("/api/paper-acceptance/scenarios/run")
def api_run_paper_acceptance_scenario(req: PaperAcceptanceScenarioRunRequest):
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.scenario_id.strip():
        raise HTTPException(400, "scenario_id is required")
    conn = get_db()
    try:
        row = run_acceptance_scenario(
            conn,
            symbol=req.symbol.strip().upper(),
            scenario_id=req.scenario_id.strip(),
            stage=req.stage,
        )
        return {"ok": True, "row": row}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()
