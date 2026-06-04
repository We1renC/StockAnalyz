"""Security hygiene scan for paper acceptance promotion checks."""

from __future__ import annotations

import re
from pathlib import Path


DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}

DEFAULT_EXCLUDE_FILES = {
    "portfolio.db",
}

TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".env.example",
    ".md",
}

HARDCODED_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"api[_-]?key\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.IGNORECASE),
    re.compile(r"secret\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.IGNORECASE),
]

ENV_USAGE_PATTERNS = [
    re.compile(r"os\.getenv\(", re.IGNORECASE),
    re.compile(r"process\.env\.", re.IGNORECASE),
    re.compile(r"dotenv", re.IGNORECASE),
]

SEPARATION_PATTERNS = [
    re.compile(r"testnet", re.IGNORECASE),
    re.compile(r"paper", re.IGNORECASE),
    re.compile(r"live", re.IGNORECASE),
]

REVOCATION_HINTS = [
    "revocation",
    "rotation",
    "incident",
    "credential",
]


def _is_text_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in TEXT_SUFFIXES or path.name.lower().endswith(".env.example")


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if any(part in DEFAULT_EXCLUDE_DIRS for part in path.parts):
            continue
        if path.is_dir():
            continue
        if path.name in DEFAULT_EXCLUDE_FILES:
            continue
        if any(path.name.endswith(ext) for ext in (".pem", ".key")):
            continue
        if not _is_text_file(path):
            continue
        yield path


def run_security_scan(root: Path) -> dict:
    """Best-effort source scan without reading known sensitive data files."""

    hardcoded_hits: list[dict] = []
    env_usage_hits = 0
    separation_hits = 0
    scanned_files = 0
    docs_found: list[str] = []

    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        scanned_files += 1
        if any(pattern.search(text) for pattern in ENV_USAGE_PATTERNS):
            env_usage_hits += 1
        if any(pattern.search(text) for pattern in SEPARATION_PATTERNS):
            separation_hits += 1
        for pattern in HARDCODED_SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                hardcoded_hits.append({
                    "file": str(path.relative_to(root)),
                    "snippet": match.group(0)[:60],
                })
                break
        lower_name = path.name.lower()
        if any(hint in lower_name for hint in REVOCATION_HINTS):
            docs_found.append(str(path.relative_to(root)))

    return {
        "scanned_files": scanned_files,
        "hardcoded_secret_hits": hardcoded_hits,
        "hardcoded_secret_count": len(hardcoded_hits),
        "env_usage_hits": env_usage_hits,
        "separation_hits": separation_hits,
        "revocation_docs": docs_found,
        "no_hardcoded_keys": len(hardcoded_hits) == 0,
        "env_only": env_usage_hits > 0 and len(hardcoded_hits) == 0,
        "test_live_separation": separation_hits > 0,
        "revocation_process": bool(docs_found),
    }


__all__ = ["run_security_scan"]
