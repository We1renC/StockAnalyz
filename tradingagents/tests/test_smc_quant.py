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


def test_kelly_fraction_full_quarter_and_cap():
    """§18.4: full Kelly + fractional scaling + hard cap."""
    from smc_quant import kelly_fraction
    # 60% win rate, +2R wins, -1R losses → b=2, f* = (0.6*2 - 0.4)/2 = 0.4
    out = kelly_fraction(win_rate=0.6, avg_win_R=2.0, avg_loss_R=1.0, fractional=0.25)
    assert out["f_kelly"] == 0.4
    assert out["f_recommended"] == 0.05  # 0.4 * 0.25 = 0.10 → capped at 0.05
    # Lower fractional still respects cap
    aggressive = kelly_fraction(0.6, 2.0, 1.0, fractional=0.5, cap=0.10)
    assert aggressive["f_recommended"] == 0.10
    # Non-positive inputs
    safe = kelly_fraction(0, 0, 0)
    assert safe["f_recommended"] == 0


def test_calibrate_kelly_falls_back_when_sample_too_small():
    from smc_quant import calibrate_kelly_from_ledger
    out = calibrate_kelly_from_ledger([{"r_multiple": 1.0}] * 5)
    assert out["f_recommended"] == 0.01
    assert "insufficient_samples" in out["note"]


def test_calibrate_kelly_uses_ledger_expectancy_when_enough_samples():
    from smc_quant import calibrate_kelly_from_ledger
    records = []
    for _ in range(20):
        records.append({"r_multiple": 2.0})
    for _ in range(15):
        records.append({"r_multiple": -1.0})
    # win_rate ≈ 0.57, b=2 → kelly ≈ 0.35; fractional 0.25 → 0.0875; capped 0.05
    out = calibrate_kelly_from_ledger(records)
    assert out["sample_size"] == 35
    assert out["f_recommended"] <= 0.05


def test_mae_mfe_recommendations_suggests_widening_stop_when_winners_breach():
    """§18.3: ≥30% of winners breach 1R stop → recommend widening."""
    from smc_quant import mae_mfe_recommendations
    records = []
    # 25 winners — 60% deep-MAE breaches (MAE > 1R), 40% tight
    for i in range(25):
        deep = i < 15
        records.append({"r_multiple": 2.0, "mae": -1.4 if deep else -0.4, "mfe": 2.5})
    out = mae_mfe_recommendations(records, min_samples=20)
    assert out["sample_size"] == 25
    assert out["deep_mae_share"] >= 0.3
    kinds = {r["kind"] for r in out["recommendations"]}
    assert "widen_stop" in kinds
    widen = next(r for r in out["recommendations"] if r["kind"] == "widen_stop")
    assert widen["suggested_stop_R"] > 1.0


def test_mae_mfe_recommendations_suggests_stretching_tp_when_mfe_runs_far():
    """If winners' MFE far exceeds realised R → stretch TP."""
    from smc_quant import mae_mfe_recommendations
    records = [{"r_multiple": 2.0, "mae": -0.3, "mfe": 5.5} for _ in range(25)]
    out = mae_mfe_recommendations(records, min_samples=20)
    kinds = {r["kind"] for r in out["recommendations"]}
    assert "stretch_tp" in kinds


def test_mae_mfe_recommendations_blocks_when_sample_too_small():
    from smc_quant import mae_mfe_recommendations
    out = mae_mfe_recommendations([{"r_multiple": 2.0, "mae": -0.5, "mfe": 2.0}] * 5)
    assert "insufficient_winners" in out["note"]


def test_crypto_daily_levels_uses_prior_utc_day():
    """§17.5: PDH/PDL must come from the previous *completed* UTC day."""
    from smc_quant import crypto_daily_levels
    # Build 4-hour bars across two UTC days
    base = datetime(2026, 1, 1)
    rows = []
    idx = []
    for h in range(0, 24, 4):
        rows.append((100, 105, 95, 100, 1))
        idx.append(base + timedelta(hours=h))
    for h in range(0, 24, 4):
        rows.append((100, 110, 90, 100, 1))  # today wider
        idx.append(base + timedelta(days=1, hours=h))
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    out = crypto_daily_levels(df)
    assert out["status"] == "ok"
    assert out["previous_high"] == 105.0
    assert out["previous_low"] == 95.0
    assert out["boundary"] == "utc_00"


def test_crypto_daily_levels_returns_status_when_only_one_day():
    from smc_quant import crypto_daily_levels
    df = normalize_ohlcv(_sample_ohlcv().head(1))  # single bar → no prior UTC day
    out = crypto_daily_levels(df)
    assert out["status"] in {"insufficient_history", "no_prior_day"}


def test_weekend_illiquidity_downweights_crypto_only():
    from smc_quant import is_weekend_illiquid
    # Saturday bar
    sat = datetime(2026, 1, 3, 12, 0)  # Sat
    df = pd.DataFrame([[1, 1, 1, 1, 1]], columns=["Open", "High", "Low", "Close", "Volume"], index=[sat])
    df = normalize_ohlcv(df)
    crypto = is_weekend_illiquid(df, market="crypto")
    tradfi = is_weekend_illiquid(df, market="us")
    assert crypto["is_weekend"] is True and crypto["weight"] < 1.0
    assert tradfi["weight"] == 1.0


def test_build_smc_analysis_routes_utc_pdh_for_crypto():
    result = build_smc_analysis(
        _sample_ohlcv(), "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    prev = result["concepts"]["previous_levels"]
    assert "weekend_illiquidity" in result["concepts"]
    # Crypto branch should annotate the boundary marker
    assert prev.get("daily_boundary") in {"utc_00", None}


def test_build_journal_entry_carries_rationale_and_features():
    """§10.5: journal entry must carry rationale + emotional state + chart path."""
    from smc_quant import build_journal_entry
    trade = {
        "trade_id": "BTCUSDT:001", "symbol": "BTCUSDT", "market": "crypto",
        "model": "sweep_reversal", "direction": 1,
        "entry_time": "2026-01-02T10:30Z", "exit_time": "2026-01-02T12:00Z",
        "entry_price": 50000, "stop": 49500, "target": 51500,
        "confluence_score": 10, "factors": {"htf_bias_aligned": True},
        "outcome": "target", "r_multiple": 3.0, "mae": -0.3, "mfe": 3.2,
        "dol_kind": "BSL",
    }
    j = build_journal_entry(trade, rationale="Asia low swept, NY CHoCH",
                            emotional_state="calm", screenshot_path="/tmp/c10.png")
    assert j["schema_version"] == 1
    assert j["rationale"] == "Asia low swept, NY CHoCH"
    assert j["emotional_state"] == "calm"
    assert j["screenshot_path"] == "/tmp/c10.png"
    assert j["source"] == "paper"  # default
    assert j["confluence_score"] == 10


def test_edge_decay_check_flags_review_when_live_lags_backtest():
    """§18.6: live expected_R below 50% of backtest → review_required."""
    from smc_quant import edge_decay_check
    bt = [{"r_multiple": 2.0}] * 20 + [{"r_multiple": -1.0}] * 10  # +1R expectancy
    live = [{"r_multiple": 0.2}] * 25  # weak +0.2R live
    out = edge_decay_check(bt, live, min_live_samples=20, decay_threshold=0.5)
    assert out["review_required"] is True
    assert out["status"] == "decay_detected"


def test_edge_decay_check_returns_stable_when_live_meets_expectations():
    from smc_quant import edge_decay_check
    bt = [{"r_multiple": 2.0}] * 20 + [{"r_multiple": -1.0}] * 10
    live = [{"r_multiple": 2.0}] * 20 + [{"r_multiple": -1.0}] * 10
    out = edge_decay_check(bt, live, min_live_samples=20)
    assert out["review_required"] is False
    assert out["status"] == "stable"


def test_edge_decay_check_skips_when_live_sample_too_small():
    from smc_quant import edge_decay_check
    out = edge_decay_check([{"r_multiple": 2.0}] * 30, [{"r_multiple": 0.1}] * 5)
    assert out["status"] == "insufficient_live_samples"
    assert out["review_required"] is False


def test_inverse_fvg_flips_direction_when_fvg_closed_through():
    """§3.4: IFVG = a mitigated+inverse FVG, direction is the OPPOSITE of original."""
    from smc_quant import detect_inverse_fvgs
    h = normalize_ohlcv(_sample_ohlcv())
    fvgs = [
        {"index": 5, "direction": 1, "top": 12.0, "bottom": 11.0, "mid": 11.5,
         "mitigated": True, "inverse": True, "displacement_confirmed": True, "time": None},
        {"index": 6, "direction": -1, "top": 10.0, "bottom": 9.0, "mid": 9.5,
         "mitigated": False, "inverse": False, "time": None},
    ]
    out = detect_inverse_fvgs(h, fvgs)
    assert len(out) == 1
    assert out[0]["direction"] == -1  # bullish FVG inverted
    assert out[0]["original_direction"] == 1
    assert out[0]["block_type"] == "inverse_fvg"


def test_balanced_price_range_returns_overlap_between_opposing_fvgs():
    from smc_quant import detect_balanced_price_range
    h = normalize_ohlcv(_sample_ohlcv())
    fvgs = [
        {"index": 5, "direction": 1, "top": 12.0, "bottom": 10.0, "mid": 11.0, "time": None},
        {"index": 6, "direction": -1, "top": 11.5, "bottom": 9.5, "mid": 10.5, "time": None},
    ]
    out = detect_balanced_price_range(h, fvgs, max_gap_bars=2)
    assert len(out) == 1
    bpr = out[0]
    assert bpr["top"] == 11.5
    assert bpr["bottom"] == 10.0  # overlap [10.0, 11.5]
    assert bpr["block_type"] == "balanced_price_range"


def test_volume_imbalance_only_fires_on_body_gap_without_wick_overlap():
    from smc_quant import detect_volume_imbalance
    # Bar 0: decisive bullish body 100→110. Bar 1 opens at 112 with low 111 → VI gap [110,111].
    rows = [(100, 110, 99, 110, 1), (112, 115, 111, 114, 1)]
    idx = [datetime(2026, 1, 1), datetime(2026, 1, 2)]
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    out = detect_volume_imbalance(df)
    assert len(out) == 1
    vi = out[0]
    assert vi["direction"] == 1
    assert vi["bottom"] == 110.0 and vi["top"] == 111.0


def test_build_smc_analysis_exposes_fvg_extensions():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    for key in ("inverse_fvgs", "balanced_price_ranges", "volume_imbalances"):
        assert key in result["concepts"]
        assert isinstance(result["concepts"][key], list)


def test_spot_perp_divergence_flags_perp_led_rallies():
    """§17.3: perp moves +1% while spot stays flat → perp_led_up_warning."""
    from smc_quant import detect_spot_perp_divergence
    idx = [datetime(2026, 1, 1) + timedelta(hours=i) for i in range(15)]
    perp_rows = [(100, 100, 100, 100 + i * 0.1, 1) for i in range(15)]
    spot_rows = [(100, 100, 100, 100, 1)] * 15
    perp = normalize_ohlcv(pd.DataFrame(perp_rows, columns=["Open","High","Low","Close","Volume"], index=idx))
    spot = normalize_ohlcv(pd.DataFrame(spot_rows, columns=["Open","High","Low","Close","Volume"], index=idx))
    out = detect_spot_perp_divergence(perp, spot, lookback=12, move_threshold_pct=0.5)
    assert out["status"] == "ok"
    assert out["verdict"] == "perp_led_up_warning"
    assert out["perp_move_pct"] > out["spot_move_pct"]


def test_spot_perp_divergence_returns_no_data_when_inputs_missing():
    from smc_quant import detect_spot_perp_divergence
    out = detect_spot_perp_divergence(None, None)
    assert out["status"] == "no_data"


def test_cvd_slope_labels_aggressive_buying_when_slope_positive():
    from smc_quant import cvd_slope
    s = pd.Series([0, 5, 10, 15, 22, 30, 38, 47, 55, 63, 72])
    out = cvd_slope(s, window=10)
    assert out["status"] == "ok"
    assert out["slope"] > 0
    assert out["regime"] in {"mild_buying", "aggressive_buying"}


def test_build_crypto_overlay_routes_spot_and_cvd():
    from smc_quant import build_crypto_overlay
    h = normalize_ohlcv(_sample_ohlcv())
    spot_rows = [(100, 100, 100, 100, 1)] * len(h)
    spot = normalize_ohlcv(pd.DataFrame(spot_rows, columns=["Open","High","Low","Close","Volume"], index=h.index))
    cvd = pd.Series(range(len(h)), index=h.index, dtype=float)
    overlay = build_crypto_overlay(h, spot_df=spot, cvd=cvd)
    assert overlay["spot_perp"]["status"] == "ok"
    assert overlay["cvd_slope"]["status"] == "ok"
    # Factor map exposes the new toggles
    assert "perp_led_warning" in overlay["factors"]
    assert "cvd_aggressive_flow" in overlay["factors"]
    # Negative weight applied to the warning factor
    assert overlay["weights"]["perp_led_warning"] < 0


def test_chart_layers_include_fvg_extensions():
    """§6.1: C3b / C3c / C3d new chart codes for IFVG / BPR / VI."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    layers = result["visualization"]["chart_layers"]
    for code in ("C3b_inverse_fvgs", "C3c_balanced_price_ranges", "C3d_volume_imbalances"):
        assert code in layers
        assert "rects" in layers[code]


def test_unicorn_pool_accepts_inverse_fvgs():
    """§5.3 Unicorn POI pool should include IFVGs (direction flipped, treated as fresh)."""
    from smc_quant import detect_unicorn_entries
    breakers = [{"index": 5, "direction": 1, "top": 12.0, "bottom": 10.0}]
    # Original FVG was bullish but inverted → IFVG direction == -1
    inverse_fvgs = [{"index": 7, "direction": 1, "top": 12.5, "bottom": 11.0, "mid": 11.75,
                     "mitigated": False, "block_type": "inverse_fvg", "displacement_confirmed": True}]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_unicorn_entries(h, breakers, inverse_fvgs, [], {"state": "discount"}, "bullish")
    assert entries
    assert entries[0]["model"] == "unicorn"


def test_premium_discount_emits_full_fib_grid_and_dual_zone_labels():
    """§3.6: PD must expose Fibonacci sub-zone grid + both `zone` (5-bucket) and `state` (legacy 2-bucket)."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    pd_zone = result["concepts"]["premium_discount"]
    # Fibonacci grid present
    for key in ("fib_0_236", "fib_0_382", "fib_0_5", "fib_0_618", "fib_0_705", "fib_0_786"):
        assert key in pd_zone
        assert isinstance(pd_zone[key], (int, float))
    # Sub-zone label (five bucket) and legacy state co-exist
    assert pd_zone["zone"] in {"pure_discount", "discount", "equilibrium", "premium", "pure_premium"}
    assert pd_zone["state"] in {"discount", "equilibrium", "premium"}
    # position_pct can exceed [0,100] when close breaks beyond the range
    assert isinstance(pd_zone["position_pct"], (int, float))


def test_premium_discount_position_pct_matches_close_in_range():
    """position_pct = (close - low) / (high - low) × 100."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    pd_zone = result["concepts"]["premium_discount"]
    if not pd_zone:
        return
    leg = pd_zone["range_high"] - pd_zone["range_low"]
    close = float(_sample_ohlcv()["Close"].iloc[-1])
    expected = (close - pd_zone["range_low"]) / leg * 100
    assert abs(pd_zone["position_pct"] - expected) < 0.5


def test_propose_strategy_yaml_refuses_when_sample_too_small():
    """§18.6: cannot adopt below the minimum-sample floor."""
    from smc_quant import propose_strategy_yaml
    out = propose_strategy_yaml(trade_records=[{"r_multiple": 1}] * 5)
    assert out["adopt"] is False
    assert out["status"] == "insufficient_samples"


def test_propose_strategy_yaml_emits_full_changelog():
    from smc_quant import propose_strategy_yaml
    records = []
    base = datetime(2026, 1, 1)
    # 40 trades, 60% winners with htf_bias_aligned drives positive edge
    for i in range(40):
        r = 2.0 if i % 5 != 0 else -1.0
        records.append({
            "entry_time": (base + timedelta(days=i)).isoformat(),
            "r_multiple": r,
            "factors": {"htf_bias_aligned": True},
            "mae": -0.3, "mfe": 2.2,
        })
    out = propose_strategy_yaml(trade_records=records, min_samples=20)
    assert out["schema_version"] == 1
    assert out["sample_size"] == 40
    # Must expose all key calibration sub-blocks
    for key in ("expectancy", "confluence", "risk", "stop_target_calibration", "validation", "changelog"):
        assert key in out
    # Changelog is a flat list of human-readable lines
    assert isinstance(out["changelog"], list) and out["changelog"]
    # Adoption decision aligns with walk_forward.passes
    assert out["adopt"] == out["validation"]["walk_forward"]["passes"]


def test_propose_strategy_yaml_marks_review_required_when_walk_forward_fails():
    """Alternating big-win then big-loss across folds → walk-forward should flag OOS decay."""
    from smc_quant import propose_strategy_yaml
    records = []
    base = datetime(2026, 1, 1)
    for i in range(40):
        # Wins concentrated in first half, losses in second → OOS regression
        r = 3.0 if i < 20 else -1.0
        records.append({
            "entry_time": (base + timedelta(days=i)).isoformat(),
            "r_multiple": r,
            "factors": {"htf_bias_aligned": True},
            "mae": -0.5, "mfe": 3.0,
        })
    out = propose_strategy_yaml(trade_records=records, min_samples=20)
    if out["validation"]["walk_forward"]["passes"] is False:
        assert out["adopt"] is False
        assert out["status"] == "review_required"


def test_multi_exchange_aggregator_flags_single_venue_wick():
    """§17.9: one venue prints a 5% wick while two others stay tight → anomaly."""
    from smc_quant import aggregate_multi_exchange
    idx = [datetime(2026, 1, 1) + timedelta(hours=i) for i in range(5)]
    base = pd.DataFrame(
        [(100, 101, 99, 100, 1)] * 5,
        columns=["Open", "High", "Low", "Close", "Volume"], index=idx,
    )
    wick = base.copy()
    wick.loc[idx[2], "High"] = 110  # 10% wick on one venue
    out = aggregate_multi_exchange({"A": base, "B": base.copy(), "C": wick}, wick_outlier_pct=2.0)
    assert out["consensus_df"] is not None
    assert any(a["exchange"] == "C" for a in out["wick_anomalies"])
    assert out["sample_size"] >= 5


def test_multi_exchange_aggregator_single_venue_marked_as_note():
    from smc_quant import aggregate_multi_exchange
    idx = [datetime(2026, 1, 1) + timedelta(hours=i) for i in range(3)]
    df = normalize_ohlcv(pd.DataFrame(
        [(100, 101, 99, 100, 1)] * 3, columns=["Open","High","Low","Close","Volume"], index=idx))
    out = aggregate_multi_exchange({"A": df})
    assert out["sample_size"] == 3
    assert "single_venue_only" in out.get("note", "")


def test_multi_exchange_aggregator_returns_empty_on_no_feeds():
    from smc_quant import aggregate_multi_exchange
    out = aggregate_multi_exchange({})
    assert out["sample_size"] == 0
    assert out["consensus_df"] is None


def test_merge_crypto_factors_appends_factor_and_weight_pairs():
    """§17.10: crypto overlay factors must merge into the confluence map."""
    from smc_quant import merge_crypto_factors
    base_factors = {"htf_bias_aligned": True}
    overlay = {
        "status": "ok",
        "factors": {"oi_drop_at_sweep": True, "perp_led_warning": True},
        "weights": {"oi_drop_at_sweep": 2, "perp_led_warning": -2},
    }
    f, w = merge_crypto_factors(base_factors, overlay)
    assert f["oi_drop_at_sweep"] is True
    assert f["perp_led_warning"] is True
    assert f["htf_bias_aligned"] is True  # base preserved
    assert w["oi_drop_at_sweep"] == 2
    assert w["perp_led_warning"] == -2


def test_score_confluence_subtracts_negative_weight_factors():
    """Drag factors (negative weight) must reduce the score."""
    from smc_quant import score_confluence
    factors = {"liquidity_swept": True, "ltf_choch": True, "perp_led_warning": True}
    weights = {"perp_led_warning": -2}
    s = score_confluence(factors, weights=weights)
    # 2 + 2 - 2 = 2
    assert s["score"] == 2
    names = {f["factor"] for f in s["contributing_factors"]}
    assert "perp_led_warning" in names


def test_merge_crypto_factors_skips_no_data_overlay():
    from smc_quant import merge_crypto_factors
    base = {"htf_bias_aligned": True}
    f, w = merge_crypto_factors(base, {"status": "no_data"})
    assert f == base
    assert w == {}


def test_btc_dominance_regime_flags_altseason_when_falling_hard():
    """§17.4 / §17.7: BTC.D dropping ≥0.5% over long window + falling regime → altseason."""
    from smc_quant import classify_btc_dominance_regime
    idx = pd.date_range("2026-01-01", periods=25, freq="D")
    # Linear decline from 55 → 50 (-9% over the window)
    btc_d = pd.Series([55 - i * 0.2 for i in range(25)], index=idx)
    out = classify_btc_dominance_regime(btc_d, short_window=5, long_window=20)
    assert out["status"] == "ok"
    assert out["regime"] == "btc_dominance_falling"
    assert out["altseason"] is True


def test_btc_dominance_regime_returns_no_data_when_history_short():
    from smc_quant import classify_btc_dominance_regime
    out = classify_btc_dominance_regime(pd.Series([55, 54, 53]))
    assert out["status"] == "no_data"
    assert out["altseason"] is False


def test_crypto_overlay_routes_btc_dominance_and_altseason_tailwind():
    from smc_quant import build_crypto_overlay
    h = normalize_ohlcv(_sample_ohlcv())
    btc_d = pd.Series([55 - i * 0.2 for i in range(25)])
    overlay = build_crypto_overlay(h, btc_dominance=btc_d, is_altcoin=True)
    assert overlay["btc_dominance"]["regime"] == "btc_dominance_falling"
    # altcoin + altseason → tailwind factor active
    assert overlay["factors"]["altseason_tailwind"] is True
    assert overlay["weights"]["altseason_tailwind"] == 2


def test_classify_liquidity_internal_external_splits_by_range_extremes():
    """§3.5: external = touches range_high/low ±0.5%, internal sits between."""
    from smc_quant import classify_liquidity_internal_external
    pd_zone = {"range_high": 100.0, "range_low": 80.0}
    pools = [
        {"type": "BSL", "level": 100.1},  # external (top)
        {"type": "SSL", "level": 79.95},  # external (bottom)
        {"type": "BSL", "level": 92.0},   # internal
        {"type": "BSL", "level": 110.0},  # out_of_range
    ]
    out = classify_liquidity_internal_external(pools, pd_zone)
    kinds = [p["liquidity_kind"] for p in out]
    assert kinds == ["external", "external", "internal", "out_of_range"]


def test_classify_liquidity_empty_zone_returns_unknown():
    from smc_quant import classify_liquidity_internal_external
    out = classify_liquidity_internal_external(
        [{"type": "BSL", "level": 100}], {}
    )
    assert out[0]["liquidity_kind"] == "unknown"


def test_build_smc_analysis_attaches_liquidity_kind_field():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    for liq in result["concepts"]["liquidity"]:
        assert "liquidity_kind" in liq
        assert liq["liquidity_kind"] in {"internal", "external", "out_of_range", "unknown"}


def test_resolve_dol_prefers_external_over_internal_liquidity():
    """§3.5: external pool wins even if an internal pool is closer."""
    from smc_quant import resolve_dol_target
    pools = [
        {"type": "BSL", "level": 105, "swept": False, "end_index": 5, "liquidity_kind": "internal"},
        {"type": "BSL", "level": 120, "swept": False, "end_index": 6, "liquidity_kind": "external"},
    ]
    out = resolve_dol_target(1, current_price=100, liquidity=pools)
    assert out["target_price"] == 120.0  # external wins despite being farther
    assert out["liquidity_kind"] == "external"


def test_resolve_dol_falls_through_to_internal_when_no_external():
    from smc_quant import resolve_dol_target
    pools = [
        {"type": "BSL", "level": 105, "swept": False, "end_index": 5, "liquidity_kind": "internal"},
    ]
    out = resolve_dol_target(1, current_price=100, liquidity=pools)
    assert out["target_price"] == 105.0
    assert out["liquidity_kind"] == "internal"


def test_displacement_strength_grading_by_atr_multiple():
    """§3.11: ATR-multiple buckets — extreme / strong / normal / body_only."""
    from smc_quant import detect_displacement, SMCConfig
    # Build a sequence where the last bar is a 3× ATR candle (extreme).
    rows = [(100, 101, 99, 100, 1)] * 14 + [(100, 110, 99, 109, 5)]
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(rows))]
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx))
    out = detect_displacement(df, SMCConfig())
    assert out, "expected at least one displacement"
    last = out[-1]
    assert last["strength"] in {"extreme", "strong"}
    assert last["atr_multiple"] >= 1.8


def test_displacement_fields_present_on_every_event():
    from smc_quant import detect_displacement, SMCConfig
    h = normalize_ohlcv(_sample_ohlcv())
    out = detect_displacement(h, SMCConfig())
    for d in out:
        assert d["strength"] in {"extreme", "strong", "normal", "body_only"}
        assert "atr_multiple" in d


def test_killzone_classifier_returns_per_market_buckets():
    """§3.9: each market gets its own killzone label + weight."""
    from smc_quant import classify_killzone
    # TW 09:30 → tw_open
    tw_idx = pd.DatetimeIndex([pd.Timestamp("2026-01-05 09:30")])  # local TW time
    tw_df = pd.DataFrame([[1,1,1,1,1]], columns=["Open","High","Low","Close","Volume"], index=tw_idx)
    tw_df = normalize_ohlcv(tw_df)
    out_tw = classify_killzone(tw_df, "tw")
    assert out_tw["zone"] == "tw_open"
    assert out_tw["weight"] > 1.0
    # Crypto London 08:00 UTC → london_killzone
    crypto_idx = pd.DatetimeIndex([pd.Timestamp("2026-01-05 08:00")])
    crypto_df = pd.DataFrame([[1,1,1,1,1]], columns=["Open","High","Low","Close","Volume"], index=crypto_idx)
    crypto_df = normalize_ohlcv(crypto_df)
    out_c = classify_killzone(crypto_df, "crypto")
    assert out_c["zone"] == "london_killzone"
    assert out_c["weight"] >= 1.4


def test_killzone_classifier_returns_quiet_outside_session():
    from smc_quant import classify_killzone
    idx = pd.DatetimeIndex([pd.Timestamp("2026-01-05 20:00")])
    df = pd.DataFrame([[1,1,1,1,1]], columns=["Open","High","Low","Close","Volume"], index=idx)
    df = normalize_ohlcv(df)
    out = classify_killzone(df, "crypto")
    assert out["zone"] in {"crypto_quiet", "ny_killzone"}


def test_build_smc_analysis_attaches_killzone_zone_field():
    result = build_smc_analysis(
        _sample_ohlcv(), "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    sess = result["concepts"]["sessions"]
    assert "zone" in sess
    assert "weight" in sess


def test_top_down_audit_emits_six_step_checklist():
    """§4: HTF→MTF→LTF audit must enumerate all 6 design-doc steps."""
    sample = _sample_ohlcv()
    result = build_mtf_analysis(
        {"htf": sample, "mtf": sample, "ltf": sample},
        "BTCUSDT",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    audit = result["top_down"]["audit"]
    step_names = [s["name"] for s in audit["steps"]]
    assert step_names == [
        "htf_bias_set",
        "htf_poi_present",
        "mtf_reaction_aligned",
        "ltf_bias_aligned",
        "ltf_choch_trigger",
        "poi_ranked",
    ]
    # max_score = 6 + score field present
    assert audit["max_score"] == 6
    assert 0 <= audit["score"] <= audit["max_score"]
    assert isinstance(audit["ready"], bool)


def test_equilibrium_reactions_count_oscillations_around_50pct():
    """§3.6: bars that wick across EQ but close on one side count as reactions."""
    from smc_quant import track_equilibrium_reactions
    pd_zone = {"equilibrium": 100.0, "range_high": 110, "range_low": 90}
    rows = [
        (95, 102, 95, 96, 1),    # wicks above EQ, closes below → reaction
        (96, 101, 96, 99, 1),    # wicks above EQ, closes below → reaction
        (99, 102, 98, 101, 1),   # closes above
        (101, 103, 99, 102, 1),  # closes above
    ]
    idx = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(len(rows))]
    df = normalize_ohlcv(pd.DataFrame(rows, columns=["Open","High","Low","Close","Volume"], index=idx))
    out = track_equilibrium_reactions(df, pd_zone, lookback=10)
    assert out["reactions"] >= 2
    assert out["active"] is True
    assert out["flips"] >= 1


def test_equilibrium_reactions_inactive_when_no_eq_data():
    from smc_quant import track_equilibrium_reactions
    h = normalize_ohlcv(_sample_ohlcv())
    out = track_equilibrium_reactions(h, {})
    assert out["active"] is False
    assert out["reactions"] == 0


def test_build_smc_analysis_routes_equilibrium_reactions():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    pd_zone = result["concepts"]["premium_discount"]
    if pd_zone:
        assert "equilibrium_reactions" in pd_zone
        assert "active" in pd_zone["equilibrium_reactions"]


def test_judas_events_carry_displacement_strength_field():
    """§3.11: every Judas event records the strongest displacement that confirmed it."""
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    events = detect_judas_swings(h, structure, liquidity, displacements, "AAPL")
    for ev in events:
        assert "displacement_strength" in ev
        assert ev["displacement_strength"] in {"none", "body_only", "normal", "strong", "extreme"}


def test_sweep_reversal_entry_credits_extreme_displacement():
    """§5.2 + §3.11: an entry with displacement_strength=extreme picks up +1 displacement_extreme."""
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "extreme",
        "session_at_sweep": None, "killzone": False,
        "sweep_time": None,
    }]
    obs = [{
        "index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
        "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
        "displacement_confirmed": True,
    }]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_sweep_reversal_entries(h, judas, obs, [], {"state": "discount"}, "bullish")
    assert entries
    e = entries[0]
    assert e["factors"]["displacement_extreme"] is True
    names = {f["factor"] for f in e["confluence"]["contributing_factors"]}
    assert "displacement_extreme" in names


def test_is_premium_killzone_only_fires_on_top_tier_sessions():
    """§3.9: ny_open / london_killzone / silver_bullet / tw_open count as premium."""
    from smc_quant import is_premium_killzone
    assert is_premium_killzone({"zone": "ny_open"})
    assert is_premium_killzone({"zone": "london_killzone"})
    assert is_premium_killzone({"zone": "ny_silver_bullet"})
    assert is_premium_killzone({"zone": "tw_open"})
    # Lower-tier zones don't count
    assert not is_premium_killzone({"zone": "asia_session"})
    assert not is_premium_killzone({"zone": "crypto_quiet"})
    assert not is_premium_killzone(None)


def test_sweep_reversal_entry_picks_up_premium_killzone_factor():
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{
        "index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
        "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
        "displacement_confirmed": True,
    }]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_sweep_reversal_entries(
        h, judas, obs, [], {"state": "discount"}, "bullish",
        session={"zone": "ny_open", "killzone": True},
    )
    assert entries
    assert entries[0]["factors"]["killzone_premium"] is True


def test_liquidity_records_carry_equal_tag_and_tier():
    """§3.5: clusters ≥2 touches expose EQH/EQL tag; ≥3 escalates to strong."""
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    liqs = detect_liquidity(h, swings, cfg)
    for l in liqs:
        assert "equal_tag" in l and "equal_tier" in l
        if l["touches"] >= 2:
            assert l["equal_tag"] in {"EQH", "EQL"}
            assert l["equal_tier"] in {"weak", "strong"}
        else:
            assert l["equal_tag"] is None
        assert l["level_dispersion"] >= 0


def test_dol_strong_eqh_escalates_internal_priority():
    """§3.5: strong EQH internal escalates one bucket → beats untagged internal."""
    from smc_quant import resolve_dol_target
    pools = [
        # Untagged internal pool, closer
        {"type": "BSL", "level": 105, "swept": False, "end_index": 5, "liquidity_kind": "internal"},
        # Strong EQH internal pool, farther
        {"type": "BSL", "level": 110, "swept": False, "end_index": 6,
         "liquidity_kind": "internal", "equal_tag": "EQH", "equal_tier": "strong"},
    ]
    out = resolve_dol_target(1, current_price=100, liquidity=pools)
    # Strong EQH escalates 2 → 1, beats plain internal at bucket 2
    assert out["target_price"] == 110.0
    assert out["equal_tier"] == "strong"


def test_round_number_magnets_lists_proximate_levels_only():
    """§3.5: only round levels within ±1% of current price are active magnets."""
    from smc_quant import detect_round_number_magnets
    # At 100 with step 1, levels 97..103 generated; within 1% are 99,100,101
    out = detect_round_number_magnets(100.0, proximity_pct=1.0)
    active = [r for r in out if r["active_magnet"]]
    levels = {r["level"] for r in active}
    assert 100.0 in levels
    # Distances are sorted ascending
    assert out[0]["distance_pct"] == 0.0


def test_round_number_magnets_empty_when_price_invalid():
    from smc_quant import detect_round_number_magnets
    assert detect_round_number_magnets(0) == []
    assert detect_round_number_magnets(None) == []


def test_build_smc_analysis_exposes_round_number_magnets():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    assert "round_number_magnets" in result["concepts"]
    for m in result["concepts"]["round_number_magnets"]:
        assert "level" in m and "distance_pct" in m and "active_magnet" in m


def test_resolve_dol_uses_round_number_when_no_liquidity():
    """§3.5: round-number magnet acts as fallback when no liquidity / PDH / FVG."""
    from smc_quant import resolve_dol_target
    round_magnets = [
        {"level": 105.0, "distance_pct": 5.0, "active_magnet": False},
        {"level": 110.0, "distance_pct": 10.0, "active_magnet": False},
    ]
    out = resolve_dol_target(
        1, current_price=100.0, liquidity=[],
        round_magnets=round_magnets,
    )
    assert out is not None
    assert out["target_kind"] == "ROUND"
    assert out["target_price"] == 105.0


def test_resolve_dol_external_still_wins_over_round_number():
    """Round number should NOT outrank an external pool."""
    from smc_quant import resolve_dol_target
    pools = [
        {"type": "BSL", "level": 120, "swept": False, "end_index": 5, "liquidity_kind": "external"},
    ]
    out = resolve_dol_target(
        1, current_price=100, liquidity=pools,
        round_magnets=[{"level": 105, "distance_pct": 5.0}],
    )
    assert out["target_kind"] == "BSL"
    assert out["target_price"] == 120.0


def test_suggest_crypto_weights_only_touches_crypto_namespaced_factors():
    """§17.10: per-crypto weight suggestion lifts cvd_divergence when edge ≥ +0.5."""
    from smc_quant import _suggest_crypto_weights
    edge = {
        "factors": {
            "crypto:cvd_divergence": {"n_with": 10, "n_without": 10, "edge": 1.0},
            "crypto:funding_extreme_contrarian": {"n_with": 10, "n_without": 10, "edge": -0.7},
            # Non-crypto namespace must be ignored
            "htf_bias_aligned": {"n_with": 10, "n_without": 10, "edge": 1.5},
        }
    }
    out = _suggest_crypto_weights(edge)
    # cvd_divergence default 2 → +1 = 3
    assert out["cvd_divergence"] == 3
    # funding_extreme_contrarian default 1 → -1 = 0
    assert out["funding_extreme_contrarian"] == 0
    # non-crypto factor never appears in output
    assert "htf_bias_aligned" not in out


def test_propose_strategy_yaml_includes_crypto_weights_block():
    from smc_quant import propose_strategy_yaml
    records = []
    base = datetime(2026, 1, 1)
    for i in range(40):
        r = 2.0 if i % 5 != 0 else -1.0
        records.append({
            "entry_time": (base + timedelta(days=i)).isoformat(),
            "r_multiple": r,
            "factors": {"htf_bias_aligned": True},
            "crypto_factors": {"cvd_divergence": True, "perp_led_warning": False},
            "mae": -0.3, "mfe": 2.2,
        })
    out = propose_strategy_yaml(trade_records=records, min_samples=20)
    assert "crypto_weights_suggested" in out["confluence"]
    crypto = out["confluence"]["crypto_weights_suggested"]
    assert "cvd_divergence" in crypto
    assert "altcoin_btc_aligned" in crypto  # confirms full default seed coverage


def test_sweep_reversal_entry_credits_pd_extreme_factor():
    """§3.6: pure_discount + long entry → factors.pd_extreme True."""
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{
        "index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
        "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
        "displacement_confirmed": True,
    }]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_sweep_reversal_entries(
        h, judas, obs, [],
        {"state": "discount", "zone": "pure_discount"},
        "bullish",
    )
    assert entries
    assert entries[0]["factors"]["pd_extreme"] is True
    names = {f["factor"] for f in entries[0]["confluence"]["contributing_factors"]}
    assert "pd_extreme" in names


def test_pd_extreme_and_killzone_premium_propagate_to_all_entry_models():
    """§3.6 + §3.9: pd_extreme + killzone_premium must appear in every model's factors."""
    from smc_quant import (
        detect_continuation_entries, detect_ote_entries,
        detect_unicorn_entries, detect_silver_bullet_entries, ote_zone,
    )
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    obs = detect_order_blocks(h, structure, displacements, liquidity)
    fvgs = [{"index": 22, "direction": 1, "top": 18.0, "bottom": 17.0, "mid": 17.5, "mitigated": False, "displacement_confirmed": True}]
    pd_zone = {"state": "discount", "zone": "pure_discount"}
    session = {"zone": "ny_open", "killzone": True}
    bias = "bullish"
    # Continuation
    cont = detect_continuation_entries(h, structure, obs, fvgs, pd_zone, bias, session)
    # OTE
    ote_block = ote_zone(swings, bias)
    ote = detect_ote_entries(h, ote_block, obs, fvgs, pd_zone, bias, session)
    # Unicorn
    breakers = [{"index": 5, "direction": 1, "top": 18.0, "bottom": 17.0}]
    uni = detect_unicorn_entries(h, breakers, fvgs, [], pd_zone, bias, session)
    # Silver Bullet
    sb = detect_silver_bullet_entries(h, liquidity, fvgs, "AAPL", pd_zone, bias, session)
    for collection in (cont, ote, uni, sb):
        for e in collection:
            assert "pd_extreme" in e["factors"]
            assert "killzone_premium" in e["factors"]


def test_suggest_weights_includes_extension_factor_seeds():
    """§3.5/§3.6/§3.9/§3.11 + §17 extension factors get default seeds."""
    from smc_quant import suggest_confluence_weights
    out = suggest_confluence_weights({"factors": {}})
    # Extension factors must appear in the suggested weights baseline
    for key in ("displacement_extreme", "killzone_premium", "pd_extreme",
                "perp_led_warning", "cvd_aggressive_flow", "altseason_tailwind"):
        assert key in out
    assert out["perp_led_warning"] == -2  # drag retained


def test_suggest_weights_lets_drag_factor_grow_more_negative():
    """Negative edge ≤ -0.5 on a drag factor → weight decreases (floor -3)."""
    from smc_quant import suggest_confluence_weights
    edge = {
        "factors": {
            "perp_led_warning": {"n_with": 10, "n_without": 10, "edge": -1.5},
        }
    }
    out = suggest_confluence_weights(edge)
    # base -2, edge ≤ -0.5 → -3 (capped)
    assert out["perp_led_warning"] == -3


def test_suggest_weights_lifts_positive_edge_extension_factor():
    """A positive edge ≥ +0.5 on pd_extreme bumps its weight."""
    from smc_quant import suggest_confluence_weights
    edge = {"factors": {"pd_extreme": {"n_with": 10, "n_without": 10, "edge": 1.2}}}
    out = suggest_confluence_weights(edge)
    # Extension default 1 + 1 = 2
    assert out["pd_extreme"] == 2


def test_chart_layer_c5_exposes_fib_grid_and_eq_reactions():
    """§6.1: C5_premium_discount now carries the §3.6 Fib grid + EQ activity."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    c5 = result["visualization"]["chart_layers"]["C5_premium_discount"]
    assert "fib_grid" in c5
    grid = c5["fib_grid"]
    for level in ("0.236", "0.382", "0.500", "0.618", "0.705", "0.786"):
        assert level in grid
    assert "equilibrium_reactions" in c5
    assert "position_pct" in c5


def test_chart_layer_c4_exposes_liquidity_kind_and_equal_tags():
    """§6.1: C4 levels must carry liquidity_kind + equal_tag + equal_tier."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    levels = result["visualization"]["chart_layers"]["C4_liquidity"]["levels"]
    for l in levels:
        assert "liquidity_kind" in l
        assert "equal_tag" in l
        assert "equal_tier" in l
        assert "touches" in l


def test_chart_layer_c9_mtf_audit_present_in_chart_layers():
    """§6.1: C9 MTF audit panel exists, even if empty for single-TF analysis."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    c9 = result["visualization"]["chart_layers"].get("C9_mtf_audit")
    assert c9 is not None
    assert c9["kind"] == "summary_panel"
    assert "rows" in c9


def test_crypto_readiness_checklist_emits_six_step_audit():
    """§17.11: six-step crypto rollout checklist must enumerate all design-doc items."""
    from smc_quant import crypto_readiness_checklist
    analysis = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    out = crypto_readiness_checklist(analysis)
    assert out["max_score"] == 6
    names = [s["name"] for s in out["steps"]]
    assert names == [
        "ccxt_core_engine",
        "visible_liquidity_overlay",
        "cross_market_footprint",
        "atr_adaptive_params",
        "batch_backtest_executed",
        "engine_extends_to_tradfi",
    ]
    # ready_for_live is True only when all six pass
    assert out["ready_for_live"] == (out["score"] == 6)


def test_crypto_readiness_handles_empty_analysis():
    from smc_quant import crypto_readiness_checklist
    out = crypto_readiness_checklist({})
    assert out["ready_for_live"] is False
    assert out["score"] == 0


def test_rule_enforcement_dashboard_reports_four_mandatory_numbers():
    """§10.5: dashboard must output equity / daily buffer / DD buffer / active days."""
    from smc_quant import rule_enforcement_dashboard
    out = rule_enforcement_dashboard(
        account_equity=100_000,
        daily_realized_pnl=-15_000,
        max_drawdown=-20_000,
        active_days_traded=7,
    )
    assert out["account_equity"] == 100_000
    assert out["daily_loss_buffer"] == 50_000 - 15_000  # 35k remaining
    assert out["max_drawdown_buffer"] == 50_000 - 20_000  # 30k remaining
    assert out["active_days_traded"] == 7
    assert out["headline"] == "LIVE"


def test_rule_enforcement_dashboard_switches_to_defensive_when_profit_hit():
    from smc_quant import rule_enforcement_dashboard
    out = rule_enforcement_dashboard(
        account_equity=180_000,
        realized_profit_this_period=85_000,  # > +80k → defensive mode
    )
    assert out["defensive_mode"] is True
    assert out["headline"] == "DEFENSIVE"


def test_rule_enforcement_dashboard_locks_on_excess_daily_loss():
    from smc_quant import rule_enforcement_dashboard
    out = rule_enforcement_dashboard(
        account_equity=100_000,
        daily_realized_pnl=-60_000,  # exceeds daily floor → locked
    )
    assert out["locked"] is True
    assert out["headline"] == "LOCKED"


def test_stamp_rule_enforcement_records_entry_time_state():
    """§10.6: trade record must carry headline+four-numbers snapshot at entry."""
    from smc_quant import stamp_rule_enforcement_at_entry, rule_enforcement_dashboard
    dash = rule_enforcement_dashboard(
        account_equity=200_000,
        daily_realized_pnl=-5_000,
        max_drawdown=-10_000,
        active_days_traded=3,
        realized_profit_this_period=85_000,  # defensive
    )
    rec = stamp_rule_enforcement_at_entry({"trade_id": "T1"}, dash)
    snap = rec["rule_enforcement_at_entry"]
    assert snap["headline"] == "DEFENSIVE"
    assert snap["account_equity"] == 200_000
    assert snap["active_days_traded"] == 3
    assert snap["defensive_mode"] is True
    # Original record fields preserved
    assert rec["trade_id"] == "T1"


def test_stamp_rule_enforcement_no_op_on_empty_record():
    from smc_quant import stamp_rule_enforcement_at_entry, rule_enforcement_dashboard
    rec = stamp_rule_enforcement_at_entry({}, rule_enforcement_dashboard(account_equity=0))
    assert rec == {}


def test_validate_emotional_state_flags_high_risk_regimes():
    """§10.5: fomo / revenge / tilted / overconfident → risk_flag True."""
    from smc_quant import validate_emotional_state
    for state in ("fomo", "revenge", "tilted", "overconfident"):
        out = validate_emotional_state(state)
        assert out["risk_flag"] is True
    for state in ("calm", "confident", "anxious"):
        out = validate_emotional_state(state)
        assert out["risk_flag"] is False
    # Unknown state → risk_flag True + note
    bad = validate_emotional_state("euphoric")
    assert bad["risk_flag"] is True
    assert bad.get("note") == "unknown_state"


def test_journal_emotional_summary_identifies_worst_state():
    """§10.5: emotional slicing identifies the state with the worst avg R (≥3 trades)."""
    from smc_quant import journal_emotional_summary
    entries = [
        {"emotional_state": "calm", "r_multiple": 2.0},
        {"emotional_state": "calm", "r_multiple": 1.5},
        {"emotional_state": "calm", "r_multiple": 1.0},
        {"emotional_state": "revenge", "r_multiple": -1.0},
        {"emotional_state": "revenge", "r_multiple": -1.0},
        {"emotional_state": "revenge", "r_multiple": -1.0},
        {"emotional_state": "fomo", "r_multiple": -0.5},
        {"emotional_state": None, "r_multiple": 0.5},
    ]
    out = journal_emotional_summary(entries)
    assert out["sample_size"] == 8
    assert out["worst_state"] == "revenge"
    assert out["by_state"]["calm"]["avg_R"] == 1.5
    assert out["by_state"]["revenge"]["avg_R"] == -1.0
    assert "unspecified" in out["by_state"]


def test_journal_emotional_summary_empty_input():
    from smc_quant import journal_emotional_summary
    out = journal_emotional_summary([])
    assert out["sample_size"] == 0
    assert out["worst_state"] is None


def test_cluster_trades_by_groups_and_finds_best_and_worst():
    """§18.3: grid by (model, market) → best / worst cluster identified."""
    from smc_quant import cluster_trades_by
    records = [
        # silver_bullet × crypto → strong avg R 2
        {"model": "silver_bullet", "market": "crypto", "r_multiple": 2},
        {"model": "silver_bullet", "market": "crypto", "r_multiple": 3},
        {"model": "silver_bullet", "market": "crypto", "r_multiple": 1},
        # sweep_reversal × us → weak avg R -0.5
        {"model": "sweep_reversal", "market": "us", "r_multiple": -1},
        {"model": "sweep_reversal", "market": "us", "r_multiple": -1},
        {"model": "sweep_reversal", "market": "us", "r_multiple": 0.5},
        # too_small bucket
        {"model": "unicorn", "market": "crypto", "r_multiple": 5},
    ]
    out = cluster_trades_by(records, ["model", "market"], min_cluster_size=3)
    assert out["best_cluster"]["key"] == "silver_bullet / crypto"
    assert out["best_cluster"]["avg_R"] == 2.0
    assert out["worst_cluster"]["key"] == "sweep_reversal / us"
    # too_small captures the 1-sample unicorn row
    assert any(r["dims"]["model"] == "unicorn" for r in out["too_small"])


def test_cluster_trades_by_empty_inputs():
    from smc_quant import cluster_trades_by
    out = cluster_trades_by([], ["model"])
    assert out["clusters"] == {}
    assert out["best_cluster"] is None


def test_r_multiple_distribution_buckets_fat_tails():
    """§18.3: distribution exposes counts per bin + fat-loss / fat-win shares."""
    from smc_quant import r_multiple_distribution
    records = [
        {"r_multiple": -3},   # fat loss
        {"r_multiple": -1.5}, # fat loss
        {"r_multiple": -0.5}, # mid loss
        {"r_multiple": 0.3},  # small win
        {"r_multiple": 1.2},  # mid win
        {"r_multiple": 2.5},  # fat win
        {"r_multiple": 3.5},  # fat win
    ]
    out = r_multiple_distribution(records)
    assert out["sample_size"] == 7
    # Two values in ≤ -1R, two values in ≥ +2R
    assert out["fat_loss_share"] == round(2 / 7, 4)
    assert out["fat_win_share"] == round(2 / 7, 4)
    assert sum(out["counts"]) == 7


def test_r_multiple_distribution_empty():
    from smc_quant import r_multiple_distribution
    out = r_multiple_distribution([])
    assert out["sample_size"] == 0
    assert out["counts"] == []


def test_propose_strategy_yaml_now_includes_r_distribution_and_clusters():
    from smc_quant import propose_strategy_yaml
    records = []
    base = datetime(2026, 1, 1)
    for i in range(40):
        r = 2.0 if i % 5 != 0 else -1.0
        records.append({
            "entry_time": (base + timedelta(days=i)).isoformat(),
            "r_multiple": r,
            "factors": {"htf_bias_aligned": True},
            "model": "sweep_reversal",
            "market": "us",
            "mae": -0.3, "mfe": 2.2,
        })
    out = propose_strategy_yaml(trade_records=records, min_samples=20)
    assert "r_distribution" in out
    assert "clusters" in out
    assert out["r_distribution"]["sample_size"] == 40
    assert "sweep_reversal / us" in out["clusters"]["clusters"]


def test_pd_array_matrix_consolidates_all_poi_kinds_with_distance_sort():
    """§3.10: PD-array matrix lists every POI kind in distance-sorted order."""
    from smc_quant import build_pd_array_matrix
    out = build_pd_array_matrix(
        current_price=100.0,
        order_blocks=[{"direction": 1, "top": 102, "bottom": 99, "status": "unmitigated", "grade": "A"}],
        mitigation_blocks=[],
        breaker_blocks=[{"direction": -1, "top": 110, "bottom": 108}],
        fvgs=[{"direction": 1, "top": 105, "bottom": 104, "mitigated": False}],
        inverse_fvgs=[],
        balanced_price_ranges=[],
        volume_imbalances=[],
        liquidity=[{"direction": -1, "type": "BSL", "level": 115, "swept": False,
                    "equal_tag": "EQH", "liquidity_kind": "external"}],
    )
    kinds = [r["kind"] for r in out["rows"]]
    assert "order_block" in kinds and "breaker_block" in kinds
    assert "fvg" in kinds and "liquidity" in kinds
    # Distance-sorted ascending
    distances = [r["distance"] for r in out["rows"]]
    assert distances == sorted(distances)
    # Each row knows whether it's above or below price
    for r in out["rows"]:
        assert r["side"] in {"above", "below"}


def test_build_smc_analysis_exposes_pd_array_matrix():
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    matrix = result["concepts"]["pd_array_matrix"]
    assert "rows" in matrix
    assert "current_price" in matrix
    assert matrix["above_count"] + matrix["below_count"] == matrix["total"]


def test_chart_layer_c11_pd_array_panel_populated():
    """§6.1: C11 PD-array panel must carry the top-N POIs from the matrix."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    c11 = result["visualization"]["chart_layers"]["C11_pd_array_matrix"]
    assert c11["kind"] == "table_panel"
    assert "rows" in c11
    # Panel should reflect the matrix counts
    matrix = result["concepts"]["pd_array_matrix"]
    assert c11.get("current_price") == matrix["current_price"]
    assert c11.get("above_count") == matrix["above_count"]


def test_nearest_poi_proximity_flags_close_match():
    """direction match + ≤ threshold% → has_poi_within True."""
    from smc_quant import nearest_poi_proximity
    matrix = {
        "rows": [
            {"kind": "order_block", "direction": 1, "distance_pct": 0.3},
            {"kind": "fvg", "direction": -1, "distance_pct": 0.1},
        ]
    }
    # Long entry: same-direction-only filters out the bearish FVG
    out = nearest_poi_proximity(matrix, direction=1, threshold_pct=0.5)
    assert out["has_poi_within"] is True
    assert out["closest_kind"] == "order_block"
    assert out["distance_pct"] == 0.3


def test_nearest_poi_proximity_returns_false_when_too_far():
    from smc_quant import nearest_poi_proximity
    matrix = {"rows": [{"kind": "order_block", "direction": 1, "distance_pct": 2.5}]}
    out = nearest_poi_proximity(matrix, direction=1, threshold_pct=0.5)
    assert out["has_poi_within"] is False
    assert out["closest_kind"] == "order_block"


def test_nearest_poi_proximity_empty_matrix():
    from smc_quant import nearest_poi_proximity
    out = nearest_poi_proximity({}, direction=1)
    assert out["has_poi_within"] is False


def test_paper_trading_report_passes_when_thresholds_met():
    """§10.5: ≥50 paper trades + healthy expectancy → ready_for_live True."""
    from smc_quant import paper_trading_report
    bt_records = [{"r_multiple": 2.0}] * 40 + [{"r_multiple": -1.0}] * 20
    paper_journal = []
    for i in range(50):
        r = 2.0 if i % 5 != 0 else -1.0
        paper_journal.append({
            "trade_id": f"P{i}", "r_multiple": r,
            "emotional_state": "calm",
            "factors": {"htf_bias_aligned": True},
        })
    out = paper_trading_report(paper_journal, bt_records, min_paper_trades=50)
    assert out["sample_size"] == 50
    assert out["sample_ready"] is True
    assert out["ready_for_live"] is True
    assert out["expectancy"]["sample_size"] == 50


def test_paper_trading_report_blocked_when_too_few_trades():
    from smc_quant import paper_trading_report
    out = paper_trading_report([{"r_multiple": 2.0}], [{"r_multiple": 2.0}] * 20)
    assert out["sample_ready"] is False
    assert out["ready_for_live"] is False


def test_paper_trading_report_blocked_when_edge_decayed():
    """If paper expectancy is way below backtest → ready_for_live False."""
    from smc_quant import paper_trading_report
    bt = [{"r_multiple": 3.0}] * 30 + [{"r_multiple": -1.0}] * 10
    paper = [{"r_multiple": 0.1, "emotional_state": "calm"}] * 50
    out = paper_trading_report(paper, bt, min_paper_trades=20)
    assert out["edge_decay"]["status"] == "decay_detected"
    assert out["ready_for_live"] is False


def test_atr_adaptive_stop_uses_bucket_multiple():
    """§17.6: stop = entry - m*ATR for longs, with m scaling per vol bucket."""
    from smc_quant import atr_adaptive_stop
    # mid: 1.2 × ATR(1.0) = 1.2 below 100 → 98.8
    out = atr_adaptive_stop(direction=1, entry=100, atr=1.0, vol_bucket="mid")
    assert out["atr_multiple"] == 1.2
    assert out["stop"] == 98.8
    assert out["distance"] == 1.2
    # extreme: 2.5 × ATR(1.0) → 97.5
    ext = atr_adaptive_stop(direction=1, entry=100, atr=1.0, vol_bucket="extreme")
    assert ext["stop"] == 97.5


def test_atr_adaptive_stop_prefers_wider_structural_stop():
    """When structural stop > ATR stop, the structural one wins (longer leash)."""
    from smc_quant import atr_adaptive_stop
    # ATR-only would put stop at 98.8; structural sits at 98 → take 98 (farther)
    out = atr_adaptive_stop(direction=1, entry=100, atr=1.0, vol_bucket="mid",
                             structural_stop=98)
    assert out["stop"] == 98.0
    assert out["rule"] == "max(atr,structural)"


def test_atr_adaptive_stop_falls_back_when_no_direction():
    from smc_quant import atr_adaptive_stop
    out = atr_adaptive_stop(direction=0, entry=100, atr=1.0, vol_bucket="mid")
    assert out["stop"] == 100
    assert out["rule"] == "no_direction"


def test_sweep_reversal_credits_bpr_and_ifvg_overlap():
    """§3.4 + §5.1: entry inside a BPR or IFVG band gets extra confluence."""
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{
        "index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
        "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
        "displacement_confirmed": True,
    }]
    h = normalize_ohlcv(_sample_ohlcv())
    bprs = [{"top": 10, "bottom": 9, "direction_a": 1, "direction_b": -1, "mid": 9.5,
             "index_a": 5, "index_b": 6}]
    ifvgs = [{"top": 10, "bottom": 9, "direction": 1, "block_type": "inverse_fvg",
              "mitigated": False, "index": 5}]
    entries = detect_sweep_reversal_entries(
        h, judas, obs, [], {"state": "discount", "zone": "pure_discount"}, "bullish",
        balanced_price_ranges=bprs, inverse_fvgs=ifvgs,
    )
    assert entries
    f = entries[0]["factors"]
    assert f["bpr_overlap"] is True
    assert f["ifvg_overlap"] is True
    names = {x["factor"] for x in entries[0]["confluence"]["contributing_factors"]}
    assert "bpr_overlap" in names and "ifvg_overlap" in names


def test_sweep_reversal_falls_through_without_bpr_ifvg():
    """When neither BPR nor IFVG are supplied → factors flip False but entries still build."""
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{"index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
            "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
            "displacement_confirmed": True}]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_sweep_reversal_entries(
        h, judas, obs, [], {"state": "discount"}, "bullish",
    )
    assert entries
    f = entries[0]["factors"]
    assert f["bpr_overlap"] is False
    assert f["ifvg_overlap"] is False


def test_suggest_weights_seeds_bpr_and_ifvg_overlap():
    """§3.4: suggest_confluence_weights now seeds bpr_overlap + ifvg_overlap."""
    from smc_quant import suggest_confluence_weights
    out = suggest_confluence_weights({"factors": {}})
    assert out["bpr_overlap"] == 1
    assert out["ifvg_overlap"] == 1


def test_chart_layer_c10_trades_carry_dol_and_factor_count():
    """§6.1: C10 signals expose DOL kind, poi_kind, and active factor count."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    trades = result["visualization"]["chart_layers"]["C10_signals"]["trades"]
    for t in trades:
        assert "dol_required" in t
        assert "poi_kind" in t
        assert "factor_count" in t
        assert isinstance(t["factor_count"], int) and t["factor_count"] >= 0


def test_chart_layer_c13_backtest_panel_populated():
    """§6.1: C13 panel shows backtest metrics + last few trades."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    c13 = result["visualization"]["chart_layers"]["C13_backtest_replay"]
    assert c13["kind"] == "summary_panel"
    assert "metrics" in c13
    assert isinstance(c13["trades_preview"], list)
    bt = result["concepts"]["entry_models"]["backtest_replay"]
    assert c13["metrics"] == bt["metrics"]


def test_sanitize_for_json_collapses_nan_inf_to_none():
    """NaN / inf must become None so the result serialises."""
    from smc_quant import sanitize_for_json
    import math, json
    out = sanitize_for_json({
        "ok": 1.5,
        "nan": float("nan"),
        "pos_inf": float("inf"),
        "neg_inf": -float("inf"),
        "nested": [float("nan"), 2.0, {"deep": float("inf")}],
    })
    assert out["ok"] == 1.5
    assert out["nan"] is None
    assert out["pos_inf"] is None
    assert out["neg_inf"] is None
    assert out["nested"][0] is None
    assert out["nested"][2]["deep"] is None
    json.dumps(out)  # serialises cleanly


def test_sanitize_for_json_handles_pandas_timestamp_and_dataframes():
    from smc_quant import sanitize_for_json
    import json
    ts = pd.Timestamp("2026-01-01T09:30")
    df = pd.DataFrame({"a": [1, 2]})
    out = sanitize_for_json({"time": ts, "table": df})
    assert isinstance(out["time"], str) and out["time"].startswith("2026")
    json.dumps(out)


def test_sanitize_for_json_passes_through_safe_primitives():
    from smc_quant import sanitize_for_json
    assert sanitize_for_json(None) is None
    assert sanitize_for_json("ok") == "ok"
    assert sanitize_for_json(True) is True
    assert sanitize_for_json(42) == 42


def test_sweep_reversal_uses_atr_adaptive_stop_when_provided():
    """§17.6: providing atr+vol_bucket → stop is widened past structural if ATR is larger."""
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{
        "index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
        "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
        "displacement_confirmed": True,
    }]
    h = normalize_ohlcv(_sample_ohlcv())
    # With "extreme" bucket and ATR=1 → ATR distance is 2.5; structural ~ 0.7
    out = detect_sweep_reversal_entries(
        h, judas, obs, [], {"state": "discount"}, "bullish",
        atr_value=1.0, vol_bucket="extreme",
    )
    assert out
    e = out[0]
    # ATR-widened stop sits below the structural one
    assert e["stop"] <= 8.8
    assert e["stop_rule"] in {"atr_dominant", "max(atr,structural)"}


def test_sweep_reversal_falls_back_when_no_atr_provided():
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{"index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
            "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
            "displacement_confirmed": True}]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_sweep_reversal_entries(h, judas, obs, [], {"state": "discount"}, "bullish")
    assert entries[0]["stop_rule"] == "structural_only"


def test_dol_pdh_carries_already_broken_flag_when_price_pierced():
    """§3.5: PDH already broken → caller can choose to skip or weight differently."""
    from smc_quant import resolve_dol_target
    prev_levels = {"previous_high": 110, "previous_low": 90, "broken_high": True}
    out = resolve_dol_target(
        1, current_price=100, liquidity=[], prev_levels=prev_levels,
    )
    assert out is not None and out["target_kind"] == "PDH"
    assert out.get("already_broken") is True


def test_dol_already_broken_pdh_loses_to_unbroken_internal():
    """§3.5: PDH already broken (priority 1+0.5=1.5) loses to a strong-tier
    internal pool (priority 2-1=1)."""
    from smc_quant import resolve_dol_target
    prev_levels = {"previous_high": 105, "previous_low": 90, "broken_high": True}
    pools = [
        {"type": "BSL", "level": 108, "swept": False, "end_index": 5,
         "liquidity_kind": "internal", "equal_tag": "EQH", "equal_tier": "strong"},
    ]
    out = resolve_dol_target(1, current_price=100, liquidity=pools, prev_levels=prev_levels)
    # Broken PDH (1.5) > strong internal (1) → internal wins
    assert out["target_kind"] != "PDH"
    assert out["target_price"] == 108


def test_dol_intact_pdh_still_beats_strong_internal():
    """§3.5: unbroken PDH (priority 1) ties with strong internal (1) but
    takes the closer-distance candidate as tiebreak."""
    from smc_quant import resolve_dol_target
    prev_levels = {"previous_high": 103, "previous_low": 90}
    pools = [
        {"type": "BSL", "level": 108, "swept": False, "end_index": 5,
         "liquidity_kind": "internal", "equal_tag": "EQH", "equal_tier": "strong"},
    ]
    out = resolve_dol_target(1, current_price=100, liquidity=pools, prev_levels=prev_levels)
    # PDH at 103 closer than internal at 108 → PDH wins
    assert out["target_kind"] == "PDH"
    assert out["target_price"] == 103


def test_continuation_uses_atr_adaptive_when_provided():
    """§17.6 + §5.1 Model 2: ATR + bucket widens stop on extreme vol."""
    from smc_quant import detect_continuation_entries
    cfg = SMCConfig(swing_length=2, internal_swing_length=2)
    h = normalize_ohlcv(_sample_ohlcv())
    swings = detect_swings(h, cfg.swing_length)
    structure = detect_structure(h, swings, cfg)
    liquidity = detect_liquidity(h, swings, cfg)
    displacements = detect_displacement(h, cfg)
    obs = detect_order_blocks(h, structure, displacements, liquidity)
    entries_static = detect_continuation_entries(h, structure, obs, [], {"state": "discount"}, "bullish")
    entries_atr = detect_continuation_entries(h, structure, obs, [], {"state": "discount"}, "bullish",
                                              atr_value=1.0, vol_bucket="extreme")
    if entries_static and entries_atr:
        # Same entry POI → ATR-widened stop must be ≤ structural stop
        # for long direction
        assert entries_atr[0]["stop"] <= entries_static[0]["stop"] + 1e-6
        assert entries_atr[0]["stop_rule"] in {"atr_dominant", "max(atr,structural)", "atr_only"}
        assert entries_static[0]["stop_rule"] == "structural_only"


def test_all_entry_models_carry_stop_rule_field():
    """§17.6: every entry model now exposes stop_rule for downstream audit."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement",
                "unicorn", "silver_bullet", "power_of_three"):
        for e in em[key]:
            assert "stop_rule" in e
            assert e["stop_rule"] in {
                "atr_only", "atr_dominant", "structural_only",
                "max(atr,structural)", "no_direction",
            }


def test_sweep_reversal_credits_nearest_poi_within_factor():
    """§3.10 + §5.2: entry within 0.5% of a same-dir POI → nearest_poi_within True."""
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{
        "index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
        "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
        "displacement_confirmed": True,
    }]
    h = normalize_ohlcv(_sample_ohlcv())
    matrix = {
        "rows": [
            # closest same-dir POI well within 0.5%
            {"kind": "order_block", "direction": 1, "distance_pct": 0.2},
        ]
    }
    entries = detect_sweep_reversal_entries(
        h, judas, obs, [], {"state": "discount"}, "bullish",
        pd_array_matrix=matrix,
    )
    assert entries[0]["factors"]["nearest_poi_within"] is True


def test_sweep_reversal_skips_nearest_poi_factor_when_matrix_missing():
    from smc_quant import detect_sweep_reversal_entries
    judas = [{
        "judas": 1, "real_direction": 1, "fakeout_direction": -1,
        "sweep_type": "SSL", "sweep_level": 9.0,
        "sweep_index": 4, "confirm_index": 6, "confirm_time": None,
        "false_move_high": 10.5, "false_move_low": 8.8,
        "displacement_confirmed": True, "displacement_strength": "normal",
        "session_at_sweep": None, "killzone": False, "sweep_time": None,
    }]
    obs = [{"index": 5, "direction": 1, "top": 10.0, "bottom": 9.0,
            "refined_entry": 9.5, "status": "unmitigated", "grade": "B",
            "displacement_confirmed": True}]
    h = normalize_ohlcv(_sample_ohlcv())
    entries = detect_sweep_reversal_entries(
        h, judas, obs, [], {"state": "discount"}, "bullish",
    )
    assert entries[0]["factors"]["nearest_poi_within"] is False


def test_all_entry_models_credit_nearest_poi_within_factor():
    """§3.10: every entry model exposes nearest_poi_within (default False without matrix)."""
    result = build_smc_analysis(
        _sample_ohlcv(), "AAPL",
        config=SMCConfig(swing_length=2, internal_swing_length=2),
    )
    em = result["concepts"]["entry_models"]
    for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement",
                "unicorn", "silver_bullet", "power_of_three"):
        for e in em[key]:
            assert "nearest_poi_within" in e["factors"]
            assert isinstance(e["factors"]["nearest_poi_within"], bool)
