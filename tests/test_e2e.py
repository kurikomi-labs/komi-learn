"""End-to-end: a session transcript flows through distill → store, then a NEW
session's recall surfaces what was learned. This is the whole loop in one test.

It also exercises the Claude Code hook entry points by calling them directly with
synthetic stdin, proving the zero-friction wiring works without a live agent.
"""

import json
import os
import sys
import importlib

from komi.engine.store import Store
from komi.engine.distill import distill
from komi.engine.recall import recall, RecallConfig
from komi.engine.model import Scope


class ScriptedLLM:
    def complete(self, *, system, user):
        # Emulate a model that learned a style correction + a general technique.
        return json.dumps([
            {"type": "identity", "category": "formatting-style",
             "title": "Prefers terse, bulleted answers",
             "body": "User said stop writing paragraphs; wants bullets, no preamble.",
             "trigger": "any response", "tags": ["style"], "signal": "user-correction"},
            {"type": "procedural", "category": "debugging",
             "title": "Read the full traceback bottom-up",
             "body": "The root cause is usually the deepest frame; read bottom-up first.",
             "trigger": "debugging a python exception", "tags": ["python", "debugging"],
             "signal": "technique"},
        ])

    def __call__(self, lng, *, context):       # ScopeJudge
        if "python" in lng.tags:
            return {"scope": "global", "category": lng.category,
                    "generalized_title": lng.title, "generalized_body": lng.body,
                    "rationale": "general python debugging"}
        return {"scope": "project", "category": lng.category, "rationale": "project"}


def test_full_loop_distill_then_recall(tmp_path):
    personal = Store(tmp_path / "komi")
    queue = tmp_path / "queue"
    llm = ScriptedLLM()

    # ---- Session 1: user corrects style + a debugging technique emerges ----
    turns = [
        {"role": "user", "text": "why is this throwing a KeyError"},
        {"role": "assistant", "text": "Let me explain how dictionaries work in detail..."},
        {"role": "user", "text": "stop writing paragraphs, just bullets. and just read the traceback"},
    ]
    res = distill(turns, personal_store=personal, queue_dir=queue, llm=llm, judge=llm,
                  session_id="sess-1", cwd=str(tmp_path))
    assert res.candidates == 2
    assert any(l.type == "identity" for l in res.learnings)
    # python debugging technique was globalized → queued for review
    assert res.queued_global == 1
    assert len(list(queue.glob("*.json"))) == 1

    # ---- Session 2 (later): recall must surface the learned style + technique ----
    block = recall(personal, cwd=str(tmp_path),
                   prompt_hint="help me debug this python exception traceback",
                   config=RecallConfig(k=5))
    assert "terse" in block.lower() or "bullet" in block.lower()   # identity recalled
    assert "traceback" in block.lower()                            # technique recalled
    assert "<komi-recall>" in block                                # framed as data


def test_hooks_via_stdin(tmp_path, monkeypatch, capsys):
    """Drive the SessionStart recall hook the way Claude Code does: hook JSON on
    stdin, additionalContext on stdout."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    # reload paths so it picks up the env override
    from komi.adapters.claude_code import paths as paths_mod
    importlib.reload(paths_mod)
    from komi.adapters.claude_code import hook_recall as hr
    importlib.reload(hr)

    # seed a learning
    s = Store(paths_mod.personal_root(), index_path=paths_mod.index_path())
    from komi.engine.model import Learning, LearningType, Category
    s.upsert(Learning(type=LearningType.IDENTITY.value, category=Category.PREFERENCE.value,
                       title="Prefers terse answers", body="no preamble", trigger="always",
                       tags=["style"], confidence=0.9).finalize())
    s.close()

    payload = {"hook_event_name": "SessionStart", "session_id": "s1",
               "cwd": str(tmp_path), "source": "startup"}
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(payload)))
    rc = hr.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "terse" in ctx


class _FakeStdin:
    def __init__(self, data): self._data = data
    def read(self): return self._data
