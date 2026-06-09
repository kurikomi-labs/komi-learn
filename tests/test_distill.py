"""Distiller: parsing, routing, secret rejection, global queueing."""

import json

from komi.engine.store import Store
from komi.engine.distill import distill, parse_transcript, render_for_prompt
from komi.engine.model import Scope


class FakeLLM:
    """Returns a fixed candidate set; ignores the prompt (deterministic)."""
    def __init__(self, candidates):
        self._c = candidates

    def complete(self, *, system, user):
        assert "distiller" in system.lower()      # prompt actually loaded
        return json.dumps(self._c)


def globalize_judge(lng, context):
    if "ripgrep" in lng.tags:
        return {"scope": "global", "category": lng.category,
                "generalized_title": "Prefer rg over grep -r",
                "generalized_body": "ripgrep is faster and respects .gitignore.",
                "rationale": "general tooling"}
    return {"scope": "project", "category": lng.category, "rationale": "project"}


def test_parse_transcript_jsonl(tmp_path):
    tr = tmp_path / "t.jsonl"
    tr.write_text(
        json.dumps({"role": "user", "content": "search for TODO"}) + "\n" +
        json.dumps({"role": "assistant", "content": [{"type": "text", "text": "using grep"}]}) + "\n" +
        json.dumps({"role": "user", "content": "use ripgrep not grep"}) + "\n",
        encoding="utf-8",
    )
    turns = parse_transcript(tr)
    assert len(turns) == 3
    assert turns[0]["role"] == "user"
    assert "grep" in render_for_prompt(turns)


def test_distill_routes_and_rejects_secret(tmp_path):
    candidates = [
        {"type": "identity", "category": "formatting-style", "title": "wants bullets",
         "body": "stop writing paragraphs", "trigger": "summarizing", "tags": ["style"],
         "signal": "user-correction"},
        {"type": "procedural", "category": "tooling", "title": "use rg",
         "body": "ripgrep is fast", "trigger": "code search", "tags": ["ripgrep"],
         "signal": "technique"},
        {"type": "semantic", "category": "tooling", "title": "token",
         "body": "token is sk-supersecret1234567890abcdef", "trigger": "auth",
         "tags": ["auth"], "signal": "durable-fact"},
    ]
    personal = Store(tmp_path / "personal")
    project = Store(tmp_path / "proj", index_path=personal.index_path)
    queue = tmp_path / "queue"

    res = distill(
        [{"role": "user", "text": "stop being verbose; use ripgrep"}],
        personal_store=personal, project_store=project, queue_dir=queue,
        llm=FakeLLM(candidates), judge=globalize_judge,
        session_id="s1", cwd=str(tmp_path), git_remote="",
    )

    assert res.candidates == 3
    assert res.rejected == 1                       # the secret
    assert res.queued_global == 1                  # ripgrep → review queue
    # secret must not be anywhere on disk
    blob = ""
    for p in tmp_path.rglob("*"):
        if p.is_file():
            blob += p.read_text(encoding="utf-8", errors="ignore")
    assert "supersecret" not in blob
    # identity landed in personal USER.md
    assert any(l.type == "identity" for l in personal.all())
    # queued global preview has no evidence
    qfiles = list(queue.glob("*.json"))
    assert len(qfiles) == 1
    rec = json.loads(qfiles[0].read_text(encoding="utf-8"))
    assert "evidence" not in rec["publishable_preview"]
    assert rec["status"] == "pending-review"


def test_distill_empty_is_nothing_to_save(tmp_path):
    personal = Store(tmp_path / "p")
    res = distill([{"role": "user", "text": "hi"}], personal_store=personal,
                  llm=FakeLLM([]), session_id="s", cwd=str(tmp_path))
    assert res.candidates == 0
    assert res.summary() == "Nothing to save."


def test_malformed_llm_output_yields_no_candidates(tmp_path):
    class Junk:
        def complete(self, *, system, user):
            return "I think you should save: not json at all"
    personal = Store(tmp_path / "p")
    res = distill([{"role": "user", "text": "x"}], personal_store=personal,
                  llm=Junk(), session_id="s", cwd=str(tmp_path))
    assert res.candidates == 0


# ── confidence: the distiller self-scores instead of stamping a constant 0.3 ──
# Regression guard for the field-data finding: every learning was born at the model
# default 0.3 because the distill schema had no confidence field. These pin the rubric.

def _conf_of(store, title):
    return next(l.confidence for l in store.all() if l.title == title)


def test_distiller_confidence_flows_through(tmp_path):
    personal = Store(tmp_path / "p")
    distill(
        [{"role": "user", "text": "x"}], personal_store=personal,
        llm=FakeLLM([{"type": "semantic", "category": "tooling",
                      "title": "durable rule", "body": "a transferable invariant",
                      "trigger": "always", "tags": ["x"], "signal": "durable-fact",
                      "confidence": 0.75}]),
        session_id="s", cwd=str(tmp_path),
    )
    assert _conf_of(personal, "durable rule") == 0.75   # honoured, not 0.3


def test_distiller_confidence_is_clamped(tmp_path):
    personal = Store(tmp_path / "p")
    distill(
        [{"role": "user", "text": "x"}], personal_store=personal,
        llm=FakeLLM([
            {"type": "semantic", "category": "tooling", "title": "over",
             "body": "b", "trigger": "t", "tags": ["a"], "confidence": 1.5},
            {"type": "semantic", "category": "tooling", "title": "under",
             "body": "b", "trigger": "t", "tags": ["a"], "confidence": -3},
        ]),
        session_id="s", cwd=str(tmp_path),
    )
    assert _conf_of(personal, "over") == 0.9            # clamped to band ceiling
    assert _conf_of(personal, "under") == 0.1           # clamped to band floor


def test_missing_or_garbled_confidence_keeps_default(tmp_path):
    personal = Store(tmp_path / "p")
    distill(
        [{"role": "user", "text": "x"}], personal_store=personal,
        llm=FakeLLM([
            {"type": "semantic", "category": "tooling", "title": "absent",
             "body": "b", "trigger": "t", "tags": ["a"]},                 # no field
            {"type": "semantic", "category": "tooling", "title": "garbled",
             "body": "b", "trigger": "t", "tags": ["a"], "confidence": "high"},
        ]),
        session_id="s", cwd=str(tmp_path),
    )
    # back-compat: model default preserved when the model omits/garbles the score
    assert _conf_of(personal, "absent") == 0.3
    assert _conf_of(personal, "garbled") == 0.3
