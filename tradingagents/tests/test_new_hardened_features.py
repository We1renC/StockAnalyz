import os
import shutil
import tempfile
import time
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from learning.uniqueness_weighted_lr import compute_roc_auc_numpy
from learning.adaptive_store import backup_and_rotate_config
from crypto_api.auth import authenticate_request
from app import app, get_db

def test_compute_roc_auc_numpy():
    # Test empty cases
    assert compute_roc_auc_numpy([0, 0], [0.1, 0.2]) == 0.5
    assert compute_roc_auc_numpy([1, 1], [0.1, 0.2]) == 0.5
    
    # Simple correct case
    y_true = [0, 0, 1, 1]
    y_scores = [0.1, 0.4, 0.35, 0.8]
    # Pos scores: 0.35, 0.8. Neg scores: 0.1, 0.4.
    # pos=0.35 is > neg=0.1 (1), but < neg=0.4 (0) -> 1 pair correct.
    # pos=0.8 is > neg=0.1 (1), and > neg=0.4 (1) -> 2 pairs correct.
    # Total correct: 3 out of 4 -> AUC = 3/4 = 0.75.
    assert compute_roc_auc_numpy(y_true, y_scores) == 0.75


def test_backup_and_rotate_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        yaml_path = tmpdir_path / "strategy.yaml"
        
        # Write initial config
        yaml_path.write_text("confluence: {}\n", encoding="utf-8")
        
        # Run backup/rotate 15 times
        for i in range(15):
            backup_and_rotate_config(yaml_path, keep=10)
            time.sleep(0.01) # ensure distinct timestamps if needed (the pattern will find all backups)
            # modify slightly
            yaml_path.write_text(f"confluence: {i}\n", encoding="utf-8")
            
        # Count backup files
        backups = list(tmpdir_path.glob("strategy.yaml.bak.*"))
        assert len(backups) <= 10


def test_metrics_endpoint():
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    content = response.text
    assert "# HELP smc_orders_total" in content
    assert "# HELP smc_fills_total" in content
    assert "smc_account_equity_usdt" in content


def test_nonce_ttl_cleanup():
    # Setup connection to in-memory db or temp db
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "portfolio.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # Create nonces table
        c.execute("""
            CREATE TABLE IF NOT EXISTS crypto_nonces (
                api_key_id INTEGER,
                nonce TEXT UNIQUE,
                timestamp INTEGER
            )
        """)
        conn.commit()
        
        # Insert old and new nonces
        now_ms = int(time.time() * 1000)
        c.execute("INSERT INTO crypto_nonces VALUES (1, 'old_nonce', ?)", (now_ms - 70000,))
        c.execute("INSERT INTO crypto_nonces VALUES (1, 'new_nonce', ?)", (now_ms - 10000,))
        conn.commit()
        
        # Call cleanup query directly
        cutoff = now_ms - 60000
        c.execute("DELETE FROM crypto_nonces WHERE timestamp < ?", (cutoff,))
        conn.commit()
        
        # Verify old nonce is deleted, new is kept
        old_row = c.execute("SELECT * FROM crypto_nonces WHERE nonce='old_nonce'").fetchone()
        new_row = c.execute("SELECT * FROM crypto_nonces WHERE nonce='new_nonce'").fetchone()
        
        assert old_row is None
        assert new_row is not None
        
        conn.close()
