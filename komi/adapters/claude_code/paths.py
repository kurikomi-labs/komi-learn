"""Resolve komi-learn's on-disk locations for the Claude Code host.

Personal scope lives under ``~/.claude/komi``; project scope under
``<cwd>/.claude/komi`` so it can be committed and shared with a team. A single
shared ``index.db`` (the "one brain") lives at the personal root and records each
row's own scope. Mirrors docs/02-architecture.md §3.2.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def claude_home() -> Path:
    # Honor an explicit override (tests, alt installs), else ~/.claude
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".claude"
    return base


def personal_root() -> Path:
    return claude_home() / "komi"


def project_root(cwd: str) -> Optional[Path]:
    if not cwd:
        return None
    p = Path(cwd).expanduser()
    if not p.exists():
        return None
    return p / ".claude" / "komi"


def index_path() -> Path:
    return personal_root() / "index.db"


def queue_dir() -> Path:
    return personal_root() / "queue"


def outbox_dir() -> Path:
    return personal_root() / "pool" / "outbox"


def inbox_dir() -> Path:
    return personal_root() / "pool" / "inbox"


def keys_dir() -> Path:
    return personal_root() / "keys"


def state_path() -> Path:
    """Small JSON for cadence bookkeeping (turns since last distill, last curate)."""
    return personal_root() / "state.json"


__all__ = [
    "claude_home", "personal_root", "project_root", "index_path",
    "queue_dir", "outbox_dir", "inbox_dir", "keys_dir", "state_path",
]
