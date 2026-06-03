"""Recall hook — inject recalled learnings into the agent's context.

Claude Code invokes this with a hook JSON on stdin. The recall build, event
classification, compaction dedup, and emit formatting all live in the shared
``komi.adapters.hooklib`` (same core the Codex adapter uses) — this module is a
thin Claude-Code shim: it wires hooklib to this host's ``paths`` module, owns the
background-maintenance cadence (pool sync + curator, genuine-session-start only),
and the ``--sync`` detached worker. It serves THREE events:

  • SessionStart (source = startup | resume | clear) — primary path; emits
    ``hookSpecificOutput.additionalContext`` once at session start (frozen-snapshot
    discipline that preserves the prompt cache).

  • SessionStart (source = compact) AND PostCompact — re-inject after a /compact.
    On current Claude Code SessionStart(compact) additionalContext is reportedly
    dropped (#15174) and PostCompact context-injection is under-documented, so we
    register BOTH and emit each event's correct format (additionalContext vs plain
    stdout). Best-effort; degrades to a harmless no-op if neither injects.

Entry points:
  ``python -m komi.adapters.claude_code.hook_recall``   (SessionStart)
  ``python -m komi.adapters.claude_code.hook_compact``  (PostCompact; shim → main)
"""

from __future__ import annotations

import sys

from .. import hooklib
from . import paths


def main(default_event: str = "") -> int:
    payload = _read_stdin_json()
    cwd = payload.get("cwd", "") or ""
    event, source = hooklib.classify_event(payload, default_event)
    is_compaction = (event == "PostCompact") or (event == "SessionStart" and source == "compact")

    # Background maintenance (pool sync ~12h, curator ~7d) belongs to a genuine
    # session START only — NOT a mid-session compaction re-inject.
    if not is_compaction:
        _maybe_sync_pool()
        try:
            from .curate import maybe_curate_in_background
            maybe_curate_in_background()
        except Exception:
            pass

    # Double-injection guard: SessionStart(compact) + PostCompact both fire for one
    # /compact; if a sibling already served this compaction moments ago, no-op.
    if is_compaction and _compaction_already_served(payload, event):
        hooklib.emit({}, note="komi recall: compaction already re-injected by a sibling event",
                     event=event)
        return 0

    # Recompute the block FRESH each event. On a genuine start, rebuild the index
    # from Markdown (fresh=True); on compaction, query the already-built index
    # (fresh=False) — a mid-session reindex + pool re-mirror is needless hot-path work.
    try:
        block = build_block(cwd, payload, fresh=not is_compaction)
    except Exception as e:
        # Never break the session because recall failed — record why (event-aware:
        # the _note is suppressed on PostCompact's verbatim-stdout channel).
        hooklib.emit({}, note=f"komi recall skipped: {e}", event=event)
        return 0

    if not block:
        hooklib.emit({}, event=event)
        return 0
    # The emit path itself must never break the session (e.g. a BrokenPipeError if
    # the host closed stdout early) — degrade to a harmless no-op.
    try:
        _emit_block(block, event, is_compaction)
        if is_compaction:
            _record_compaction_served(payload, event)
    except Exception:
        pass
    return 0


# ── thin delegations to hooklib (kept as module names so the host's tests can
#    patch them, and so the shim reads as a coherent recall surface) ──────────

def build_block(cwd: str, payload: dict, *, fresh: bool = True) -> str:
    """Build the recall block via the shared core, rooted at this host's paths."""
    return hooklib.build_recall_block(
        paths, cwd=cwd, recent_files=_recent_files(payload), prompt_hint="",
        recall_k=8, fresh=fresh,
    )


def _read_stdin_json() -> dict:
    return hooklib.read_stdin_json()


def _emit_block(block: str, event: str, is_compaction: bool) -> None:
    hooklib.emit_recall(block, event, is_compaction)


def _compaction_already_served(payload: dict, event: str) -> bool:
    return hooklib.compaction_already_served(paths, payload, event)


def _record_compaction_served(payload: dict, event: str) -> None:
    hooklib.record_compaction_served(paths, payload, event)


def _recent_files(payload: dict) -> list:
    # SessionStart doesn't carry file context; left as a hook for future use.
    return []


# ── background pool sync (Claude-Code cadence; detached so start never blocks) ──

def _maybe_sync_pool() -> None:
    """Trigger a background pool sync if the cadence has elapsed. Spawns detached."""
    try:
        from . import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return
        if not _sync_due(cfg.pool_sync_hours):
            return
        import subprocess, os
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
    """Throttle pool syncs via an atomic+locked state update (concurrent-session safe).
    Returns True (and records 'now') when >= *hours* since the last sync."""
    import time
    now = time.time()

    def _mut(state: dict) -> bool:
        last = float(state.get("pool_last_sync", 0) or 0)
        if now - last < hours * 3600:
            return False
        state["pool_last_sync"] = now
        return True

    return bool(paths.update_state(_mut))


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


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--sync":
        run_sync()
    else:
        raise SystemExit(main())
