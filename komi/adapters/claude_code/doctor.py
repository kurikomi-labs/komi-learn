"""komi-learn — ``komi-learn doctor``: diagnose the install and point at fixes.

Like ``hermes doctor``. Read-only and safe: it never modifies anything and never
raises. Each check returns pass/warn/fail with a one-line fix hint. The bar is:
recall must work (that's the always-on value); distill is allowed to be a warning.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Literal

from . import paths

Status = Literal["pass", "warn", "fail"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


def run_doctor() -> list[Check]:
    checks: list[Check] = []

    # 1. import + interpreter
    try:
        import komi
        checks.append(Check("install", "pass",
                            f"komi-learn {getattr(komi, '__version__', '?')} ({sys.executable})"))
    except Exception as e:
        checks.append(Check("install", "fail", str(e),
                            "pip install komi-learn"))
        return checks  # everything else depends on this

    # 2. hooks registered, and pointing at a python that can import komi
    checks.append(_check_hooks())

    # 3. config
    checks.append(_check_config())

    # 4. contributor key
    kp = paths.keys_dir() / "contributor.key.json"
    if kp.exists():
        checks.append(Check("identity", "pass", "contributor key present"))
    else:
        checks.append(Check("identity", "warn", "no contributor key",
                            "Run: komi-learn install  (needed only to contribute to the pool)"))

    # 5. model credential (distill) — warn, not fail
    checks.append(_check_model())

    # 6. pool reachability (best-effort)
    checks.append(_check_pool())

    # 7. learnings present
    checks.append(_check_learnings())

    return checks


def _check_hooks() -> Check:
    sp = paths.claude_home() / "settings.json"
    if not sp.exists():
        return Check("hooks", "fail", "no ~/.claude/settings.json",
                     "Run: komi-learn install")
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
    except Exception as e:
        return Check("hooks", "fail", f"settings.json unreadable: {e}", "Fix the JSON or restore the .komi-bak backup")
    hooks = data.get("hooks", {})
    present = [ev for ev in ("SessionStart", "Stop")
               if any("komi.adapters.claude_code" in h.get("command", "")
                      for entry in hooks.get(ev, []) for h in entry.get("hooks", []))]
    if "SessionStart" in present:
        return Check("hooks", "pass", f"registered: {', '.join(present)}")
    return Check("hooks", "fail", "SessionStart hook not registered",
                 "Run: komi-learn install")


def _check_config() -> Check:
    try:
        cpath = paths.personal_root() / "config.json"
        if cpath.exists():
            try:
                json.loads(cpath.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                return Check("config", "fail", f"config.json is invalid JSON: {e}",
                            "Repair or delete ~/.claude/komi/config.json, then re-run komi-learn install")
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if cfg.pool_enabled:
            return Check("config", "pass", f"pool: {cfg.pool_repo_url}")
        return Check("config", "warn", "pool not configured (personal-only)",
                     "Set pool.repo_url in ~/.claude/komi/config.json to join the global pool")
    except Exception as e:
        return Check("config", "fail", str(e), "Run: komi-learn install")


def _check_model() -> Check:
    """Verify distillation FOR REAL (a tiny model call), matching the install gate.

    Distillation is a REQUIRED capability under the strict-setup stance, so a model
    that doesn't actually respond is a FAIL, not a warning — doctor and install
    agree. (Network/transient failures will also read as fail here; re-run when
    connectivity is back.)"""
    from .requirements import verify_model
    r = verify_model()
    if r.ok:
        return Check("distillation", "pass", r.detail)
    return Check("distillation", "fail", r.detail, r.fix)


def _check_pool() -> Check:
    try:
        from . import config as cfg_mod
        from ...pool.github_backend import GitHubPool, PoolConfig
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return Check("pool", "warn", "not configured", "Set pool.repo_url to join the pool")
        cache = paths.personal_root() / "pool" / "repo"
        if (cache / ".git").exists():
            pool = GitHubPool(PoolConfig(repo_url=cfg.pool_repo_url, cache_dir=str(cache),
                                         require_signature=cfg.pool_require_signature))
            n = len(pool.pull())
            return Check("pool", "pass", f"{n} learning(s) cached locally")
        return Check("pool", "warn", "not synced yet",
                     "Syncs automatically on next session start, or run: komi-learn sync")
    except Exception as e:
        return Check("pool", "warn", f"check skipped ({e})")


def _check_learnings() -> Check:
    try:
        from ...engine.store import Store
        s = Store(paths.personal_root(), index_path=paths.index_path())
        n = len(s.all())
        s.close()
        if n:
            return Check("learnings", "pass", f"{n} personal learning(s) stored")
        return Check("learnings", "pass", "0 personal learnings yet (they accrue as you work)")
    except Exception as e:
        return Check("learnings", "warn", f"check skipped ({e})")


__all__ = ["run_doctor", "Check"]
