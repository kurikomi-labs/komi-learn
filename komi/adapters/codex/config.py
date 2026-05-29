"""komi-learn — Codex adapter configuration (reads ~/.codex/komi/config.json).

Same config shape as the Claude Code adapter; only the root differs (Codex's
config dir). Kept separate so the two hosts can be configured independently.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import paths


@dataclass
class Config:
    nudge_turns: int = 8
    distill_model: str = "gpt-5-mini"
    recall_k: int = 8
    pool_repo_url: str = ""
    pool_mode: str = "pr"
    pool_branch: str = "main"
    pool_require_signature: bool = True
    pool_sync_hours: float = 12.0
    pool_auto_contribute: bool = False

    @property
    def pool_enabled(self) -> bool:
        return bool(self.pool_repo_url)

    @property
    def pool_cache_dir(self) -> str:
        return str(paths.personal_root() / "pool" / "repo")


def load() -> Config:
    cfg = Config()
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
        except json.JSONDecodeError as e:
            import sys
            sys.stderr.write(f"komi-learn: ~/.codex/komi/config.json is invalid JSON: {e}\n")
        except Exception:
            pass
    for env_key, attr in {
        "KOMI_NUDGE_TURNS": "nudge_turns",
        "KOMI_POOL_REPO_URL": "pool_repo_url",
        "KOMI_POOL_REQUIRE_SIGNATURE": "pool_require_signature",
    }.items():
        if env_key in os.environ:
            v = os.environ[env_key]
            cur = getattr(cfg, attr)
            if isinstance(cur, bool):
                setattr(cfg, attr, v.strip().lower() in {"1", "true", "yes", "on"})
            elif isinstance(cur, int):
                try: setattr(cfg, attr, int(v))
                except ValueError: pass
            else:
                setattr(cfg, attr, v)
    return cfg


__all__ = ["Config", "load"]
