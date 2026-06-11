"""pytest fixtures shared across tests.

Adds web/ to sys.path so we can `import app` and `import llm_providers` directly.
"""
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))

# Phase-1 token rollout: the production settings.json holds a real
# dashboard_api_token. SMC_TEST_MODE makes api_auth skip the settings
# fallback so TestClient-based tests aren't all 401; tests that
# explicitly setenv DASHBOARD_API_TOKEN still exercise enforcement.
import os
os.environ.setdefault("SMC_TEST_MODE", "1")


import pytest


@pytest.fixture(autouse=True)
def _isolate_confluence_weights():
    """D8: snapshot/restore the global confluence weights around EVERY test.

    The learning loop legitimately mutates config/strategy.yaml (soft
    adoption) and apply_strategy_yaml_overrides() loads it into the
    module globals. Without isolation, any test that triggers an
    overrides load leaks LEARNED weights into later tests that assert
    DEFAULT weight behaviour — an ordering-dependent failure class we
    hit twice. This fixture makes every test see pristine globals.
    """
    try:
        import smc_quant
    except Exception:
        yield
        return
    saved_w = dict(smc_quant.CONFLUENCE_WEIGHTS_DEFAULT)
    saved_c = dict(smc_quant.CRYPTO_CONFLUENCE_WEIGHTS_DEFAULT)
    saved_t = smc_quant.CONFLUENCE_THRESHOLD_DEFAULT
    try:
        yield
    finally:
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.clear()
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.update(saved_w)
        smc_quant.CRYPTO_CONFLUENCE_WEIGHTS_DEFAULT.clear()
        smc_quant.CRYPTO_CONFLUENCE_WEIGHTS_DEFAULT.update(saved_c)
        smc_quant.CONFLUENCE_THRESHOLD_DEFAULT = saved_t
