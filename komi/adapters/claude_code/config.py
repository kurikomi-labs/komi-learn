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
from dataclasses import dataclass

from . import paths
from .. import config_schema


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
    pool_min_corroboration: int = 1       # only pull pool learnings ≥ N distinct signers (Phase 5b)
    pool_sync_hours: float = 12.0         # background sync cadence
    pool_auto_contribute: bool = False    # if True, approved globals auto-open PRs; else stay in queue
    pool_github_user: str = ""            # your GitHub username — bound into signatures (Phase 7 Sybil)

    @property
    def pool_enabled(self) -> bool:
        return bool(self.pool_repo_url)

    @property
    def pool_cache_dir(self) -> str:
        return str(paths.personal_root() / "pool" / "repo")


def load() -> Config:
    cfg = Config()
    # file (shared schema: which json keys map onto which Config attrs)
    cpath = paths.personal_root() / "config.json"
    if cpath.exists():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            config_schema.apply_file(cfg, data)
        except json.JSONDecodeError as e:
            # Don't silently run on defaults — a corrupt config that quietly
            # disables the pool is exactly the kind of "looks fine but isn't" trap
            # the review flagged. Surface it; doctor reports it as a failure too.
            import sys
            sys.stderr.write(f"komi-learn: config.json is invalid JSON, using defaults: {e}\n")
        except Exception:
            pass
    # env overrides (shared KOMI_* map)
    config_schema.apply_env(cfg)
    # Loud warning: accepting unsigned pool entries is only safe for a private/test
    # pool. For a public pool it lets anyone inject unsigned learnings.
    if cfg.pool_enabled and not cfg.pool_require_signature:
        import sys
        sys.stderr.write(
            "komi-learn WARNING: pool.require_signature is False — unsigned learnings "
            "will be accepted. Only safe for a private/test pool.\n"
        )
    return cfg


__all__ = ["Config", "load"]
