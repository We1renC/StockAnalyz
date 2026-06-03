"""Tests for LLM workflow prompt composition."""
from unittest.mock import patch

import llm_providers


def test_run_workflow_analyst_prompt_includes_smc_context():
    captured = {}

    def fake_call_llm(provider, model, prompt, api_key, mode="api"):
        captured["provider"] = provider
        captured["model"] = model
        captured["prompt"] = prompt
        captured["mode"] = mode
        return "ok"

    fake_settings = {
        "api_keys": {"openai": "", "anthropic": ""},
        "roles": {
            "analyst": {"provider": "openai", "model": "gpt-5.5", "mode": "cli"},
            "reviewer": {"provider": "anthropic", "model": "opus", "mode": "cli"},
        },
    }

    with patch.object(llm_providers, "load_settings", return_value=fake_settings), \
         patch.object(llm_providers, "call_llm", side_effect=fake_call_llm):
        res = llm_providers.run_workflow("【SMC 結構與回測】mock", mode="analyst")

    assert res["steps"][0]["output"] == "ok"
    assert "SMC 結構與回測摘要" in captured["prompt"]
    assert "財報 × 估值 × 17D 技術 × SMC" in captured["prompt"]
    assert "SMC POI" in captured["prompt"]
    assert "DOL" in captured["prompt"]
