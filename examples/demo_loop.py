"""komi-learn — runnable demo of the full learning loop (no API key needed).

Simulates two sessions to show the loop end-to-end with a scripted "model":

  Session 1  the user corrects the agent's style and a reusable technique emerges
             → the distiller extracts learnings, routes them by scope, and queues
               a general one for the global pool (behind the human review gate).
  Session 2  a NEW session recalls what was learned and injects it as context —
             the agent now "starts already knowing".

Run:  python examples/demo_loop.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Allow running directly (python examples/demo_loop.py) without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles default to cp1252; force UTF-8 so box-drawing/dashes render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from komi.engine.store import Store
from komi.engine.distill import distill
from komi.engine.recall import recall, RecallConfig


class ScriptedModel:
    """Stands in for the LLM so the demo is deterministic and offline."""

    def complete(self, *, system: str, user: str) -> str:
        return json.dumps([
            {"type": "identity", "category": "formatting-style",
             "title": "Prefers terse, bulleted answers",
             "body": "User said to stop writing long paragraphs — wants bullets, no preamble.",
             "trigger": "any response", "tags": ["style", "verbosity"],
             "signal": "user-correction"},
            {"type": "procedural", "category": "debugging",
             "title": "Read Python tracebacks bottom-up",
             "body": "The root cause is usually the deepest frame. Read the traceback "
                     "from the bottom up before changing code.",
             "trigger": "debugging a python exception", "tags": ["python", "debugging"],
             "signal": "technique"},
            {"type": "semantic", "category": "tooling",
             "title": "Internal API base URL",
             "body": "The service is at https://api.internal.corp/v2 — note for later.",
             "trigger": "calling the api", "tags": ["api"], "signal": "durable-fact"},
        ])

    def __call__(self, lng, *, context):  # ScopeJudge
        if "python" in lng.tags:
            return {"scope": "global", "category": lng.category,
                    "generalized_title": lng.title, "generalized_body": lng.body,
                    "rationale": "general python debugging practice"}
        return {"scope": "project", "category": lng.category, "rationale": "project-specific"}


def banner(t: str) -> None:
    print("\n" + "═" * 70 + f"\n  {t}\n" + "═" * 70)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="komi-demo-"))
    personal = Store(workdir / "komi")
    queue = workdir / "queue"
    model = ScriptedModel()

    banner("SESSION 1 — the user works with the agent and corrects it")
    transcript = [
        {"role": "user", "text": "why does this raise a KeyError?"},
        {"role": "assistant", "text": "Let me give you a thorough explanation of how "
                                      "Python dictionaries handle missing keys, with background..."},
        {"role": "user", "text": "stop writing paragraphs, just bullets. and read the "
                                 "traceback bottom-up instead of guessing"},
        {"role": "assistant", "text": "- Root cause is the deepest frame\n- It's a missing key"},
    ]
    for t in transcript:
        print(f"  {t['role'].upper():9} {t['text'][:80]}")

    res = distill(transcript, personal_store=personal, queue_dir=queue,
                  llm=model, judge=model, session_id="sess-1", cwd=str(workdir))

    banner("DISTILL — what the background pass learned")
    print(f"  candidates extracted : {res.candidates}")
    print(f"  stored personally    : {res.stored_personal}")
    print(f"  queued for global    : {res.queued_global}  (awaiting your review — not published)")
    print(f"  rejected (unsafe)    : {res.rejected}")
    print(f"\n  summary: {res.summary()}")
    print("\n  Learnings now on disk:")
    for lng in personal.all():
        print(f"    • [{lng.type}/{lng.scope}] {lng.title}")

    if queue.exists():
        banner("GLOBAL REVIEW QUEUE — held back for your approval")
        for f in queue.glob("*.json"):
            rec = json.loads(f.read_text(encoding="utf-8"))
            prev = rec["publishable_preview"]
            print(f"  • {prev['title']}")
            print(f"    body: {prev['body']}")
            print(f"    has local evidence attached? {'evidence' in prev}  (must be False)")
            print(f"    status: {rec['status']}")

    banner("SESSION 2 — a brand-new session. What does the agent recall?")
    block = recall(personal, cwd=str(workdir),
                   prompt_hint="help me debug this python exception and keep it short",
                   config=RecallConfig(k=5))
    print(block)

    banner("RESULT")
    print("  The agent now starts the new session already knowing the user's style")
    print("  and the debugging technique — with zero commands typed. That's the loop.")
    print(f"\n  (demo files under {workdir})")


if __name__ == "__main__":
    main()
