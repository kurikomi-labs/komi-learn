"""SessionStart hook — inject recalled learnings as additionalContext.

Claude Code invokes this with the SessionStart hook JSON on stdin. We build the
recall block from the personal + project stores and emit it via
``hookSpecificOutput.additionalContext`` so it lands in the model's context with
zero user action. Runs once at session start to keep the prompt prefix stable
(the frozen-snapshot discipline that preserves the host's prompt cache).

Entry point: ``python -m komi.adapters.claude_code.hook_recall``
"""

from __future__ import annotations

import json
import sys

from ...engine.store import Store
from ...engine.recall import recall, RecallConfig
from . import paths


def main() -> int:
    payload = _read_stdin_json()
    cwd = payload.get("cwd", "") or ""

    # Kick off a background pool sync if due (detached; never blocks this hook).
    _maybe_sync_pool()

    try:
        store = _merged_store(cwd)
        block = recall(
            store,
            cwd=cwd,
            recent_files=_recent_files(payload),
            prompt_hint="",
            config=RecallConfig(k=8, include_global=True),
        )
    except Exception as e:
        # Never break the session because recall failed — emit nothing.
        _emit({}, note=f"komi recall skipped: {e}")
        return 0

    if not block:
        _emit({})
        return 0

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": block,
        }
    })
    return 0


def _merged_store(cwd: str) -> Store:
    """Personal store is the base; if in a project, its learnings share the same
    index so a single recall query sees both. We open the personal store (which
    owns index.db) and ensure the project store + synced global pool are mirrored
    into the shared index so one recall query sees personal + project + global."""
    personal = Store(paths.personal_root(), index_path=paths.index_path())
    proot = paths.project_root(cwd)
    if proot is not None:
        proj = Store(proot, index_path=paths.index_path())
        # cheap: make sure project rows are present in the shared index
        proj.reindex()
    _mirror_pool_into_index(personal)
    return personal


def _mirror_pool_into_index(personal: Store) -> None:
    """Read the locally-synced, re-verified global pool and mirror it into the
    shared index (origin_root namespaced as 'pool') so recall can rank it
    alongside personal/project learnings. Best-effort: any failure is silent."""
    try:
        from ...pool.github_backend import GitHubPool, PoolConfig
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return
        pool = GitHubPool(PoolConfig(
            repo_url=cfg.pool_repo_url, cache_dir=cfg.pool_cache_dir,
            branch=cfg.pool_branch, require_signature=cfg.pool_require_signature,
        ))
        learnings = pool.pull(limit=500)
        if not learnings:
            return
        # Mirror under a dedicated 'pool' store so it has its own origin_root slice
        # and never collides with personal/project rows on reindex.
        pool_store = Store(paths.personal_root() / "pool", index_path=paths.index_path())
        pool_store._db.execute("DELETE FROM learnings WHERE origin_root=?", (pool_store._root_key,))
        pool_store._db.commit()
        for lng in learnings:
            pool_store._index_one(lng, source="pool")
        pool_store.close()
    except Exception:
        pass


def _maybe_sync_pool() -> None:
    """Trigger a background pool sync if the cadence has elapsed. Spawns detached
    so session start is never blocked on the network."""
    try:
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return
        if not _sync_due(cfg.pool_sync_hours):
            return
        import subprocess, sys, os
        from pathlib import Path
        cmd = [sys.executable, "-m", "komi.adapters.claude_code.hook_recall", "--sync"]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "stdin": subprocess.DEVNULL,
                  "cwd": str(Path(__file__).resolve().parents[3])}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def _sync_due(hours: float) -> bool:
    """Throttle pool syncs using a timestamp in state.json. Returns True (and
    records 'now') when at least *hours* have elapsed since the last sync."""
    import time
    sp = paths.state_path()
    state = {}
    try:
        if sp.exists():
            state = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    last = float(state.get("pool_last_sync", 0) or 0)
    now = time.time()
    if now - last < hours * 3600:
        return False
    state["pool_last_sync"] = now
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return True


def run_sync() -> None:
    """Detached worker: actually sync the pool repo to the local cache."""
    try:
        from ...pool.github_backend import GitHubPool, PoolConfig
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return
        GitHubPool(PoolConfig(
            repo_url=cfg.pool_repo_url, cache_dir=cfg.pool_cache_dir,
            branch=cfg.pool_branch,
        )).sync()
    except Exception:
        pass


def _recent_files(payload: dict) -> list[str]:
    # SessionStart doesn't carry file context; left as a hook for future use
    # (e.g. a wrapper that passes recently-edited paths). Empty is fine.
    return []


def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def _emit(obj: dict, *, note: str = "") -> None:
    if note:
        obj = {**obj, "_note": note}
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--sync":
        run_sync()
    else:
        raise SystemExit(main())
