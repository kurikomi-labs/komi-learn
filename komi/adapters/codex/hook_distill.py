"""Codex Stop/SubagentStop hook — distill the finished session in the background.

Thin shim over ``komi.adapters.hooklib``: throttle by turn cadence, then spawn the
detached distill worker. Never blocks the Codex turn.

Entry point: ``python -m komi.adapters.codex.hook_distill``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import hooklib
from . import paths

NUDGE_TURNS = int(os.environ.get("KOMI_NUDGE_TURNS", "8"))
_WORKER_MODULE = "komi.adapters.codex.hook_distill"


def main() -> int:
    payload = hooklib.read_stdin_json()
    transcript = payload.get("transcript_path", "") or ""
    session_id = payload.get("session_id", "") or ""
    cwd = payload.get("cwd", "") or ""
    if not transcript or not Path(transcript).exists():
        return hooklib.emit_continue()
    if not hooklib.should_distill(paths, session_id, nudge_turns=NUDGE_TURNS):
        return hooklib.emit_continue()
    hooklib.spawn_distill_worker(_WORKER_MODULE, transcript, session_id, cwd)
    return hooklib.emit_continue()


def run_worker(transcript: str, session_id: str, cwd: str) -> None:
    from .llm import build_llm
    hooklib.run_distill_worker(paths, build_llm, transcript, session_id, cwd)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--worker":
        run_worker(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(main())
