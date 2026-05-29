"""Stop / SubagentStop hook — run the distiller after a turn, in the background.

Claude Code invokes this when the agent finishes responding. We respect a turn
cadence (default every 8 turns + on session end) so we don't distill after every
single reply, then spawn the distillation **detached** and exit immediately —
the hook never blocks the user (the Hermes pattern: learning happens off to the
side and never touches the live turn).

This module has two entry points:
  • main()        — the hook itself (decides cadence, spawns the worker, exits)
  • run_worker()  — the detached worker that actually distills a transcript

Entry point: ``python -m komi.adapters.claude_code.hook_distill``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import paths


NUDGE_TURNS = int(os.environ.get("KOMI_NUDGE_TURNS", "8"))


# ── the hook ────────────────────────────────────────────────────────────

def main() -> int:
    payload = _read_stdin_json()
    transcript = payload.get("transcript_path", "") or ""
    session_id = payload.get("session_id", "") or ""
    cwd = payload.get("cwd", "") or ""
    event = payload.get("hook_event_name", "")
    # Stop fires when Claude finishes; we treat every Stop as a candidate and use
    # the turn counter to throttle. (A future "session end" signal would force it.)
    if not transcript or not Path(transcript).exists():
        return _ok()

    if not _should_distill(session_id):
        return _ok()

    _spawn_worker(transcript, session_id, cwd)
    return _ok()


def _should_distill(session_id: str) -> bool:
    """Throttle by turn count using a tiny JSON state file. Returns True when the
    accumulated turns since the last distill reach NUDGE_TURNS, and resets."""
    sp = paths.state_path()
    state = {}
    try:
        if sp.exists():
            state = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    key = f"turns:{session_id}"
    state[key] = state.get(key, 0) + 1
    fire = state[key] >= NUDGE_TURNS
    if fire:
        state[key] = 0
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return fire


def _spawn_worker(transcript: str, session_id: str, cwd: str) -> None:
    """Launch the worker detached so the hook returns instantly. Cross-platform:
    Windows uses DETACHED_PROCESS; POSIX uses start_new_session."""
    cmd = [sys.executable, "-m", "komi.adapters.claude_code.hook_distill",
           "--worker", transcript, session_id, cwd]
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": str(Path(__file__).resolve().parents[3]),  # repo root, so -m resolves
    }
    try:
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass  # spawning failure must never surface to the user


# ── the detached worker ───────────────────────────────────────────────────

def run_worker(transcript: str, session_id: str, cwd: str) -> None:
    """Actually distill. Imports the engine here (not at hook import time) so the
    fast-path hook stays lightweight."""
    from ...engine.store import Store
    from ...engine.distill import distill_from_file
    from .llm import build_llm

    git_remote = _git_remote(cwd)
    personal = Store(paths.personal_root(), index_path=paths.index_path())
    proot = paths.project_root(cwd)
    project = Store(proot, index_path=paths.index_path()) if proot else None

    llm = build_llm()
    distill_from_file(
        transcript,
        personal_store=personal,
        project_store=project,
        queue_dir=paths.queue_dir(),
        llm=llm,
        judge=llm if hasattr(llm, "__call__") else None,
        session_id=session_id,
        cwd=cwd,
        git_remote=git_remote,
    )


def _git_remote(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ── plumbing ────────────────────────────────────────────────────────────

def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def _ok() -> int:
    # Stop hooks don't inject context; just signal "continue".
    sys.stdout.write(json.dumps({"continue": True}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--worker":
        run_worker(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(main())
