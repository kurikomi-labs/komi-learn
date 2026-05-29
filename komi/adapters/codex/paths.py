"""Resolve komi-learn's on-disk locations for the Codex CLI host.

Same shape as the Claude Code adapter's paths, but rooted at Codex's config dir
(``$CODEX_HOME`` or ``~/.codex``). Exposes the exact surface hooklib needs:
personal_root / index_path / project_root / queue_dir / state_path / update_state
/ pool_config — so the shared hook logic works unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".codex"


def personal_root() -> Path:
    return codex_home() / "komi"


def project_root(cwd: str) -> Optional[Path]:
    if not cwd:
        return None
    p = Path(cwd).expanduser()
    if not p.exists():
        return None
    return p / ".codex" / "komi"


def index_path() -> Path:
    return personal_root() / "index.db"


def queue_dir() -> Path:
    return personal_root() / "queue"


def keys_dir() -> Path:
    return personal_root() / "keys"


def state_path() -> Path:
    return personal_root() / "state.json"


def hooks_path() -> Path:
    """Codex registers hooks in ~/.codex/hooks.json (same schema as Claude Code)."""
    return codex_home() / "hooks.json"


def update_state(mutator):
    """Atomic+locked state update, rooted at THIS host's state.json. We bind the
    Claude Code helper to our own state_path via a tiny shim so the lock file and
    the data file are both under ~/.codex/komi."""
    return _update_state_for(state_path(), mutator)


def pool_config() -> Optional[dict]:
    try:
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return None
        return {"repo_url": cfg.pool_repo_url, "cache_dir": cfg.pool_cache_dir,
                "branch": cfg.pool_branch, "require_signature": cfg.pool_require_signature}
    except Exception:
        return None


# ── state update bound to an explicit path (host-neutral core) ──────────────

def _update_state_for(sp: Path, mutator):
    """Same algorithm as claude_code.paths.update_state, but for an explicit path
    so each host's state lives under its own config dir."""
    import json
    import tempfile
    from ..claude_code.paths import _lock_file, _unlock_file
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
                state = {}
        if not isinstance(state, dict):
            state = {}
        result = mutator(state)
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


__all__ = [
    "codex_home", "personal_root", "project_root", "index_path", "queue_dir",
    "keys_dir", "state_path", "hooks_path", "update_state", "pool_config",
]
