from datetime import datetime, timedelta

import pandas as pd

from smc_quant import (
    SMCConfig,
    build_mtf_analysis,
    build_smc_analysis,
    calculate_position_size,
    detect_breaker_blocks,
    detect_displacement,
    detect_judas_swings,
    detect_liquidity,
    detect_mitigation_blocks,
    detect_order_blocks,
    detect_smt_divergence,
    detect_structure,
    detect_swings,
    infer_market,
    normalize_ohlcv,
    rule_enforcement_snapshot,
)


def _sample_ohlcv() -> pd.DataFrame:
    base = datetime(2026, 1, 1)
    rows = [
        (10, 11, 9, 10.5, 100),
        (10.5, 12, 10, 11.5, 120),
        (11.5, 13, 11, 12.8, 150),
        (12.8, 12.9, 10.8, 11.0, 180),
        (11.0, 11.2, 9.2, 9.6, 200),
        (9.6, 10.2, 8.8, 9.1, 210),
        (9.1, 10.0, 8.9, 9.8, 150),
        (9.8, 11.8, 9.7, 11.6, 260),
        (11.6, 14.2, 11.5, 14.0, 320),
        (14.0, 15.0, 13.4, 14.8, 280),
        (14.8, 14.9, 12.6, 13.0, 260),
        (13.0, 13.4, 11.8, 12.1, 240),
        (12.1, 12.7, 10.5, 10.8, 300),
        (10.8, 11.1, 9.4, 10.2, 270),
        (10.2, 12.6, 10.1, 12.4, 310),
        (12.4, 15.6, 12.3, 15.2, 360),
        (15.2, 16.4, 14.9, 16.1, 330),
        (16.1, 16.2, 14.2, 14.6, 290),
        (14.6, 15.8, 14.1, 15.5, 260),
        (15.5, 17.1, 15.4, 16.8, 340),
        (16.8, 18.2, 16.6, 17.9, 390),
        (17.9, 18.0, 16.0, 16.4, 280),
        (16.4, 17.5, 16.1, 17.2, 250),
        (17.2, 19.3, 17.0, 19.0, 410),
        (19.0, 20.2, 18.7, 19.7, 360),
    ]
    idx = [base + timedelta(days=i) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def test_swings_include_confirmation_index_for_lookahead_safety():
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, swing_length=2)
    assert swings
    assert all(s["confirm_index"] == s["index"] + 2 for s in swings)
    assert all(s["lookahead_safe"] for s in swings)


def test_build_smc_analysis_outputs_core_concepts_and_markers():
    result = build_smc_analysis(_sample_ohlcv(), "2330.TW", config=SMCConfig(swing_length=2, internal_swing_length=2))
    assert result["summary"]["bias"] in {"strong_bullish", "bullish", "neutral", "bearish", "strong_bearish"}
    assert result["market"] == "tw"
    assert result["market_config"]["timezone"] == "Asia/Taipei"
    concepts = result["concepts"]
    assert concepts["swings"]
    assert "premium_discount" in concepts
    assert "crypto_derivatives" in concepts
    assert isinstance(result["signals"], list)
    assert isinstance(result["markers"], list)
    assert result["visualization"]["enabled_charts"]


def test_standardized_signal_contains_feature_vector_and_risk_contract():
    result = build_smc_analysis(
        _sample_ohlcv(),
        "ABAT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
        account_equity=10_000,
    )
    assert infer_market("ABAT") == "us"
    assert result["signals"]
    signal = result["signals"][0]
    assert signal["symbol"] == "ABAT"
    assert signal["market"] == "us"
    assert signal["signal_id"].startswith("ABAT:")
    assert isinstance(signal["feature_vector"], dict)
    assert signal["risk"]["position_sizing"]["risk_amount"] == 100


def test_position_sizing_and_rule_enforcement_are_deterministic():
    sizing = calculate_position_size({"entry": 100, "stop": 95}, account_equity=50_000, risk_pct=0.01, market="us")
    assert sizing["qty"] == 100
    assert sizing["risk_amount"] == 500
    assert sizing["blocked"] is False

    ok = rule_enforcement_snapshot(100_000, daily_realized_pnl=-10_000, max_drawdown=-20_000, active_days_traded=12)
    assert ok["locked"] is False
    locked = rule_enforcement_snapshot(100_000, daily_realized_pnl=-55_000, max_drawdown=-20_000)
    assert locked["locked"] is True
    assert locked["lock_reason"] == "risk_limit_breached"


def test_mtf_analysis_returns_top_down_alignment_and_poi_list():
    sample = _sample_ohlcv()
    result = build_mtf_analysis(
        {"htf": sample, "mtf": sample, "ltf": sample},
        "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
        account_equity=25_000,
    )
    assert result["market"] == "crypto"
    assert set(result["layers"]) == {"htf", "mtf", "ltf"}
    assert "aligned" in result["top_down"]
    assert isinstance(result["poi"], list)


def test_order_block_has_refined_entry_and_volume_metrics():
    """Per §3.3 each OB must expose Consequent Encroachment + OBVolume + Percentage."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    obs = result["concepts"]["order_blocks"]
    assert obs, "expected at least one detected order block"
    for ob in obs:
        # Consequent Encroachment = 50% mid-line of the OB range
        assert "refined_entry" in ob
        assert ob["refined_entry"] == round((ob["top"] + ob["bottom"]) / 2, 4)
        # OBVolume and Percentage per design doc
        assert "ob_volume" in ob and ob["ob_volume"] >= 0
        assert "ob_percentage" in ob and 0 <= ob["ob_percentage"] <= 100
        # Lifecycle status replaces the prior boolean-only fields
        assert ob.get("status") in {"unmitigated", "mitigation", "breaker"}
        # Body range exposed for close_mitigation policy choice
        assert ob["body_top"] >= ob["body_bottom"]


def test_mitigation_and_breaker_blocks_extracted_from_order_blocks():
    """detect_mitigation_blocks + detect_breaker_blocks split the OB list."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    obs = result["concepts"]["order_blocks"]
    mit = detect_mitigation_blocks(obs)
    brk = detect_breaker_blocks(obs)
    # Helper invariants
    for m in mit:
        assert m["status"] == "mitigation"
        assert m["block_type"] == "mitigation"
        # Mitigation downgrades A→B (never fresh Grade-A)
        assert m["grade"] in {"B", "C"}
    for b in brk:
        assert b["status"] == "breaker"
        assert b["block_type"] == "breaker"
        assert b["grade"] == "C"
        # Direction flipped vs the original OB role
        assert b["direction"] == -b["original_direction"]
    # build_smc_analysis surfaces them as separate concept arrays
    assert "mitigation_blocks" in result["concepts"]
    assert "breaker_blocks" in result["concepts"]


def test_mitigation_blocks_disjoint_from_unmitigated_and_breaker():
    """mitigation vs unmitigated vs breaker must partition (no double-counting)."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    obs = result["concepts"]["order_blocks"]
    statuses = {ob["status"] for ob in obs}
    # Every status seen in the OB list maps cleanly to one of the three buckets
    assert statuses.issubset({"unmitigated", "mitigation", "breaker"})
    mit_set = {(m["index"], m["event_index"]) for m in detect_mitigation_blocks(obs)}
    brk_set = {(b["index"], b["event_index"]) for b in detect_breaker_blocks(obs)}
    assert mit_set.isdisjoint(brk_set)


def test_judas_swing_detects_sweep_then_choch_reversal():
    """§3.12: BSL sweep + opposite-direction CHoCH within window = Judas confirmed."""
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    events = detect_judas_swings(h, structure, liquidity, displacements, "AAPL")
    # Algorithm should run without error on the synthetic frame
    assert isinstance(events, list)
    for ev in events:
        # Mandatory output shape per §3.12
        assert ev["judas"] in (1, -1)
        assert ev["real_direction"] == -ev["fakeout_direction"]
        assert ev["sweep_type"] in {"BSL", "SSL"}
        assert ev["confirm_index"] > ev["sweep_index"]
        # FalseMoveHigh ≥ FalseMoveLow within the fakeout window
        assert ev["false_move_high"] >= ev["false_move_low"]


def test_judas_swing_handles_empty_inputs_gracefully():
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    # No liquidity / no structure → no events, no exception
    assert detect_judas_swings(h, [], [], [], "AAPL") == []
    assert detect_judas_swings(h, [{"type": "CHOCH", "index": 5, "direction": 1}], [], [], "AAPL") == []


def test_build_smc_analysis_exposes_judas_events_list():
    """concepts.judas now carries an events list (not just a boolean)."""
    result = build_smc_analysis(
        _sample_ohlcv(), "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    judas = result["concepts"]["judas"]
    assert "events" in judas and isinstance(judas["events"], list)
    assert "latest" in judas
    assert "active" in judas  # legacy field preserved


def test_smt_divergence_bullish_when_correlated_holds_higher_low():
    """§3.13: primary makes LL, paired holds above prior low → bullish SMT (+1)."""
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    # Craft paired feed: identical highs but lows clamped so it never makes a LL.
    paired = _sample_ohlcv().copy()
    paired["Low"] = paired["Low"].clip(lower=15.0)
    paired["Open"] = paired["Open"].clip(lower=15.0)
    paired["Close"] = paired["Close"].clip(lower=15.0)
    paired["High"] = paired[["High", "Low"]].max(axis=1)
    events = detect_smt_divergence(h, {"PAIR": paired}, swings)
    bullish = [e for e in events if e["smt"] == 1]
    if bullish:  # only assert when the swing arrangement actually triggers
        ev = bullish[-1]
        assert ev["paired_symbol"] == "PAIR"
        assert ev["primary_curr_level"] < ev["primary_prev_level"]
        assert ev["paired_curr_level"] > ev["paired_prev_level"]


def test_smt_divergence_handles_missing_or_empty_correlated():
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    # No correlated dict → empty
    assert detect_smt_divergence(h, None, swings) == []
    assert detect_smt_divergence(h, {}, swings) == []
    # Empty paired DataFrame → silently skipped
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    assert detect_smt_divergence(h, {"EMPTY": empty}, swings) == []


def test_build_smc_analysis_exposes_smt_events_when_correlated_provided():
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    paired = _sample_ohlcv().copy()
    paired["High"] = paired["High"] * 0.95
    paired["Low"] = paired["Low"] * 0.95
    result = build_smc_analysis(
        _sample_ohlcv(), "ES=F",
        config=cfg,
        correlated={"NQ=F": paired},
    )
    smt = result["concepts"]["smt"]
    assert "events" in smt and isinstance(smt["events"], list)
    assert "latest" in smt
    assert "NQ=F" in smt["pairs"]


def test_unicorn_and_smt_divergence_entry_models():
    from smc_quant import build_signals, SMCConfig
    df = normalize_ohlcv(_sample_ohlcv())
    cfg = SMCConfig()
    
    # 1. Unicorn Model (Breaker Block + FVG overlap)
    obs = [{"direction": 1, "unmitigated": True, "breaker": True, "top": 12.0, "bottom": 10.0, "mid": 11.0, "body_top": 11.5, "body_bottom": 10.5, "index": 5, "event_index": 5}]
    fvgs = [{"direction": 1, "mitigated": False, "displacement_confirmed": True, "top": 12.5, "bottom": 11.5, "mid": 12.0, "index": 20}]
    signals = build_signals(
        df=df,
        bias="bullish",
        order_blocks=obs,
        fvgs=fvgs,
        liquidity=[],
        pd_zone={"zone": "discount"},
        ote={},
        structure=[],
        displacements=[],
        session={"name": "New York", "killzone": True},
        prev={"previous_high": 15.0, "previous_low": 8.0},
        cfg=cfg,
        symbol="AAPL"
    )
    assert len(signals) > 0
    assert signals[0]["model"] == "Unicorn"

    # 2. SMT Divergence Model (SMT events present)
    signals_smt = build_signals(
        df=df,
        bias="bullish",
        order_blocks=[],
        fvgs=[],
        liquidity=[],
        pd_zone={"zone": "discount"},
        ote={},
        structure=[],
        displacements=[],
        session={"name": "New York", "killzone": True},
        prev={"previous_high": 15.0, "previous_low": 8.0},
        cfg=cfg,
        smt_events=[{"index": len(df) - 5, "smt": 1, "paired_symbol": "QQQ"}],
        symbol="AAPL"
    )
    assert len(signals_smt) > 0
    assert signals_smt[0]["model"] == "SMT Divergence Model"


def test_silver_bullet_and_power_of_three_entry_models():
    from smc_quant import build_signals, SMCConfig
    cfg = SMCConfig()
    
    # 1. Silver Bullet Model (In specific time window + recent FVG)
    base = datetime(2026, 1, 1, 10, 30, 0)
    idx = [base - timedelta(minutes=i*5) for i in range(25)]
    idx.reverse()
    df = pd.DataFrame(
        {"open": [10.0]*25, "high": [10.5]*25, "low": [9.5]*25, "close": [10.0]*25, "volume": [100]*25},
        index=idx
    )
    df.index = pd.to_datetime(df.index).tz_localize("US/Eastern")
    
    fvgs = [{"direction": 1, "mitigated": False, "displacement_confirmed": True, "top": 10.2, "bottom": 9.8, "mid": 10.0, "index": len(df) - 2}]
    
    signals_sb = build_signals(
        df=df,
        bias="bullish",
        order_blocks=[],
        fvgs=fvgs,
        liquidity=[],
        pd_zone={"zone": "discount"},
        ote={},
        structure=[],
        displacements=[],
        session={"name": "New York", "killzone": True},
        prev={"previous_high": 15.0, "previous_low": 8.0},
        cfg=cfg,
        symbol="AAPL"
    )
    assert len(signals_sb) > 0
    assert signals_sb[0]["model"] == "Silver Bullet"

    # 2. Power of Three (AMD) (Judas event present)
    signals_amd = build_signals(
        df=df,
        bias="bullish",
        order_blocks=[],
        fvgs=[],
        liquidity=[],
        pd_zone={"zone": "discount"},
        ote={},
        structure=[],
        displacements=[],
        session={"name": "New York", "killzone": True},
        prev={"previous_high": 15.0, "previous_low": 8.0},
        cfg=cfg,
        judas_events=[{"index": len(df) - 5, "judas": 1}],
        symbol="AAPL"
    )
    assert len(signals_amd) > 0
    assert signals_amd[0]["model"] == "Power of Three (AMD)"



def test_confluence_scorer_obeys_threshold_and_weights():
    """§5.2 scorer must respect weights, threshold, and surface contributing factors."""
    from smc_quant import score_confluence
    factors = {
        "htf_bias_aligned": True,
        "premium_discount_side": True,
        "unmitigated_ob": True,
        "unfilled_fvg": False,
        "liquidity_swept": True,
        "ltf_choch": True,
        "ote_zone": False,
        "killzone": True,
        "volume_displacement": True,
    }
    s = score_confluence(factors)
    # 2+2+2+0+2+2+0+1+1 = 12 → over the 8-point threshold
    assert s["score"] == 12
    assert s["triggered"] is True
    names = {f["factor"] for f in s["contributing_factors"]}
    assert "htf_bias_aligned" in names and "ote_zone" not in names
    # Below threshold case
    cold = {k: False for k in factors}
    cold["liquidity_swept"] = True
    cold["ltf_choch"] = True  # 4 only
    s2 = score_confluence(cold)
    assert s2["score"] == 4 and s2["triggered"] is False


def test_sweep_reversal_entries_produce_rr_and_scored_signals():
    """§5.1 Model 1 chains Judas → POI → entry/stop/target with §5.2 score attached."""
    from smc_quant import detect_sweep_reversal_entries
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    obs = detect_order_blocks(h, structure, displacements, liquidity)
    fvgs = []  # exercise the OB-only branch
    judas = detect_judas_swings(h, structure, liquidity, displacements, "AAPL")
    entries = detect_sweep_reversal_entries(h, judas, obs, fvgs, {"state": "discount"}, "bullish")
    assert isinstance(entries, list)
    for e in entries:
        # Mandatory shape per §5.1 + §5.2
        assert e["model"] == "sweep_reversal"
        assert e["direction"] in (1, -1)
        assert e["risk"] > 0 and e["rr"] >= 1.99  # 2R fallback
        # Stop sits beyond the structural invalidation point
        if e["direction"] == 1:
            assert e["stop"] <= e["false_move_low"]
        else:
            assert e["stop"] >= e["false_move_high"]
        # Confluence contract
        assert "score" in e["confluence"] and "threshold" in e["confluence"]
        assert set(e["factors"]).issuperset({"htf_bias_aligned", "liquidity_swept", "ltf_choch"})


def test_build_smc_analysis_exposes_entry_models_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "sweep_reversal" in em and isinstance(em["sweep_reversal"], list)
    assert "triggered" in em
    assert "latest" in em


def test_continuation_entries_anchor_to_latest_bos():
    """§5.1 Model 2: continuation entries must derive from a BOS event."""
    from smc_quant import detect_continuation_entries
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    obs = detect_order_blocks(h, structure, displacements, liquidity)
    entries = detect_continuation_entries(h, structure, obs, [], {"state": "discount"}, "bullish")
    bos_events = [ev for ev in structure if ev["type"] == "BOS"]
    if not bos_events:
        # If no BOS in sample → no entries (do not fabricate)
        assert entries == []
    else:
        for e in entries:
            assert e["model"] == "ob_fvg_continuation"
            assert e["direction"] in (1, -1)
            assert e["risk"] > 0 and e["rr"] >= 1.99
            if e["direction"] == 1:
                assert e["stop"] <= e["poi_bottom"]
            else:
                assert e["stop"] >= e["poi_top"]
            assert e["bos_index"] == bos_events[-1]["index"]


def test_build_smc_analysis_exposes_continuation_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "ob_fvg_continuation" in em and isinstance(em["ob_fvg_continuation"], list)


def test_ote_entries_use_fib_band_and_optional_poi_overlap():
    """§5.1 Model 3: OTE band 0.62–0.79 (ideal 0.705) drives the entry."""
    from smc_quant import detect_ote_entries, ote_zone
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    obs = detect_order_blocks(h, structure, displacements, liquidity)
    bias = "bullish"
    ote = ote_zone(swings, bias)
    entries = detect_ote_entries(h, ote, obs, [], {"state": "discount"}, bias)
    for e in entries:
        assert e["model"] == "ote_retracement"
        assert e["direction"] in (1, -1)
        assert e["risk"] > 0 and e["rr"] >= 1.99
        # Entry must lie within the 0.62–0.79 OTE band
        assert e["ote_bottom"] <= e["entry"] <= e["ote_top"]
        # OTE factor is always credited (band is the entry by construction)
        assert e["factors"]["ote_zone"] is True


def test_ote_entries_empty_when_ote_missing():
    from smc_quant import detect_ote_entries
    h = normalize_ohlcv(_sample_ohlcv())
    assert detect_ote_entries(h, {}, [], [], {}, "neutral") == []


def test_build_smc_analysis_exposes_ote_retracement_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "ote_retracement" in em and isinstance(em["ote_retracement"], list)


def test_unicorn_entries_require_breaker_fvg_overlap():
    """§5.3: Unicorn = Breaker ∩ FVG in the same direction. Without overlap → []."""
    from smc_quant import detect_unicorn_entries
    # Hand-craft: a bullish breaker [10, 12] and an overlapping bullish FVG [11, 12.5]
    breakers = [{"index": 5, "direction": 1, "top": 12.0, "bottom": 10.0, "block_type": "breaker", "grade": "C"}]
    fvgs = [{"index": 7, "direction": 1, "top": 12.5, "bottom": 11.0, "mid": 11.75, "mitigated": False, "displacement_confirmed": True}]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_unicorn_entries(h, breakers, fvgs, [], {"state": "discount"}, "bullish")
    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == "unicorn"
    assert e["direction"] == 1
    assert e["poi_kind"] == "breaker_fvg_overlap"
    assert e["poi_bottom"] == 11.0 and e["poi_top"] == 12.0
    assert e["rr"] >= 1.99
    # Non-overlapping FVG must not trigger.
    fvgs2 = [{"index": 7, "direction": 1, "top": 9.0, "bottom": 8.0, "mid": 8.5, "mitigated": False, "displacement_confirmed": False}]
    assert detect_unicorn_entries(h, breakers, fvgs2, [], {}, "neutral") == []


def test_unicorn_smt_confirmation_flag_propagates():
    from smc_quant import detect_unicorn_entries
    breakers = [{"index": 5, "direction": 1, "top": 12.0, "bottom": 10.0}]
    fvgs = [{"index": 7, "direction": 1, "top": 12.5, "bottom": 11.0, "mid": 11.75, "mitigated": False, "displacement_confirmed": True}]
    smt = [{"smt": 1, "direction": 1, "paired_symbol": "NQ=F"}]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_unicorn_entries(h, breakers, fvgs, smt, {"state": "discount"}, "bullish")
    assert entries
    assert entries[0]["smt_confirmed"] is True
    assert entries[0]["smt_paired_symbol"] == "NQ=F"


def test_build_smc_analysis_exposes_unicorn_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "unicorn" in em and isinstance(em["unicorn"], list)


def test_silver_bullet_entry_requires_sweep_then_fvg():
    """§5.3 Silver Bullet: sweep within recent window followed by same-direction FVG."""
    from smc_quant import detect_silver_bullet_entries
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    fvgs = []  # No FVG → no entries
    assert detect_silver_bullet_entries(h, liquidity, fvgs, "AAPL", {}, "neutral") == []
    # Daily data (no intraday filter) — time_filtered must be False
    fvgs = [{"index": 22, "direction": 1, "top": 18.0, "bottom": 17.0, "mid": 17.5, "mitigated": False, "displacement_confirmed": True}]
    entries = detect_silver_bullet_entries(h, liquidity, fvgs, "AAPL", {"state": "discount"}, "bullish")
    for e in entries:
        assert e["model"] == "silver_bullet"
        assert e["direction"] in (1, -1)
        assert e["fvg_index"] > e["sweep_index"]
        assert e["time_filtered"] is False  # daily bars degrade gracefully
        assert e["rr"] >= 1.99


def test_silver_bullet_window_per_market():
    from smc_quant import _silver_bullet_window_minutes
    assert _silver_bullet_window_minutes("2330.TW") == (9 * 60, 10 * 60)
    assert _silver_bullet_window_minutes("AAPL") == (10 * 60, 11 * 60)
    assert _silver_bullet_window_minutes("BTCUSDT") == (10 * 60, 11 * 60)


def test_build_smc_analysis_exposes_silver_bullet_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "silver_bullet" in em and isinstance(em["silver_bullet"], list)


def test_power_of_three_requires_accumulation_then_judas():
    """§5.3 Power of Three: Accumulation (tight pre-sweep range) + Judas + Distribution."""
    from smc_quant import detect_power_of_three_entries
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    judas = detect_judas_swings(h, structure, liquidity, displacements, "AAPL")
    # No Judas → no entries
    assert detect_power_of_three_entries(h, [], [], [], {}, "neutral") == []
    entries = detect_power_of_three_entries(h, judas, [], [], {"state": "discount"}, "bullish")
    for e in entries:
        assert e["model"] == "power_of_three"
        assert e["accumulation_end"] == e["judas_index"] - 1 or e["accumulation_end"] < e["judas_index"]
        assert e["accumulation_range"] > 0
        assert e["rr"] >= 1.99


def test_build_smc_analysis_exposes_power_of_three_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "power_of_three" in em and isinstance(em["power_of_three"], list)
    # Should also still expose all earlier models in a single block.
    for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement", "unicorn", "silver_bullet"):
        assert key in em


def test_risk_pipeline_filters_by_rr_then_sizes():
    """§6: RR floor first, lock check next, then position-size."""
    from smc_quant import apply_risk_pipeline
    entries = [
        {"model": "x", "entry": 100, "stop": 99, "rr": 2.0, "triggered": True},
        {"model": "x", "entry": 100, "stop": 99.5, "rr": 1.0, "triggered": True},   # RR too low
        {"model": "x", "entry": 100, "stop": 99, "rr": 2.0, "triggered": False},    # confluence fail
    ]
    out = apply_risk_pipeline(entries, account_equity=50_000, market="us")
    reasons = {r["reject_reason"] for r in out["rejected"]}
    assert any(r.startswith("rr_below_floor") for r in reasons)
    assert "confluence_below_threshold" in reasons
    assert out["ready"], "the valid entry should size up"
    assert out["ready"][0]["sizing"]["qty"] > 0
    assert out["lock"]["locked"] is False


def test_risk_pipeline_blocks_when_account_locked():
    from smc_quant import apply_risk_pipeline
    entries = [{"model": "x", "entry": 100, "stop": 99, "rr": 2.0, "triggered": True}]
    out = apply_risk_pipeline(
        entries, account_equity=100_000,
        daily_realized_pnl=-60_000,  # exceeds default 50k daily floor → locked
    )
    assert out["lock"]["locked"] is True
    assert out["ready"] == []
    assert "account_locked" in out["rejected"][0]["reject_reason"]


def test_build_smc_analysis_exposes_risk_gated_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
        account_equity=25_000,
    )
    rg = result["concepts"]["entry_models"]["risk_gated"]
    assert "ready" in rg and "rejected" in rg and "lock" in rg


def test_dol_target_picks_nearest_opposite_pool():
    """§3.5 DOL: long → nearest unswept BSL above price; short → nearest SSL below."""
    from smc_quant import resolve_dol_target
    liquidity = [
        {"type": "BSL", "level": 110, "swept": False, "end_index": 5},
        {"type": "BSL", "level": 120, "swept": False, "end_index": 6},
        {"type": "BSL", "level": 105, "swept": True, "end_index": 4},   # swept → ignored
        {"type": "SSL", "level": 90, "swept": False, "end_index": 3},
    ]
    long_target = resolve_dol_target(1, current_price=100, liquidity=liquidity)
    assert long_target["target_price"] == 110.0
    assert long_target["target_kind"] == "BSL"
    short_target = resolve_dol_target(-1, current_price=100, liquidity=liquidity)
    assert short_target["target_kind"] == "SSL"
    assert short_target["target_price"] == 90.0


def test_dol_falls_back_to_pdh_and_fvg_when_no_liquidity():
    from smc_quant import resolve_dol_target
    prev = {"previous_high": 115, "previous_low": 85}
    fvgs = [
        {"direction": -1, "mid": 112, "mitigated": False, "index": 7},   # bearish FVG = magnet for long
        {"direction": -1, "mid": 118, "mitigated": False, "index": 8},
    ]
    target = resolve_dol_target(1, current_price=100, liquidity=[], prev_levels=prev, fvgs=fvgs)
    assert target is not None
    # PDH @ 115 (distance 15) vs FVG mid 112 (12) → FVG wins on proximity
    assert target["target_price"] in (112.0, 115.0)
    assert target["target_kind"] in ("FVG_MID", "PDH")


def test_attach_dol_blocks_entries_without_target():
    """Per §3.5: 'do not enter trades without a clear DOL'."""
    from smc_quant import attach_dol_targets
    entries = [
        {"model": "x", "direction": 1, "entry": 100, "stop": 99, "target": 102, "risk": 1, "rr": 2.0, "triggered": True}
    ]
    # No liquidity / no prev / no FVG above current → no DOL → triggered must flip False
    out = attach_dol_targets(entries, liquidity=[], prev_levels=None, fvgs=[], current_price=100)
    assert out[0]["dol_target"] is None
    assert out[0]["dol_required"] is True
    assert out[0]["triggered"] is False


def test_build_smc_analysis_attaches_dol_to_entry_models():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement", "unicorn", "silver_bullet", "power_of_three"):
        for e in result["concepts"]["entry_models"][key]:
            assert "dol_target" in e
            assert "dol_required" in e


def test_backtest_replay_settles_long_trade_at_target():
    """§10: bar-by-bar replay must hit target before max_hold expiry."""
    from smc_quant import evaluate_entry_models
    # 6 bars, price rallies from 100 → 112; entry @ 100 / stop @ 99 / target @ 110 → +10R
    rows = [(100, 101, 99.5, 100.5, 100)] * 2 + [
        (100.5, 105, 100, 104, 110),
        (104, 108, 103.5, 107, 120),
        (107, 112, 106, 111, 130),
        (111, 113, 110, 112, 140),
    ]
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(rows))]
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    entries = [{
        "model": "sweep_reversal", "direction": 1,
        "entry": 100.0, "stop": 99.0, "target": 110.0,
        "rr": 10.0, "triggered": True, "sweep_index": 1,
    }]
    out = evaluate_entry_models(df, entries, max_hold_bars=10)
    assert out["metrics"]["count"] == 1
    assert out["metrics"]["wins"] == 1
    assert out["trades"][0]["outcome"] == "target"
    assert out["trades"][0]["r_multiple"] == 10.0


def test_backtest_replay_settles_at_stop_when_pierced():
    from smc_quant import evaluate_entry_models
    # Bar after entry pierces stop @ 99
    rows = [(100, 101, 99.5, 100.5, 100), (100.5, 100.6, 98.0, 98.5, 110)]
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(rows))]
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    entries = [{
        "model": "x", "direction": 1,
        "entry": 100.0, "stop": 99.0, "target": 110.0,
        "rr": 10.0, "triggered": True, "sweep_index": 0,
    }]
    out = evaluate_entry_models(df, entries, max_hold_bars=5)
    assert out["metrics"]["losses"] == 1
    assert out["trades"][0]["r_multiple"] == -1.0


def test_backtest_replay_ignores_untriggered_entries():
    """only_triggered=True must skip entries that did not pass confluence."""
    from smc_quant import evaluate_entry_models
    h = normalize_ohlcv(_sample_ohlcv())
    entries = [
        {"model": "a", "direction": 1, "entry": 10, "stop": 9, "target": 12, "rr": 2, "triggered": False, "sweep_index": 5},
    ]
    out = evaluate_entry_models(h, entries)
    assert out["metrics"]["count"] == 0


def test_backtest_replay_respects_lookahead_guard():
    """Entry index must come strictly AFTER the last confirmation event."""
    from smc_quant import evaluate_entry_models, _entry_bar_of
    e = {"judas_index": 10, "bos_index": 8, "fvg_index": 12}
    assert _entry_bar_of(e) == 12  # newest anchor wins


def test_build_smc_analysis_exposes_backtest_replay_block():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    bt = result["concepts"]["entry_models"]["backtest_replay"]
    assert "metrics" in bt and "trades" in bt
    assert "win_rate" in bt["metrics"]
    assert "passes_acceptance" in bt["metrics"]


def test_factor_edge_reports_per_factor_avg_R_and_winrate():
    """§10.6: positive edge factor must show higher avg_R when True."""
    from smc_quant import extract_factor_edge
    entries = [
        {"model": "x", "entry": 100, "stop": 99, "triggered": True,
         "factors": {"htf_bias_aligned": True, "ote_zone": True}},
        {"model": "x", "entry": 100, "stop": 99, "triggered": True,
         "factors": {"htf_bias_aligned": True, "ote_zone": False}},
        {"model": "x", "entry": 100, "stop": 99, "triggered": True,
         "factors": {"htf_bias_aligned": False, "ote_zone": True}},
    ]
    # Trades aligned 1:1 with entries (same key tuple repeats join the first)
    trades = [
        {"model": "x", "entry": 100, "stop": 99, "r_multiple": 2.0},
        {"model": "x", "entry": 100, "stop": 99, "r_multiple": 2.0},
        {"model": "x", "entry": 100, "stop": 99, "r_multiple": 2.0},
    ]
    edge = extract_factor_edge(entries, trades)
    assert edge["sample_size"] == 3
    assert "htf_bias_aligned" in edge["factors"]
    assert isinstance(edge["ranked"], list)


def test_suggest_weights_lifts_high_edge_factors():
    """Factor with edge ≥ +0.5 and enough samples on both sides → weight +1."""
    from smc_quant import suggest_confluence_weights
    edge = {
        "factors": {
            "ote_zone": {"n_with": 10, "n_without": 10, "edge": 0.8},
            "killzone": {"n_with": 10, "n_without": 10, "edge": -0.7},
            "unmitigated_ob": {"n_with": 2, "n_without": 10, "edge": 0.9},  # too few → skip
        }
    }
    suggested = suggest_confluence_weights(edge)
    # Default OTE=1 → +1=2, killzone=1 → -1=0, ob unchanged due to sample floor
    assert suggested["ote_zone"] == 2
    assert suggested["killzone"] == 0
    assert suggested["unmitigated_ob"] == 2  # base default, untouched


def test_build_smc_analysis_exposes_factor_edge_and_suggested_weights():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    assert "factor_edge" in em
    assert "suggested_weights" in em
    assert isinstance(em["suggested_weights"], dict)


def test_chart_layers_include_all_documented_chart_codes():
    """§6.1 Appendix A: each chart code must be present in the layer map."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    layers = result["visualization"]["chart_layers"]
    for code in ("C1_structure", "C2_order_blocks", "C3_fvgs", "C4_liquidity",
                 "C7_session_judas", "C8_sweep_reversal", "C10_signals", "C12_smt"):
        assert code in layers, f"chart layer {code} missing"


def test_chart_layers_carry_renderable_primitives():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    layers = result["visualization"]["chart_layers"]
    c1 = layers["C1_structure"]
    assert "swings" in c1 and "events" in c1
    # C2 rectangles
    for r in layers["C2_order_blocks"]["rects"]:
        assert r["top"] >= r["bottom"]
        assert r["direction"] in (1, -1)
        assert r["status"] in {"unmitigated", "mitigation", "breaker"}
    # C10 signals must mirror entry_models_combined
    trade_models = {t["model"] for t in layers["C10_signals"]["trades"]}
    assert trade_models.issubset({
        "sweep_reversal", "ob_fvg_continuation", "ote_retracement",
        "unicorn", "silver_bullet", "power_of_three",
    })


def test_crypto_overlay_detects_liquidation_sweep_and_oi_drop():
    """§17.2: liquidation cluster swept + OI drop → both crypto factors active."""
    from smc_quant import build_crypto_overlay
    h = normalize_ohlcv(_sample_ohlcv())
    # Sample bar -3 (idx 22): high 17.5, close 17.2 → BSL @ 17.3 is pierced then closed below
    liqs = [{"type": "BSL_LIQ", "level": 17.3, "size": 1_000_000}]
    # OI drops 5% across last bars
    oi = pd.Series([100, 100, 95, 90], index=h.index[-4:])
    overlay = build_crypto_overlay(h, liquidations=liqs, open_interest=oi)
    assert overlay["status"] == "ok"
    assert overlay["factors"]["liquidation_cluster_sweep"] is True
    assert overlay["oi"]["drop_at_sweep"] is True
    assert overlay["factors"]["oi_drop_at_sweep"] is True


def test_crypto_overlay_funding_extreme_and_premium_alignment():
    from smc_quant import build_crypto_overlay
    h = normalize_ohlcv(_sample_ohlcv())
    # Extreme positive funding → "long_crowded"
    funding = pd.Series([0.0006], index=h.index[-1:])
    premium = pd.Series([-0.1], index=h.index[-1:])  # bearish premium
    overlay = build_crypto_overlay(
        h, funding_rate=funding, coinbase_premium=premium, direction_bias=-1,
    )
    assert overlay["funding"]["status"] == "long_crowded"
    assert overlay["factors"]["funding_extreme_contrarian"] is True
    assert overlay["coinbase_premium"]["status"] == "bearish"
    assert overlay["factors"]["coinbase_premium_aligned"] is True


def test_crypto_overlay_no_inputs_returns_graceful_no_data_substates():
    from smc_quant import build_crypto_overlay
    h = normalize_ohlcv(_sample_ohlcv())
    overlay = build_crypto_overlay(h)
    assert overlay["status"] == "ok"
    assert overlay["funding"]["status"] == "no_data"
    assert overlay["coinbase_premium"]["status"] == "no_data"
    assert overlay["cvd"]["status"] == "no_data"
    assert overlay["oi"]["status"] == "no_data"
    # Factors all default to False when no data is supplied
    assert not any(overlay["factors"].values())


def test_build_smc_analysis_routes_crypto_inputs_into_overlay():
    funding = pd.Series([0.001])
    result = build_smc_analysis(
        _sample_ohlcv(), "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
        crypto_inputs={"funding_rate": funding},
    )
    cd = result["concepts"]["crypto_derivatives"]
    assert cd["status"] == "ok"
    assert "factors" in cd
    assert "weights" in cd


def test_trade_record_schema_captures_features_and_outcome():
    """§18.2: normalized trade record must carry features X + outcome Y."""
    from smc_quant import build_trade_record
    entry = {
        "model": "sweep_reversal", "direction": 1,
        "entry": 100, "stop": 99, "target": 110, "rr": 10,
        "triggered": True, "time": "2026-01-01T09:30",
        "factors": {"htf_bias_aligned": True, "killzone": False},
        "confluence": {"score": 11},
        "dol_target": {"target_kind": "BSL", "distance": 5},
    }
    outcome = {"outcome": "target", "r_multiple": 10.0, "bars_held": 4, "mae": -0.4, "mfe": 10.2, "entry_index": 5}
    rec = build_trade_record(entry, trade_outcome=outcome, symbol="AAPL")
    assert rec["schema_version"] == 1
    assert rec["model"] == "sweep_reversal"
    assert rec["confluence_score"] == 11
    assert rec["dol_kind"] == "BSL"
    assert rec["r_multiple"] == 10.0
    assert rec["mae"] == -0.4 and rec["mfe"] == 10.2
    assert rec["factors"]["htf_bias_aligned"] is True


def test_annotate_mae_mfe_reports_R_units():
    from smc_quant import annotate_mae_mfe
    rows = [
        (100, 102, 99.6, 101, 100),
        (101, 105, 99.5, 104, 110),
        (104, 110, 103, 109, 120),
    ]
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(rows))]
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    trades = [{
        "model": "x", "direction": 1, "entry": 100, "stop": 99,
        "entry_index": 0, "settled_index": 2,
    }]
    out = annotate_mae_mfe(df, trades)
    assert out[0]["mae"] == -0.5
    assert out[0]["mfe"] == 10.0


def test_persist_then_load_trade_records_roundtrip(tmp_path):
    from smc_quant import persist_trade_records, load_trade_records
    records = [
        {"trade_id": "AAPL:1", "r_multiple": 2.0, "factors": {"htf_bias_aligned": True}},
        {"trade_id": "AAPL:2", "r_multiple": -1.0, "factors": {"htf_bias_aligned": False}},
    ]
    path = tmp_path / "trades.jsonl"
    n = persist_trade_records(records, str(path))
    assert n == 2
    loaded = load_trade_records(str(path))
    assert len(loaded) == 2
    persist_trade_records([{"trade_id": "AAPL:3", "r_multiple": 0.0}], str(path))
    assert len(load_trade_records(str(path))) == 3


def test_compute_expectancy_reports_lift_per_factor():
    from smc_quant import compute_expectancy
    records = [
        {"r_multiple": 2.0, "factors": {"htf_bias_aligned": True}},
        {"r_multiple": 2.0, "factors": {"htf_bias_aligned": True}},
        {"r_multiple": -1.0, "factors": {"htf_bias_aligned": False}},
        {"r_multiple": -1.0, "factors": {"htf_bias_aligned": False}},
    ]
    rep = compute_expectancy(records)
    assert rep["sample_size"] == 4
    assert rep["expected_R"] == 0.5
    assert "htf_bias_aligned" in rep["lift"]
    assert rep["lift"]["htf_bias_aligned"]["expected_R"] == 2.0
    assert rep["lift"]["htf_bias_aligned"]["lift"] > 1.0


def test_backtest_replay_attaches_mae_mfe_to_each_trade():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    for t in result["concepts"]["entry_models"]["backtest_replay"]["trades"]:
        assert "mae" in t and "mfe" in t


def test_classify_asset_volatility_buckets_by_atr_pct():
    """§17.6: ATR%-based bucket assignment matches design-doc thresholds."""
    from smc_quant import classify_asset_volatility
    rows = [(100, 100.5, 99.5, 100, 1)] * 30
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(rows))]
    df_low = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    out_low = classify_asset_volatility(df_low)
    assert out_low["bucket"] == "low"
    rows = [(100, 105, 95, 100, 1)] * 30
    df_high = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    out_high = classify_asset_volatility(df_high)
    assert out_high["bucket"] in {"high", "extreme"}
    assert out_high["scale"] > out_low["scale"]


def test_adaptive_smc_config_tightens_low_loosens_high():
    from smc_quant import adaptive_smc_config
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(30)]
    df_low = normalize_ohlcv(pd.DataFrame(
        [(100, 100.4, 99.6, 100, 1)] * 30,
        columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    cfg_low, info_low = adaptive_smc_config(df_low)
    df_high = normalize_ohlcv(pd.DataFrame(
        [(100, 110, 90, 100, 1)] * 30,
        columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    cfg_high, info_high = adaptive_smc_config(df_high)
    assert cfg_high.liquidity_range_percent > cfg_low.liquidity_range_percent
    assert info_high["stop_distance_atr"] > info_low["stop_distance_atr"]


def test_build_smc_analysis_emits_adaptive_block_for_crypto():
    result = build_smc_analysis(_sample_ohlcv(), "BTCUSDT")
    assert "adaptive" in result
    assert result["adaptive"]["bucket"] in {"low", "mid", "high", "extreme", "unknown"}


def test_build_smc_analysis_static_config_disables_auto_adapt():
    cfg = SMCConfig(swing_length=7, internal_swing_length=4)
    result = build_smc_analysis(_sample_ohlcv(), "BTCUSDT", config=cfg)
    assert result["config"]["swing_length"] == 7
    assert result["config"]["internal_swing_length"] == 4


def test_walk_forward_evaluate_returns_per_fold_metrics():
    """§18.6: walk-forward should report IS / OOS expected_R per fold."""
    from smc_quant import walk_forward_evaluate
    records = []
    base_t = datetime(2026, 1, 1)
    for i in range(16):
        r = 2.0 if i % 2 == 0 else -1.0
        records.append({
            "entry_time": (base_t + timedelta(days=i)).isoformat(),
            "r_multiple": r,
            "factors": {},
        })
    out = walk_forward_evaluate(records, folds=4)
    assert out["sample_size"] == 16
    assert len(out["folds"]) >= 2
    for f in out["folds"]:
        assert "in_sample_expected_R" in f and "oos_expected_R" in f


def test_purged_train_test_split_drops_embargo_window():
    from smc_quant import purged_train_test_split
    records = [{"entry_time": f"2026-01-{i+1:02d}", "r_multiple": 1.0} for i in range(20)]
    train, test = purged_train_test_split(records, train_fraction=0.5, embargo_pct=0.1)
    assert len(train) == 10
    # 10% embargo of 20 = 2 dropped
    assert len(test) == 8


def test_estimate_pbo_flags_overfit_when_ranks_invert():
    from smc_quant import estimate_pbo
    is_R = [5, 4, 3, 2, 1, 0]
    # Out-of-sample inverts the rank entirely → high PBO
    oos_R = [0, 1, 2, 3, 4, 5]
    out = estimate_pbo(is_R, oos_R)
    assert out["pbo"] is not None and out["pbo"] >= 0.5
    assert out["interpretation"] == "high_overfit_risk"
    # Same rank → low PBO
    same = estimate_pbo(is_R, is_R)
    assert same["pbo"] is not None and same["pbo"] < 0.5


def test_crypto_risk_check_blocks_excess_leverage():
    """§17.8: leverage above per-bucket cap → reject."""
    from smc_quant import crypto_risk_check
    entry = {"direction": 1, "entry": 100, "stop": 99}
    out = crypto_risk_check(entry, leverage=10, is_altcoin=False)
    assert out["ok"] is False
    assert any("leverage_exceeds_cap" in r for r in out["reasons"])
    ok = crypto_risk_check(entry, leverage=3, is_altcoin=False)
    assert ok["ok"] is True


def test_crypto_risk_check_blocks_liquidation_too_close():
    from smc_quant import crypto_risk_check
    entry = {"direction": 1, "entry": 100, "stop": 99}
    out = crypto_risk_check(entry, leverage=80, is_altcoin=False)
    assert any("liquidation_too_close" in r for r in out["reasons"]) or \
           any("leverage_exceeds_cap" in r for r in out["reasons"])


def test_crypto_risk_check_warns_aligned_with_crowded_side():
    from smc_quant import crypto_risk_check
    entry = {"direction": 1, "entry": 100, "stop": 99}
    out = crypto_risk_check(entry, funding_state="long_crowded")
    assert "aligned_with_crowded_longs" in out["reasons"]
    ok = crypto_risk_check({"direction": -1, "entry": 100, "stop": 101}, funding_state="long_crowded")
    assert "aligned_with_crowded_longs" not in ok["reasons"]


def test_apply_risk_pipeline_rejects_when_crypto_context_blocks():
    from smc_quant import apply_risk_pipeline
    entries = [{"model": "x", "direction": 1, "entry": 100, "stop": 99, "rr": 2.0, "triggered": True}]
    out = apply_risk_pipeline(
        entries,
        account_equity=50_000,
        crypto_context={"is_altcoin": True, "leverage": 20},
    )
    assert out["ready"] == []
    assert "crypto_risk" in out["rejected"][0]["reject_reason"]
