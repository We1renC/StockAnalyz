"""API-level tests for paper acceptance endpoints."""

from unittest.mock import patch

import app
from app import (
    PaperAcceptanceEventCreate,
    PaperAcceptanceGenerateRequest,
    SMCJournalCreate,
)


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "paper_acceptance_api.db")
    app.init_db()
    return original


def test_api_generate_smc_paper_acceptance_persists_report(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "_get_vault", return_value=None):
            app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="ABAT",
                    environment="paper",
                    status="closed",
                    direction="long",
                    entry_price=10,
                    exit_price=11,
                    stop_price=9.5,
                    qty=5,
                    model="sweep_reversal",
                )
            )

        payload = app.api_generate_smc_paper_acceptance(
            PaperAcceptanceGenerateRequest(symbol="ABAT", persist=True)
        )
        assert payload["run_key"]
        assert payload["report"]["summary"]["conclusion"] == "failed_repeat_paper"

        reports = app.api_get_paper_acceptance_reports(symbol="ABAT")
        assert reports["count"] == 1
        assert reports["reports"][0]["run_key"] == payload["run_key"]
    finally:
        app.DB = original


def test_api_generate_smc_paper_acceptance_without_persist(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_generate_smc_paper_acceptance(
            PaperAcceptanceGenerateRequest(symbol="ABAT", persist=False)
        )
        assert payload["run_key"] is None
        assert "markdown" in payload

        reports = app.api_get_paper_acceptance_reports(symbol="ABAT")
        assert reports["count"] == 0
    finally:
        app.DB = original


def test_api_record_paper_acceptance_event(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_record_paper_acceptance_event(
            PaperAcceptanceEventCreate(
                symbol="ABAT",
                event_type="reconciliation",
                severity="warning",
                detail={"difference": "position mismatch"},
            )
        )
        assert payload["ok"] is True

        events = app.api_get_paper_acceptance_events(symbol="ABAT")
        assert events["count"] == 1
        assert events["events"][0]["event_key"] == payload["event_key"]
        assert events["events"][0]["detail"]["difference"] == "position mismatch"
    finally:
        app.DB = original
