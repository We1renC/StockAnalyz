"""Tests for acceptance security hygiene scan."""

from pathlib import Path

from paper_acceptance_security import run_security_scan


def test_security_scan_detects_env_usage_and_hardcoded_secret(tmp_path: Path):
    (tmp_path / "app.py").write_text("import os\nkey = os.getenv('OPENAI_API_KEY')\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("api_key = 'sk-testsecret12345678901234567890'\n", encoding="utf-8")
    (tmp_path / "rotation_playbook.md").write_text("# rotation\n", encoding="utf-8")

    payload = run_security_scan(tmp_path)

    assert payload["scanned_files"] >= 3
    assert payload["env_usage_hits"] >= 1
    assert payload["hardcoded_secret_count"] == 1
    assert payload["revocation_process"] is True


def test_security_scan_passes_clean_tree(tmp_path: Path):
    (tmp_path / "worker.py").write_text("import os\nclient = os.getenv('API_KEY')\n", encoding="utf-8")
    payload = run_security_scan(tmp_path)
    assert payload["no_hardcoded_keys"] is True
    assert payload["env_only"] is True
