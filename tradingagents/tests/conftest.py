"""pytest fixtures shared across tests.

Adds web/ to sys.path so we can `import app` and `import llm_providers` directly.
"""
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))
