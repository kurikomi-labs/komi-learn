"""komi-learn — Codex adapter configuration (reads ~/.codex/komi/config.json).

Same config shape as the Claude Code adapter; only the root differs (Codex's
config dir). Kept separate so the two hosts can be configured independently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import paths
from .. import config_schema


@dataclass
class Config:
    nudge_turns: int = 8
    distill_model: str = "gpt-5-mini"
    recall_k: int = 8
    pool_repo_url: str = ""
    pool_mode: str = "pr"
    pool_branch: str = "main"
    pool_require_signature: bool = True
    pool_min_corroboration: int = 1
    pool_sync_hours: float = 12.0
    pool_auto_contribute: bool = False
    pool_github_user: str = ""

    @property
    def pool_enabled(self) -> bool:
        return bool(self.pool_repo_url)

    @property
    def pool_cache_dir(self) -> str:
        return str(paths.personal_root() / "pool" / "repo")


def load() -> Config:
    cfg = Config()
    # Shared config schema (komi.adapters.config_schema) — same file-key + KOMI_* env
    # surface as the Claude Code adapter, so a new pool key can't be honored on one
    # host and silently dropped on the other (which previously happened: Codex used
    # to ignore 6 of the 10 env vars). Only the *defaults* above are host-specific.
    cpath = paths.personal_root() / "config.json"
    if cpath.exists():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            config_schema.apply_file(cfg, data)
        except json.JSONDecodeError as e:
            import sys
            sys.stderr.write(f"komi-learn: ~/.codex/komi/config.json is invalid JSON: {e}\n")
        except Exception:
            pass
    config_schema.apply_env(cfg)
    return cfg


__all__ = ["Config", "load"]
