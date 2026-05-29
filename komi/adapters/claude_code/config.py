"""komi-learn — Claude Code adapter configuration.

Config is read from (lowest → highest precedence):
  1. built-in defaults
  2. ``~/.claude/komi/config.json``
  3. environment variables (KOMI_*)

The pool is OFF until you set ``pool.repo_url`` (or KOMI_POOL_REPO_URL) to the
GitHub repo you create. Until then the engine runs personal-only — no global
sync, no contributions leave the device.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class Config:
    # learning loop
    nudge_turns: int = 8
    distill_model: str = "claude-haiku-4-5-20251001"
    recall_k: int = 8

    # global pool (GitHub-backed). Empty repo_url => pool disabled.
    pool_repo_url: str = ""
    pool_mode: str = "pr"                 # "pr" (open PRs) | "local" (commit directly)
    pool_branch: str = "main"
    pool_require_signature: bool = True
    pool_sync_hours: float = 12.0         # background sync cadence
    pool_auto_contribute: bool = False    # if True, approved globals auto-open PRs; else stay in queue

    @property
    def pool_enabled(self) -> bool:
        return bool(self.pool_repo_url)

    @property
    def pool_cache_dir(self) -> str:
        return str(paths.personal_root() / "pool" / "repo")


def _coerce(value: str, like: Any) -> Any:
    if isinstance(like, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(like, int):
        try: return int(value)
        except ValueError: return like
    if isinstance(like, float):
        try: return float(value)
        except ValueError: return like
    return value


_ENV = {
    "KOMI_NUDGE_TURNS": "nudge_turns",
    "KOMI_DISTILL_MODEL": "distill_model",
    "KOMI_RECALL_K": "recall_k",
    "KOMI_POOL_REPO_URL": "pool_repo_url",
    "KOMI_POOL_MODE": "pool_mode",
    "KOMI_POOL_BRANCH": "pool_branch",
    "KOMI_POOL_REQUIRE_SIGNATURE": "pool_require_signature",
    "KOMI_POOL_SYNC_HOURS": "pool_sync_hours",
    "KOMI_POOL_AUTO_CONTRIBUTE": "pool_auto_contribute",
}


def load() -> Config:
    cfg = Config()
    # file
    cpath = paths.personal_root() / "config.json"
    if cpath.exists():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            pool = data.get("pool", {})
            for k, v in {
                "nudge_turns": data.get("nudge_turns"),
                "distill_model": data.get("distill_model"),
                "recall_k": data.get("recall_k"),
                "pool_repo_url": pool.get("repo_url"),
                "pool_mode": pool.get("mode"),
                "pool_branch": pool.get("branch"),
                "pool_require_signature": pool.get("require_signature"),
                "pool_sync_hours": pool.get("sync_hours"),
                "pool_auto_contribute": pool.get("auto_contribute"),
            }.items():
                if v is not None:
                    setattr(cfg, k, v)
        except Exception:
            pass
    # env overrides
    for env_key, attr in _ENV.items():
        if env_key in os.environ:
            setattr(cfg, attr, _coerce(os.environ[env_key], getattr(cfg, attr)))
    return cfg


__all__ = ["Config", "load"]
