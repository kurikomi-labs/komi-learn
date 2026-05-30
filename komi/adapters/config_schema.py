"""komi-learn — the shared config schema for every host adapter.

Both the Claude Code and Codex adapters store the SAME pool/learning settings in
``<host root>/komi/config.json`` and accept the SAME ``KOMI_*`` environment
overrides — only their *defaults* (model name, config root) genuinely differ per
host. Previously each adapter hand-copied the field list, the env-var map, and the
file-key map into its own ``config.py``. That duplication drifted: the Codex
adapter silently honored only 4 of the 10 env vars Claude Code did (a contributor
review caught it after a new pool key was added to one host but not the other).

This module is the single source of truth for the SHARED surface:
  • ``ENV_MAP``       — KOMI_* env var → Config attribute name
  • ``FILE_KEYS``     — how nested config.json keys map onto flat Config attributes
Each adapter keeps only its host-specific defaults in its own ``Config`` dataclass
and calls :func:`apply_file` + :func:`apply_env` to populate it. A parity test
(tests/test_config_parity.py) asserts both adapters expose this whole surface, so a
future key can't be added to one host and forgotten on the other.
"""

from __future__ import annotations

import os
from typing import Any

# KOMI_* environment variable → flat Config attribute. Shared by all hosts.
ENV_MAP: dict[str, str] = {
    "KOMI_NUDGE_TURNS": "nudge_turns",
    "KOMI_DISTILL_MODEL": "distill_model",
    "KOMI_RECALL_K": "recall_k",
    "KOMI_POOL_REPO_URL": "pool_repo_url",
    "KOMI_POOL_MODE": "pool_mode",
    "KOMI_POOL_BRANCH": "pool_branch",
    "KOMI_POOL_REQUIRE_SIGNATURE": "pool_require_signature",
    "KOMI_POOL_MIN_CORROBORATION": "pool_min_corroboration",
    "KOMI_POOL_SYNC_HOURS": "pool_sync_hours",
    "KOMI_POOL_AUTO_CONTRIBUTE": "pool_auto_contribute",
    "KOMI_POOL_GITHUB_USER": "pool_github_user",
}

# config.json layout → flat Config attribute. Top-level keys + the nested pool.* block.
# Each entry: attribute -> (json path tuple).
FILE_KEYS: dict[str, tuple] = {
    "nudge_turns": ("nudge_turns",),
    "distill_model": ("distill_model",),
    "recall_k": ("recall_k",),
    "pool_repo_url": ("pool", "repo_url"),
    "pool_mode": ("pool", "mode"),
    "pool_branch": ("pool", "branch"),
    "pool_require_signature": ("pool", "require_signature"),
    "pool_min_corroboration": ("pool", "min_corroboration"),
    "pool_sync_hours": ("pool", "sync_hours"),
    "pool_auto_contribute": ("pool", "auto_contribute"),
    "pool_github_user": ("pool", "github_user"),
}


def _coerce(value: str, like: Any) -> Any:
    """Coerce a string (env var) to the type of the current attribute value."""
    if isinstance(like, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(like, int):
        try:
            return int(value)
        except ValueError:
            return like
    if isinstance(like, float):
        try:
            return float(value)
        except ValueError:
            return like
    return value


def apply_file(cfg: Any, data: dict) -> None:
    """Populate a Config dataclass from a parsed config.json dict, per FILE_KEYS.
    Only sets an attribute when the key is present (so defaults survive)."""
    for attr, path in FILE_KEYS.items():
        node: Any = data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is not None and hasattr(cfg, attr):
            setattr(cfg, attr, node)


def apply_env(cfg: Any, environ: dict | None = None) -> None:
    """Apply KOMI_* environment overrides to a Config dataclass, per ENV_MAP."""
    env = environ if environ is not None else os.environ
    for env_key, attr in ENV_MAP.items():
        if env_key in env and hasattr(cfg, attr):
            setattr(cfg, attr, _coerce(env[env_key], getattr(cfg, attr)))


__all__ = ["ENV_MAP", "FILE_KEYS", "apply_file", "apply_env"]
