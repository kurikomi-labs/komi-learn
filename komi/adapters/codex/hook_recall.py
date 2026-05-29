"""Codex SessionStart hook — inject recalled learnings as additionalContext.

Thin shim over the shared ``komi.adapters.hooklib``: parse Codex's stdin payload,
build the recall block from the Codex stores, emit it in Codex's response schema
(identical to Claude Code's — hookSpecificOutput.additionalContext).

Entry point: ``python -m komi.adapters.codex.hook_recall``
"""

from __future__ import annotations

import sys

from .. import hooklib
from . import paths


def main() -> int:
    payload = hooklib.read_stdin_json()
    cwd = payload.get("cwd", "") or ""
    block = hooklib.build_recall_block(paths, cwd=cwd)
    return hooklib.emit_session_context(block)


def run_sync() -> None:
    """Detached pool-sync worker (same as Claude Code's, Codex-rooted)."""
    try:
        from ...pool.github_backend import GitHubPool, PoolConfig
        cfg = paths.pool_config()
        if cfg:
            GitHubPool(PoolConfig(**cfg)).sync()
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--sync":
        run_sync()
    else:
        raise SystemExit(main())
