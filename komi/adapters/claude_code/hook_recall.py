"""Recall hook — inject recalled learnings into the agent's context.

Claude Code invokes this with a hook JSON on stdin. We build the recall block from
the personal + project stores and emit it so it lands in the model's context with
zero user action. It serves THREE events:

  • SessionStart (source = startup | resume | clear) — the primary path; emits
    ``hookSpecificOutput.additionalContext``. Runs once at session start to keep the
    prompt prefix stable (the frozen-snapshot discipline that preserves the cache).

  • SessionStart (source = compact) AND PostCompact — re-inject after a /compact (or
    auto-compact), because compaction can drop the originally-injected learnings and
    the agent would otherwise stop applying them mid-session. Research caveat: on
    current Claude Code, SessionStart(compact) additionalContext is known to be
    dropped (issue #15174) and PostCompact context-injection is under-documented — so
    we register on BOTH and additionally print the block as plain stdout (the
    documented "stdout is added to context" mechanism), to maximize the chance the
    re-injection actually lands on whatever the host version honors. Best-effort by
    design; if none inject, it degrades to a harmless no-op (the learnings are still
    on disk and reload fully next session).

Entry points:
  ``python -m komi.adapters.claude_code.hook_recall``   (SessionStart)
  ``python -m komi.adapters.claude_code.hook_compact``  (PostCompact; thin shim → main)
"""

from __future__ import annotations

import json
import sys

from ...engine.store import Store
from ...engine.recall import recall, RecallConfig
from . import paths


def main(default_event: str = "") -> int:
    payload = _read_stdin_json()
    cwd = payload.get("cwd", "") or ""
    event, source = _classify_event(payload, default_event)
    is_compaction = (event == "PostCompact") or (event == "SessionStart" and source == "compact")

    # Background maintenance (pool sync ~12h, curator ~7d) belongs to a genuine
    # session START only — NOT to a compaction re-inject (which happens mid-session
    # and shouldn't kick off cadenced jobs or disturb the running session).
    if not is_compaction:
        _maybe_sync_pool()
        try:
            from .curate import maybe_curate_in_background
            maybe_curate_in_background()
        except Exception:
            pass

    # Double-injection guard: we register BOTH SessionStart(compact) and PostCompact
    # for one compaction (a host-reliability hedge — see module docstring). On a host
    # that honors both channels the block would otherwise be injected twice. If a
    # sibling event already served THIS compaction moments ago, no-op.
    if is_compaction and _compaction_already_served(payload, event):
        _emit({}, note="komi recall: compaction already re-injected by a sibling event")
        return 0

    try:
        # Recompute the block FRESH every time — at compaction this picks up anything
        # learned earlier this session. On a genuine session start we rebuild the
        # index from Markdown (fresh=True); on a compaction we query the index that
        # was already built at session start (fresh=False) — rebuilding mid-session
        # would be a synchronous reindex + pool re-mirror in the hook's critical path.
        block = build_block(cwd, payload, fresh=not is_compaction)
    except Exception as e:
        # Never break the session because recall failed — emit nothing.
        _emit({}, note=f"komi recall skipped: {e}")
        return 0

    # The emit path itself must never break the session (e.g. a BrokenPipeError if
    # the host closed the hook's stdout early) — degrade to a harmless no-op.
    try:
        if not block:
            _emit({})
            return 0
        _emit_block(block, event, is_compaction)
        if is_compaction:
            _record_compaction_served(payload, event)
    except Exception:
        pass
    return 0


def build_block(cwd: str, payload: dict, *, fresh: bool = True) -> str:
    """Build the recall context block from the merged store. Reusable across events.

    ``fresh`` rebuilds this store's index slice + re-mirrors the pool from disk (the
    right thing at a genuine session start). When False (compaction), we skip that
    rebuild and query the existing shared index — it was already populated at session
    start, and a mid-session reindex is needless synchronous work in the hook path.
    """
    store = _merged_store(cwd, fresh=fresh)
    return recall(
        store,
        cwd=cwd,
        recent_files=_recent_files(payload),
        prompt_hint="",
        config=RecallConfig(k=8, include_global=True),
    )


def _classify_event(payload: dict, default_event: str = "") -> tuple[str, str]:
    """Return (event, source). ``event`` is the hook event name (SessionStart /
    PostCompact / …); ``source`` is the SessionStart trigger (startup/resume/clear/
    compact) or the compaction trigger (manual/auto), empty if absent.

    ``default_event`` is supplied by the invoking entry point (e.g. hook_compact
    passes "PostCompact") and WINS over a missing/absent ``hook_event_name`` — the
    entry point knows its own identity, so we never misroute a real PostCompact to
    the SessionStart format just because the host omitted the field. Falls back to
    SessionStart so a bare/legacy payload behaves exactly as before."""
    event = payload.get("hook_event_name") or default_event or "SessionStart"
    source = payload.get("source") or payload.get("trigger") or ""
    return event, source


# How close two events must be (seconds) to count as serving the SAME compaction.
# SessionStart(compact) and PostCompact fire within moments of one /compact; a later
# genuine compaction is many seconds away. Generous enough to dedup siblings, tight
# enough not to swallow a real subsequent compaction.
_COMPACTION_DEDUP_WINDOW = 45.0


def _compaction_key(payload: dict) -> str:
    """Identify a compaction event for dedup. Prefer the host's session id (both
    sibling events share it); fall back to a constant so dedup still works per-window
    when no id is present."""
    return str(payload.get("session_id") or payload.get("sessionId") or "_nosid")


def _compaction_already_served(payload: dict, event: str) -> bool:
    """True if a sibling event already re-injected for THIS compaction (same session
    id, within the dedup window) — so we don't inject the block twice. Read-only."""
    import time
    key = _compaction_key(payload)
    try:
        state = paths.update_state(lambda s: s) or {}
    except Exception:
        return False
    last = state.get("last_compact_reinject") or {}
    if last.get("key") != key:
        return False
    if last.get("event") == event:
        return False  # the SAME event re-firing (e.g. retry) — let it re-inject
    try:
        return (time.time() - float(last.get("ts", 0))) < _COMPACTION_DEDUP_WINDOW
    except Exception:
        return False


def _record_compaction_served(payload: dict, event: str) -> None:
    """Breadcrumb: record that THIS event re-injected for THIS compaction. Doubles as
    the dedup signal a sibling event reads, and as on-device observability (which path
    actually fired in production)."""
    import time
    key = _compaction_key(payload)
    now = time.time()

    def _mut(s: dict):
        s["last_compact_reinject"] = {"key": key, "event": event, "ts": now}
        return None

    try:
        paths.update_state(_mut)
    except Exception:
        pass


def _emit_block(block: str, event: str, is_compaction: bool) -> None:
    """Emit the recall block in the form the given event supports.

    The two injection mechanisms are mutually exclusive on one stdout stream (a
    JSON object plus trailing plain text is neither valid JSON nor clean text), so
    we choose by EVENT:

      • SessionStart (incl. source=compact): structured
        ``hookSpecificOutput.additionalContext`` — the documented SessionStart path.
      • PostCompact: plain stdout text — the documented "stdout is added to context"
        path for PostCompact (its JSON additionalContext support is unconfirmed).

    Registering komi on BOTH SessionStart(compact) and PostCompact is the
    belt-and-suspenders part (see module docstring): each speaks its own correct
    format, so whichever event the host version actually honors, the learnings land.
    At compaction we frame the block so the model knows it's a re-application."""
    if event == "PostCompact":
        framed = (
            "Recalled learnings (re-applied after this conversation was compacted — "
            "keep using them):\n" + block
        )
        sys.stdout.write(framed)        # plain stdout: PostCompact's add-to-context path
        sys.stdout.flush()
        return

    # SessionStart (startup/resume/clear/compact): structured additionalContext.
    ctx = block
    if is_compaction:
        ctx = ("Recalled learnings (re-applied after this conversation was "
               "compacted — keep using them):\n" + block)
    _emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": ctx}})


def _merged_store(cwd: str, *, fresh: bool = True) -> Store:
    """Personal store is the base; if in a project, its learnings share the same
    index so a single recall query sees both. We open the personal store (which
    owns index.db) and ensure the project store + synced global pool are mirrored
    into the shared index so one recall query sees personal + project + global.

    When ``fresh`` is False (a compaction re-inject), we SKIP the project reindex
    and the pool re-mirror: the shared index was already built at session start, and
    those operations are a full DELETE+re-INSERT (project Markdown) plus up to a
    500-row pool mirror — too heavy to run synchronously in the hook's critical path
    on every mid-session compaction. We just query what's already indexed.
    """
    personal = Store(paths.personal_root(), index_path=paths.index_path())
    if not fresh:
        return personal
    proot = paths.project_root(cwd)
    if proot is not None:
        proj = Store(proot, index_path=paths.index_path())
        # make sure project rows are present in the shared index (session start only)
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
        # Mirror via the Store's public API (own origin_root namespace, never
        # collides with personal/project rows). No reach into Store internals.
        personal.mirror_external(learnings, source="pool")
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
    """Throttle pool syncs via an atomic+locked state update (concurrent sessions
    safe). Returns True (and records 'now') when >= *hours* since the last sync."""
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


def _recent_files(payload: dict) -> list[str]:
    # SessionStart doesn't carry file context; left as a hook for future use
    # (e.g. a wrapper that passes recently-edited paths). Empty is fine.
    return []


_MAX_STDIN_BYTES = 4 * 1024 * 1024  # hook payloads are tiny; cap to avoid a runaway read


def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        if len(data) > _MAX_STDIN_BYTES:
            return {}                     # oversized/garbage payload → safe no-op
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
