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
    owns index.db) and ensure the project store has mirrored its rows into it."""
    personal = Store(paths.personal_root(), index_path=paths.index_path())
    proot = paths.project_root(cwd)
    if proot is not None:
        proj = Store(proot, index_path=paths.index_path())
        # cheap: make sure project rows are present in the shared index
        proj.reindex()
    return personal


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
    raise SystemExit(main())
