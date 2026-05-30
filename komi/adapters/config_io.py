"""komi-learn — read/write the per-host config.json (shared by wizard + config menu).

Both adapters store config as ``<host root>/komi/config.json`` with the same shape
(see each host's config.py). This module centralizes safe read/merge/atomic-write
and the dotted-key get/set used by `komi-learn config set <key> <value>`, so the
install wizard and the config menu touch config the same way.

A host paths module (claude_code.paths or codex.paths) is passed in; we only need
its ``personal_root()``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def config_path(paths_mod) -> Path:
    return paths_mod.personal_root() / "config.json"


def load_raw(paths_mod) -> dict:
    p = config_path(paths_mod)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_raw(paths_mod, data: dict) -> bool:
    p = config_path(paths_mod)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except Exception:
        return False


# Dotted keys map onto the nested config. Pool keys live under "pool.*".
_POOL_KEYS = {"repo_url", "mode", "branch", "require_signature", "sync_hours",
              "auto_contribute", "min_corroboration", "github_user"}
_TOP_KEYS = {"nudge_turns", "recall_k", "distill_model"}
# recall.semantic controls whether semantic recall is used even if the model is present
_RECALL_KEYS = {"semantic"}


def get_key(data: dict, dotted: str) -> Any:
    if "." in dotted:
        section, key = dotted.split(".", 1)
        return (data.get(section) or {}).get(key)
    return data.get(dotted)


def set_key(data: dict, dotted: str, value: Any) -> None:
    """Set a dotted key, coercing common types from string input."""
    value = _coerce(value)
    if "." in dotted:
        section, key = dotted.split(".", 1)
        data.setdefault(section, {})[key] = value
    elif dotted in _POOL_KEYS:
        data.setdefault("pool", {})[dotted] = value
    elif dotted in _RECALL_KEYS:
        data.setdefault("recall", {})[dotted] = value
    else:
        data[dotted] = value


def _coerce(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    low = v.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def known_keys() -> list[str]:
    return sorted(
        list(_TOP_KEYS)
        + [f"pool.{k}" for k in _POOL_KEYS]
        + [f"recall.{k}" for k in _RECALL_KEYS]
    )


__all__ = ["config_path", "load_raw", "save_raw", "get_key", "set_key", "known_keys"]
