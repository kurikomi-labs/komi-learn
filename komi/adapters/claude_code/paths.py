"""Resolve komi-learn's on-disk locations for the Claude Code host.

Personal scope lives under ``~/.claude/komi``; project scope under
``<cwd>/.claude/komi`` so it can be committed and shared with a team. A single
shared ``index.db`` (the "one brain") lives at the personal root and records each
row's own scope.
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


def pool_config() -> Optional[dict]:
    """PoolConfig kwargs for hooklib's pool mirror, or None if the pool is off.
    Keeps hooklib host-neutral — it just asks each host's paths for this."""
    try:
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return None
        return {"repo_url": cfg.pool_repo_url, "cache_dir": cfg.pool_cache_dir,
                "branch": cfg.pool_branch, "require_signature": cfg.pool_require_signature,
                "min_corroboration": cfg.pool_min_corroboration}
    except Exception:
        return None


def state_path() -> Path:
    """Small JSON for cadence bookkeeping (turns since last distill, last curate)."""
    return personal_root() / "state.json"


def update_state(mutator):
    """Atomically read-modify-write state.json under an exclusive cross-process lock.

    Several Claude Code sessions run hooks concurrently and all touch this file
    (distill turn counter, pool-sync clock, curate clock). Without locking + atomic
    writes, concurrent updates clobber each other or truncate the file. ``mutator``
    receives the current state dict (possibly {}) and mutates it in place; its
    return value (if not None) is taken as the result to hand back to the caller.

    Returns whatever the mutator returns (e.g. a bool "should I fire?"). Best-effort:
    on any I/O/lock error it still runs the mutator on a fresh dict so callers never
    crash — the worst case is a missed/duplicated cadence tick, never a broken hook.
    """
    import json
    import tempfile

    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    lock = sp.with_suffix(".lock")
    fh = None
    try:
        fh = open(lock, "a+")
        _lock_file(fh)
        state = {}
        if sp.exists():
            try:
                state = json.loads(sp.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                state = {}            # tolerate a corrupt/partial file
        if not isinstance(state, dict):
            state = {}
        result = mutator(state)
        # atomic write
        fd, tmp = tempfile.mkstemp(dir=str(sp.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state))
        os.replace(tmp, sp)
        return result
    except Exception:
        try:
            return mutator({})
        except Exception:
            return None
    finally:
        if fh is not None:
            try:
                _unlock_file(fh)
            finally:
                fh.close()


def _lock_file(fh) -> None:
    """Acquire an exclusive advisory lock (blocking). No-op if locking is unavailable."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        pass


def _unlock_file(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


__all__ = [
    "claude_home", "personal_root", "project_root", "index_path",
    "queue_dir", "outbox_dir", "inbox_dir", "keys_dir", "state_path", "update_state",
]
