"""Tests for Obsidian-backed domain research reloads."""
import json

import app  # type: ignore


def test_domain_research_loads_modified_obsidian_notes_with_sqlite_metadata_fallback(tmp_path):
    result = {
        "domain": "AI基礎設施",
        "ts": "2026-05-31 21:30",
        "summary": "原始總覽",
        "frontier": [{
            "symbol": "VRT",
            "name": "Vertiv",
            "thesis": "原始論點",
            "fundamentals": "原始基本面",
            "news": "原始新聞",
            "technology": "原始技術",
            "orders": "原始訂單",
            "week_term": "原始週線",
            "short_term": "原始短線",
            "mid_term": "原始中線",
            "long_term": "原始長線",
            "best_fit": ["short", "mid"],
            "indicators": {"price": 100.0, "rsi": 50.0},
        }],
        "leading": [],
        "analyst_report": "分析師報告",
        "reviewer_report": "審查報告",
    }
    index_path = app._save_obsidian_notes("AI基礎設施", result, str(tmp_path))
    assert index_path

    note_path = tmp_path / "Research" / "AI基礎設施" / "前瞻技術" / "VRT.md"
    note_text = note_path.read_text(encoding="utf-8")
    note_text = note_text.replace("原始論點", "Obsidian 手動修改論點")
    note_text = note_text.replace("best_fit: [short, mid]", "")
    note_path.write_text(note_text, encoding="utf-8")

    row = {
        "id": 1,
        "domain": "AI基礎設施",
        "ts": "2026-05-31 21:30",
        "frontier_stocks": json.dumps(result["frontier"], ensure_ascii=False),
        "leading_stocks": "[]",
        "analyst_report": "SQLite 分析師報告",
        "reviewer_report": "SQLite 審查報告",
        "obsidian_path": index_path,
    }
    loaded = app._load_domain_research_from_obsidian(row)

    assert loaded["data_source"] == "obsidian"
    assert loaded["obsidian_loaded"] is True
    assert loaded["frontier_stocks"][0]["thesis"] == "Obsidian 手動修改論點"
    assert loaded["frontier_stocks"][0]["best_fit"] == ["short", "mid"]
    assert loaded["frontier_stocks"][0]["indicators"]["price"] == 100.0


def test_obsidian_file_status_reports_missing_path(tmp_path):
    status = app._obsidian_file_status(str(tmp_path / "missing.md"))

    assert status["obsidian_status"] == "missing"
    assert status["obsidian_error"]
