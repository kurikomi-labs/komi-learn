"""komi-learn — host-agnostic hook logic shared by every adapter.

The recall and distill hook bodies are ~90% identical across hosts (Claude Code,
Codex, …) — only three things differ per host: (1) where files live (a ``paths``
module), (2) the field names in the host's stdin payload, and (3) how the host
wants the response emitted. Everything else — building the recall block, the
turn-cadence throttle, spawning the detached distiller, the worker — is the same.

This module holds that shared core so a new adapter is a thin shim, not a copy.
Proving this seam is the whole point of the second-host (Codex) work: if the
engine were secretly Claude-Code-shaped, this refactor wouldn't be possible.

Each adapter passes its own ``paths`` module (must expose: personal_root(),
index_path(), project_root(cwd), queue_dir(), update_state(mutator)) and the
dotted module path of its distill worker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[2])  # so `python -m komi…` resolves


# ── recall ────────────────────────────────────────────────────────────────

def build_recall_block(paths_mod, *, cwd: str, recent_files: Optional[list[str]] = None,
                       prompt_hint: str = "", recall_k: int = 8) -> str:
    """Build the SessionStart context block from a host's stores. Host-neutral:
    opens the personal store, mirrors the project store + synced pool into the
    shared index, runs recall. Returns "" on any failure (never break a session)."""
    try:
        from ..engine.store import Store
        from ..engine.recall import recall as _recall, RecallConfig
        personal = Store(paths_mod.personal_root(), index_path=paths_mod.index_path())
        proot = paths_mod.project_root(cwd)
        if proot is not None:
            Store(proot, index_path=paths_mod.index_path()).reindex()
        _mirror_pool(paths_mod, personal)
        return _recall(personal, cwd=cwd, recent_files=recent_files or [],
                       prompt_hint=prompt_hint, config=RecallConfig(k=recall_k))
    except Exception:
        return ""


def _mirror_pool(paths_mod, personal) -> None:
    """Mirror the synced global pool into the shared index (best-effort)."""
    try:
        from ..pool.github_backend import GitHubPool, PoolConfig
        cfg = _pool_cfg(paths_mod)
        if not cfg:
            return
        pool = GitHubPool(PoolConfig(**cfg))
        learnings = pool.pull(limit=500)
        if learnings:
            personal.mirror_external(learnings, source="pool")
    except Exception:
        pass


def _pool_cfg(paths_mod) -> Optional[dict]:
    """A host exposes its pool config via paths_mod.pool_config() → dict | None."""
    fn = getattr(paths_mod, "pool_config", None)
    return fn() if callable(fn) else None


# ── distill cadence + spawn ─────────────────────────────────────────────────

def should_distill(paths_mod, session_id: str, *, nudge_turns: int) -> bool:
    """Throttle by turn count via the host's atomic+locked state. True (and resets)
    when accumulated turns since the last distill reach ``nudge_turns``."""
    key = f"turns:{session_id}"

    def _mut(state: dict) -> bool:
        state[key] = state.get(key, 0) + 1
        if state[key] >= nudge_turns:
            state[key] = 0
            return True
        return False

    return bool(paths_mod.update_state(_mut))


def spawn_distill_worker(worker_module: str, transcript: str, session_id: str,
                         cwd: str) -> None:
    """Launch a host's distill worker detached so the hook returns instantly.
    Cross-platform (Windows DETACHED_PROCESS / POSIX start_new_session)."""
    cmd = [sys.executable, "-m", worker_module, "--worker", transcript, session_id, cwd]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL, "cwd": _REPO_ROOT,
    }
    try:
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def run_distill_worker(paths_mod, build_llm: Callable, transcript: str,
                       session_id: str, cwd: str) -> None:
    """The detached worker body: distill a transcript into the host's stores.
    Host passes its build_llm (so the model backend can differ per host)."""
    try:
        from ..engine.store import Store
        from ..engine.distill import distill_from_file
        personal = Store(paths_mod.personal_root(), index_path=paths_mod.index_path())
        proot = paths_mod.project_root(cwd)
        project = Store(proot, index_path=paths_mod.index_path()) if proot else None
        llm = build_llm()
        distill_from_file(
            transcript, personal_store=personal, project_store=project,
            queue_dir=paths_mod.queue_dir(), llm=llm,
            judge=llm if hasattr(llm, "__call__") else None,
            session_id=session_id, cwd=cwd, git_remote=_git_remote(cwd),
        )
    except Exception:
        pass


def _git_remote(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ── stdin/stdout plumbing ───────────────────────────────────────────────────

def read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def emit_continue() -> int:
    """Stop hooks: just signal 'continue' (Claude Code + Codex both accept this)."""
    emit({"continue": True})
    return 0


def emit_session_context(block: str) -> int:
    """SessionStart: emit additionalContext if non-empty (identical schema on both
    Claude Code and Codex — hookSpecificOutput.additionalContext)."""
    if block:
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                     "additionalContext": block}})
    else:
        emit({})
    return 0


__all__ = [
    "build_recall_block", "should_distill", "spawn_distill_worker",
    "run_distill_worker", "read_stdin_json", "emit", "emit_continue",
    "emit_session_context",
]
