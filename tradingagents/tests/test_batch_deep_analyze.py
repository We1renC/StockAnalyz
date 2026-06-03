"""Tests for parallel batch deep analysis."""
import json
import time
from datetime import date
from unittest.mock import patch

import app


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "batch.db")
    app.init_db()
    return original


def _seed(symbols, table="watchlist"):
    conn = app.get_db()
    for s in symbols:
        if table == "watchlist":
            conn.execute("INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,'x','TWD')", (s, s))
        else:
            conn.execute(
                "INSERT INTO positions (symbol,name,category,shares,cost_price,currency,purchase_date) VALUES (?,?,'x',1,1,'TWD',?)",
                (s, s, date.today().isoformat()),
            )
        c = conn.cursor()
        app.store_price_cache(c, s, {"price": 100, "source": "t"})
    conn.commit()
    conn.close()


def test_run_deep_analysis_stores_result(tmp_path):
    original = _temp_db(tmp_path)
    try:
        _seed(["AAA"])
        with patch.object(app, "_build_context", return_value={"context": "c", "name": "AAA", "symbol": "AAA"}), \
             patch.object(app, "_build_smc_snapshot_payload", return_value={"available": True, "bias": "bullish"}), \
             patch.object(app, "run_workflow", return_value={"steps": [
                 {"role": "analyst", "output": "## 操作建議\n買進 AAA", "provider": "p", "model": "m"}]}):
            res = app._run_deep_analysis("AAA", "analyst")
        assert res["ok"] is True
        conn = app.get_db()
        row = conn.execute("SELECT symbol, sections FROM analysis_results WHERE symbol='AAA'").fetchone()
        conn.close()
        assert row is not None
        sections = json.loads(row["sections"])
        assert "analyst" in sections
        assert sections["smc"]["bias"] == "bullish"
    finally:
        app.DB = original


def test_run_deep_analysis_handles_context_error(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "_build_context", return_value={"error": "no cache"}):
            res = app._run_deep_analysis("ZZZ", "analyst")
        assert res["ok"] is False
        assert "no cache" in res["error"]
    finally:
        app.DB = original


def test_batch_deep_analyze_runs_parallel(tmp_path):
    original = _temp_db(tmp_path)
    try:
        _seed(["AAA", "BBB", "CCC", "DDD"])

        def slow_wf(ctx, mode="analyst"):
            time.sleep(0.5)
            return {"steps": [{"role": "analyst", "output": "## 建議\n買", "provider": "p", "model": "m"}]}

        with patch.object(app, "_build_context", side_effect=lambda s: {"context": "c", "name": s, "symbol": s}), \
             patch.object(app, "run_workflow", side_effect=slow_wf):
            t0 = time.time()
            resp = app.api_batch_deep_analyze(mode="analyst", max_workers=4, scope="watchlist")
            # drain the async body iterator
            import asyncio
            chunks = []
            async def drain():
                async for c in resp.body_iterator:
                    chunks.append(c)
            asyncio.new_event_loop().run_until_complete(drain())
            elapsed = time.time() - t0
        # 4 × 0.5s sequential would be ~2s; parallel(4) should be < 1s
        assert elapsed < 1.2, f"not parallel: {elapsed:.2f}s"
        # final done event present
        joined = "".join(chunks)
        assert '"status": "done"' in joined
        assert "完成 4/4" in joined
    finally:
        app.DB = original


def test_batch_deep_analyze_max_workers_capped(tmp_path):
    original = _temp_db(tmp_path)
    try:
        _seed(["AAA"])
        with patch.object(app, "_build_context", return_value={"context": "c", "name": "AAA", "symbol": "AAA"}), \
             patch.object(app, "run_workflow", return_value={"steps": [
                 {"role": "analyst", "output": "x", "provider": "p", "model": "m"}]}):
            # request 100 workers; should cap to 4 internally and still work
            resp = app.api_batch_deep_analyze(mode="analyst", max_workers=100, scope="watchlist")
            import asyncio
            chunks = []
            async def drain():
                async for c in resp.body_iterator:
                    chunks.append(c)
            asyncio.new_event_loop().run_until_complete(drain())
        assert "完成 1/1" in "".join(chunks)
    finally:
        app.DB = original
