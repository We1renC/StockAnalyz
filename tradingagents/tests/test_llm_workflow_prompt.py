"""Tests for LLM workflow prompt composition."""
from unittest.mock import patch

import llm_providers


def test_prompt_builders_include_smc_and_reviewer_sections():
    smc_prompt = llm_providers.build_smc_structure_analyst_prompt("CTX")
    analyst_prompt = llm_providers.build_analyst_prompt("CTX", smc_report="SMC_REPORT")
    reviewer_prompt = llm_providers.build_reviewer_prompt("CTX", "REPORT", smc_report="SMC_REPORT")

    assert "你是 SMC 結構分析師" in smc_prompt
    assert "一句話結論" in smc_prompt
    assert "SMC 結構與回測摘要" in analyst_prompt
    assert "[SMC 結構分析師判讀]\nSMC_REPORT" in analyst_prompt
    assert "財報 × 估值 × 17D 技術 × SMC" in analyst_prompt
    assert "SMC POI" in analyst_prompt
    assert "DOL" in analyst_prompt

    assert "[原始數據]\nCTX" in reviewer_prompt
    assert "[SMC 結構分析師判讀]\nSMC_REPORT" in reviewer_prompt
    assert "[分析師報告]\nREPORT" in reviewer_prompt
    assert "修正後的建議" in reviewer_prompt


def test_run_workflow_analyst_prompt_includes_smc_context():
    captured = []

    def fake_call_llm(provider, model, prompt, api_key, mode="api"):
        captured.append({
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "mode": mode,
        })
        if "你是 SMC 結構分析師" in prompt:
            return "SMC 結構摘要"
        return "ok"

    fake_settings = {
        "api_keys": {"openai": "", "anthropic": "", "google": ""},
        "roles": {
            "smc_structure_analyst": {"provider": "openai", "model": "gpt-5.5", "mode": "cli"},
            "analyst": {"provider": "openai", "model": "gpt-5.5", "mode": "cli"},
            "reviewer": {"provider": "anthropic", "model": "opus", "mode": "cli"},
        },
    }

    with patch.object(llm_providers, "load_settings", return_value=fake_settings), \
         patch.object(llm_providers, "call_llm", side_effect=fake_call_llm):
        res = llm_providers.run_workflow("【SMC 結構與回測】mock", mode="analyst")

    assert [step["role"] for step in res["steps"]] == ["smc_structure_analyst", "analyst"]
    assert res["steps"][0]["output"] == "SMC 結構摘要"
    analyst_prompt = captured[1]["prompt"]
    assert "SMC 結構與回測摘要" in analyst_prompt
    assert "財報 × 估值 × 17D 技術 × SMC" in analyst_prompt
    assert "SMC POI" in analyst_prompt
    assert "DOL" in analyst_prompt
    assert "[SMC 結構分析師判讀]\nSMC 結構摘要" in analyst_prompt


def test_run_workflow_both_passes_smc_output_to_reviewer():
    captured = []

    def fake_call_llm(provider, model, prompt, api_key, mode="api"):
        captured.append(prompt)
        if "你是 SMC 結構分析師" in prompt:
            return "SMC 結構摘要"
        if "你是專業金融分析師" in prompt:
            return "分析師摘要"
        return "審查員摘要"

    fake_settings = {
        "api_keys": {"openai": "", "anthropic": "", "google": ""},
        "roles": {
            "smc_structure_analyst": {"provider": "openai", "model": "gpt-5.5", "mode": "cli"},
            "analyst": {"provider": "openai", "model": "gpt-5.5", "mode": "cli"},
            "reviewer": {"provider": "anthropic", "model": "opus", "mode": "cli"},
        },
    }

    with patch.object(llm_providers, "load_settings", return_value=fake_settings), \
         patch.object(llm_providers, "call_llm", side_effect=fake_call_llm):
        res = llm_providers.run_workflow("CTX", mode="both")

    assert [step["role"] for step in res["steps"]] == ["smc_structure_analyst", "analyst", "reviewer"]
    assert "[SMC 結構分析師判讀]\nSMC 結構摘要" in captured[1]
    assert "[SMC 結構分析師判讀]\nSMC 結構摘要" in captured[2]
    assert "[分析師報告]\n分析師摘要" in captured[2]
