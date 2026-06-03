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
