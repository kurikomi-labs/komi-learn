"""OpenAI Codex CLI adapter for komi-learn — the second host.

Codex's lifecycle hooks are remarkably close to Claude Code's (same SessionStart/
Stop events, same `hookSpecificOutput.additionalContext` response, same stdin
fields). So this adapter is intentionally THIN: it reuses the host-agnostic engine
verbatim and the shared `komi.adapters.hooklib` for all the hook mechanics. The
only Codex-specific pieces are: config lives under ``~/.codex`` (``$CODEX_HOME``),
hooks register in ``~/.codex/hooks.json``, and the model backend is OpenAI/codex.

That this is a thin shim — not a fork — is the proof that the engine is genuinely
host-agnostic (Phase 6 goal).
"""

from __future__ import annotations

from ..base import Adapter, RecallContext


class CodexAdapter(Adapter):
    """Binds the komi-learn engine to the OpenAI Codex CLI host."""

    name = "codex"

    def recall(self, context: RecallContext) -> str:
        from .. import hooklib
        from . import paths
        return hooklib.build_recall_block(
            paths, cwd=context.cwd, recent_files=context.recent_files,
            prompt_hint=context.prompt_hint,
        )

    def on_session_end(self, turns: list[dict]):
        from ...engine.store import Store
        from ...engine.distill import distill
        from . import paths
        from .llm import build_llm
        llm = build_llm()
        personal = Store(paths.personal_root(), index_path=paths.index_path())
        return distill(turns, personal_store=personal, queue_dir=paths.queue_dir(),
                       llm=llm, judge=llm if hasattr(llm, "__call__") else None)


__all__ = ["CodexAdapter"]
