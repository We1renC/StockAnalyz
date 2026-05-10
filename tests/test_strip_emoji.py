"""Tests for _strip_emoji helper that scrubs emoji from alert text."""
import app  # type: ignore


def test_strips_common_emoji():
    s = "📍 元大台灣50反1 已到進場區 11.50"
    assert app._strip_emoji(s) == "元大台灣50反1 已到進場區 11.50"


def test_strips_multiple_emoji():
    s = "🎯 達標 ✅ 完成 🎉"
    assert app._strip_emoji(s) == "達標 完成"


def test_strips_geometric():
    assert app._strip_emoji("▲ MA60 ▼") == "MA60"


def test_strips_arrows():
    assert app._strip_emoji("分析師 → 審查員 ←") == "分析師 審查員"


def test_strips_dingbats():
    assert app._strip_emoji("✓ 通過 ✗ 失敗") == "通過 失敗"


def test_no_change_for_pure_chinese():
    s = "元大台灣50反1 已到進場區 11.50（現價 11.19）"
    assert app._strip_emoji(s) == s


def test_no_change_for_pure_ascii():
    s = "Hello World 2883.TW @ 22.90"
    assert app._strip_emoji(s) == s


def test_handles_none():
    assert app._strip_emoji(None) is None


def test_handles_empty():
    assert app._strip_emoji("") == ""


def test_collapses_whitespace_after_emoji_removal():
    """移除 emoji 後產生的多餘空白應壓回單一空格"""
    s = "  📍   元大     ❄️  超賣  "
    assert app._strip_emoji(s) == "元大 超賣"


def test_enrich_alert_strips_message_and_diagnosis():
    row = {
        "type": "STOP_LOSS",
        "message": "🛑 2883 觸及停損",
        "diagnosis": "🔴 已虧損 -10.5%",
    }
    enriched = app._enrich_alert(row)
    assert enriched["message"] == "2883 觸及停損"
    assert enriched["diagnosis"] == "已虧損 -10.5%"
    # type metadata 仍正確
    assert enriched["type_label"] == "停損"
    assert enriched["type_color"] == "bg-red-700"


def test_enrich_alert_keeps_chinese_intact():
    row = {
        "type": "ENTRY_TRIGGER",
        "message": "凱基金控 已到進場區",
        "diagnosis": None,
    }
    enriched = app._enrich_alert(row)
    assert enriched["message"] == "凱基金控 已到進場區"
    assert enriched["diagnosis"] is None
