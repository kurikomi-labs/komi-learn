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
                       prompt_hint: str = "", recall_k: int = 8, fresh: bool = True) -> str:
    """Build the SessionStart context block from a host's stores. Host-neutral:
    opens the personal store, mirrors the project store + synced pool into the
    shared index, runs recall. Returns "" on any failure (never break a session).

    ``fresh`` (default True) rebuilds this store's index slice from Markdown and
    re-mirrors the synced pool — the right thing at a genuine session start. When
    False (a mid-session compaction re-inject), we SKIP that rebuild and query the
    index that was already populated at session start: a full project reindex +
    500-row pool re-mirror is too heavy to run synchronously in the hook's critical
    path on every compaction. Lifted from the Claude Code adapter so any host with a
    compaction event gets the same perf behavior."""
    try:
        apply_semantic_pref(paths_mod)
        from ..engine.store import Store
        from ..engine.recall import recall as _recall, RecallConfig
        personal = Store(paths_mod.personal_root(), index_path=paths_mod.index_path())
        if fresh:
            proot = paths_mod.project_root(cwd)
            if proot is not None:
                Store(proot, index_path=paths_mod.index_path()).reindex()
            _mirror_pool(paths_mod, personal)
        return _recall(personal, cwd=cwd, recent_files=recent_files or [],
                       prompt_hint=prompt_hint, config=RecallConfig(k=recall_k))
    except Exception:
        return ""


def apply_semantic_pref(paths_mod) -> None:
    """Export the user's recall.semantic preference to KOMI_SEMANTIC so the
    host-agnostic engine honors 'semantic off' even when the model is installed.

    A supported cross-module entry point (the curate worker calls it too — it's a
    fresh process that didn't see the recall hook's env export)."""
    try:
        import json as _json
        import os as _os
        cfg_path = paths_mod.personal_root() / "config.json"
        if not cfg_path.exists():
            return
        data = _json.loads(cfg_path.read_text(encoding="utf-8"))
        sem = (data.get("recall") or {}).get("semantic")
        if sem is not None:
            _os.environ["KOMI_SEMANTIC"] = "1" if sem else "0"
            from ..engine import embed
            embed._reset_cache_for_tests()   # re-resolve with the new pref
    except Exception:
        pass


# Back-compat alias (was private). Kept so existing callers/tests don't break.
_apply_semantic_pref = apply_semantic_pref


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

_MAX_STDIN_BYTES = 4 * 1024 * 1024  # hook payloads are tiny; cap to avoid a runaway read


def read_stdin_json() -> dict:
    try:
        data = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        if len(data) > _MAX_STDIN_BYTES:
            return {}                     # oversized/garbage payload → safe no-op
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def emit(obj: dict, *, note: str = "", event: str = "") -> None:
    """Emit a JSON result. Event-aware + pipe-safe:

    - For PostCompact, stdout is appended VERBATIM to the model's context (its
      documented add-to-context path), so a diagnostic JSON blob like
      ``{"_note": ...}`` would pollute it — emit NOTHING on PostCompact.
    - Otherwise (SessionStart / Codex / no event) write the JSON object; an empty
      ``{}`` is the correct no-op for the additionalContext schema.
    - A closed stdout (BrokenPipeError, host hung up early) must never wedge the
      session — swallow any write error.

    ``note``/``event`` default to empty, so existing callers (e.g. Codex's
    ``emit_session_context``) are unchanged."""
    if event == "PostCompact":
        return
    if note:
        obj = {**obj, "_note": note}
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except Exception:
        pass


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


# ── compaction-aware recall routing (lifted from the Claude Code adapter) ────
# A host that re-injects after a context-truncation event (Claude Code's /compact)
# needs: (1) to classify the event, (2) to emit in the format that event honors,
# (3) a guard so two sibling events don't double-inject. These are host-agnostic;
# the adapter supplies its event/source field names + its paths module.

_COMPACTION_FRAME = ("Recalled learnings (re-applied after this conversation was "
                     "compacted — keep using them):\n")
# How close two events must be (seconds) to count as serving the SAME compaction.
_COMPACTION_DEDUP_WINDOW = 45.0


def classify_event(payload: dict, default_event: str = "") -> tuple:
    """Return ``(event, source)`` from a hook payload. ``event`` is the hook event
    name; ``source`` is the trigger (startup/resume/clear/compact | manual/auto),
    empty if absent. ``default_event`` (supplied by the invoking entry point, e.g.
    a PostCompact shim) WINS over a missing ``hook_event_name`` so a real event is
    never misrouted just because the host omitted the field. Defaults to
    SessionStart so a bare/legacy payload behaves as before."""
    event = payload.get("hook_event_name") or default_event or "SessionStart"
    source = payload.get("source") or payload.get("trigger") or ""
    return event, source


def emit_recall(block: str, event: str, is_compaction: bool) -> int:
    """Emit a (non-empty) recall block in the form the given event supports:
    PostCompact → plain stdout (its add-to-context path); SessionStart (incl.
    source=compact) → structured additionalContext. At compaction the block is
    framed so the model knows it's a re-application. Pipe-safe."""
    try:
        if event == "PostCompact":
            sys.stdout.write(_COMPACTION_FRAME + block)
            sys.stdout.flush()
            return 0
        ctx = (_COMPACTION_FRAME + block) if is_compaction else block
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                     "additionalContext": ctx}})
    except Exception:
        pass        # a broken pipe must never wedge the session
    return 0


def _compaction_key(payload: dict) -> str:
    """Identify a compaction event for dedup — prefer the host's session id (both
    sibling events share it); fall back to a constant so dedup still works per-window
    when no id is present."""
    return str(payload.get("session_id") or payload.get("sessionId") or "_nosid")


def compaction_already_served(paths_mod, payload: dict, event: str) -> bool:
    """True if a SIBLING event already re-injected for THIS compaction (same session
    id, within the dedup window) — so we don't inject twice. Read-only."""
    import time
    key = _compaction_key(payload)
    try:
        state = paths_mod.read_state()
    except Exception:
        return False
    last = state.get("last_compact_reinject") or {}
    if last.get("key") != key:
        return False
    if last.get("event") == event:
        return False        # the SAME event re-firing (e.g. retry) — let it re-inject
    try:
        return (time.time() - float(last.get("ts", 0))) < _COMPACTION_DEDUP_WINDOW
    except Exception:
        return False


def record_compaction_served(paths_mod, payload: dict, event: str) -> None:
    """Breadcrumb: record that THIS event re-injected for THIS compaction. Doubles as
    the dedup signal a sibling reads, and as on-device observability."""
    import time
    key = _compaction_key(payload)
    now = time.time()

    def _mut(s: dict):
        s["last_compact_reinject"] = {"key": key, "event": event, "ts": now}
        return None

    try:
        paths_mod.update_state(_mut)
    except Exception:
        pass


__all__ = [
    "build_recall_block", "should_distill", "spawn_distill_worker",
    "run_distill_worker", "read_stdin_json", "emit", "emit_continue",
    "emit_session_context", "apply_semantic_pref",
    # compaction-aware recall routing
    "classify_event", "emit_recall", "compaction_already_served",
    "record_compaction_served",
]
