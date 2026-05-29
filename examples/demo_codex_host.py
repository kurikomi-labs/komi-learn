"""komi-learn — proof the engine is genuinely host-agnostic (Phase 6).

The SAME engine that powers the Claude Code adapter, driven instead through the
**Codex** adapter against a Codex-style transcript and ~/.codex storage. No Claude
Code involved. A learning distilled in one Codex session is recalled in the next —
demonstrating "works for every agent", not just claiming it.

Run:  python examples/demo_codex_host.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Isolate a throwaway Codex home so we never touch the user's real ~/.codex.
os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="komi-codex-demo-")

from komi.adapters.codex import paths, CodexAdapter
from komi.adapters.base import RecallContext
from komi.engine.store import Store
from komi.engine.distill import distill
from komi.engine.model import Scope


class ScriptedGPT:
    """Stands in for GPT so the demo is deterministic + offline. The engine is
    model-agnostic, so it neither knows nor cares this isn't Claude."""

    def complete(self, *, system: str, user: str) -> str:
        return json.dumps([{
            "type": "procedural", "category": "tooling",
            "title": "Run cargo test with --workspace",
            "body": "In a multi-crate Rust workspace, `cargo test --workspace` runs "
                    "every crate's tests in one go.",
            "trigger": "testing a rust workspace", "tags": ["rust", "cargo"],
            "signal": "technique",
        }])

    def __call__(self, lng, *, context):
        return {"scope": Scope.PROJECT.value, "category": lng.category, "rationale": "project"}


def banner(t: str) -> None:
    print("\n" + "═" * 70 + f"\n  {t}\n" + "═" * 70)


def main() -> None:
    model = ScriptedGPT()
    cwd = os.getcwd()

    banner("HOST = OpenAI Codex CLI (not Claude Code)")
    print(f"  storage root: {paths.personal_root()}   (under $CODEX_HOME, not ~/.claude)")
    print(f"  adapter: {CodexAdapter().name}  → implements the same Adapter ABC")

    banner("SESSION 1 (Codex) — a Rust testing tip emerges; distiller learns it")
    turns = [
        {"role": "user", "text": "how do I run all tests across this rust workspace?"},
        {"role": "assistant", "text": "Use `cargo test --workspace`."},
        {"role": "user", "text": "that worked — thanks"},
    ]
    personal = Store(paths.personal_root(), index_path=paths.index_path())
    res = distill(turns, personal_store=personal, queue_dir=paths.queue_dir(),
                  llm=model, judge=model, session_id="codex-1", cwd=cwd)
    print(f"  distilled {res.candidates} candidate(s); stored {res.stored_personal + res.stored_project}")
    for l in personal.all():
        print(f"    • [{l.type}/{l.scope}] {l.title}")

    banner("SESSION 2 (Codex) — fresh session recalls it via CodexAdapter")
    block = CodexAdapter().recall(RecallContext(
        cwd=cwd, prompt_hint="running the tests in this rust workspace"))
    print(block)

    banner("RESULT")
    ok = "cargo test" in block.lower()
    print(f"  Learning distilled in a Codex session was recalled in the next —")
    print(f"  same engine, different host, files under $CODEX_HOME. Proof: {ok}")
    print(f"\n  (demo files under {paths.codex_home()})")


if __name__ == "__main__":
    main()
