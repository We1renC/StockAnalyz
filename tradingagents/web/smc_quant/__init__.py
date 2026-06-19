from __future__ import annotations

# Import the sub-modules
from . import config, utils, structure, engine

# Dynamically re-export everything from the sub-modules to this module's namespace
# (including private underscore variables/functions for tests/monkeypatching backward compatibility)
import sys
this_module = sys.modules[__name__]

for sub_module in (config, utils, structure, engine):
    for name in dir(sub_module):
        if not name.startswith("__"):
            setattr(this_module, name, getattr(sub_module, name))

# Explicitly import and re-export the private helper functions used in tests for robustness
from .engine import (
    _builtin_detect_cme_gaps,
    _entry_bar_of,
    _merge_detector_extras,
    _sanitize_weight_overrides,
    _silver_bullet_window_minutes,
    _suggest_crypto_weights,
)

# Also explicitly import and re-export the items from smc_ledger_io
from smc_ledger_io import (
    TRADE_LEDGER_SCHEMA_VERSION,
    TRADE_RECORD_SCHEMA_VERSION,
    LedgerPaths,
    connect_db,
    persist_trade_records,
    load_trade_records,
    load_cached_trade_records,
    read_trade_ledger,
)

