"""Regression tests for the AI-Engineer-lens review fixes (docs/04-ai-engineering-review.md):
popularity-bias damping, identity/community bounding, candidate cap+dedup, corpus drift."""

import time

from komi.engine.store import Store
from komi.engine.recall import recall, RecallConfig, _rank_score
from komi.engine.curator import corpus_health
from komi.engine.distill import distill, _dedup_candidates, MAX_CANDIDATES_PER_PASS
from komi.engine.model import Learning, LearningType, Category, Scope


def L(title, *, typ=LearningType.PROCEDURAL.value, scope=Scope.PROJECT.value,
      conf=0.5, reused=0, tags=None, age_days=0.0) -> Learning:
    l = Learning(type=typ, category=Category.TOOLING.value, title=title,
                 body=f"body {title}", trigger=f"when {title}", tags=tags or ["x"],
                 scope=scope, confidence=conf).finalize()
    l.usage.reused = reused
    if age_days:
        l.lifecycle.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(time.time() - age_days * 86400))
    return l


# ── #1: popularity-bias damping ─────────────────────────────────────────────

def test_reuse_is_log_dampened_not_linear():
    # A heavily-reused row must NOT dominate purely on reuse: a far-more-relevant
    # fresh row should still be able to win. Compare salience growth.
    class Row(dict):
        pass
    def row(conf, reused):
        return {"confidence": conf, "reused": reused, "updated_at": ""}
    # same similarity; the only difference is reuse
    s_new = _rank_score(row(0.5, 0), 0.5)
    s_used = _rank_score(row(0.5, 50), 0.5)
    # reuse helps, but the gap is bounded (log), not 51x linear
    assert s_used > s_new
    assert (s_used - s_new) < 0.25       # would be huge under the old linear formula


def test_relevance_beats_popularity(tmp_path):
    s = Store(tmp_path)
    # a hugely-reused but OFF-topic learning vs a fresh ON-topic one
    s.upsert(L("docker image layer caching tricks", tags=["docker"], reused=40, conf=0.7))
    on_topic = L("pytest fixture scope gotchas", tags=["pytest"], reused=0, conf=0.5)
    s.upsert(on_topic)
    block = recall(s, prompt_hint="debug a failing pytest fixture", config=RecallConfig(k=2))
    # the on-topic learning must surface (relevance dominates, not raw reuse)
    assert "pytest" in block.lower()


# ── #2: identity bounded ────────────────────────────────────────────────────

def test_identity_recall_is_capped(tmp_path):
    s = Store(tmp_path)
    for i in range(12):
        s.upsert(L(f"user fact {i}", typ=LearningType.IDENTITY.value,
                   scope=Scope.PERSONAL.value, conf=0.5 + i * 0.01))
    block = recall(s, config=RecallConfig(max_identity=4, k=2))
    # only the capped number of identity facts appear in the "Who you're working with" block
    who = block.split("## Relevant")[0]
    assert who.count("- ") <= 4


# ── #6: community items capped + deduped in recall ──────────────────────────

def test_community_items_capped(tmp_path):
    s = Store(tmp_path)
    for i in range(8):
        s.upsert(L(f"community trick {i}", scope=Scope.GLOBAL.value, tags=["trick"], conf=0.6))
    s.upsert(L("personal trick", scope=Scope.PERSONAL.value, tags=["trick"], conf=0.6))
    block = recall(s, prompt_hint="a trick", config=RecallConfig(k=8, max_community=2))
    # count actual list items tagged [community] (not the explanatory note line,
    # which also contains the literal "[community]")
    items = [ln for ln in block.splitlines()
             if ln.strip().startswith("- ") and "[community]" in ln]
    assert len(items) <= 2


# ── #4: candidate cap + dedup ───────────────────────────────────────────────

def test_dedup_candidates():
    cands = [{"title": "A", "body": "x"}, {"title": "A", "body": "x"},
             {"title": "B", "body": "y"}, {"title": "", "body": "z"}]
    out = _dedup_candidates(cands)
    assert len(out) == 2                  # A (deduped) + B; empty-title dropped


def test_distill_caps_runaway_candidates(tmp_path):
    import json as _json
    flood = [{"type": "procedural", "category": "tooling", "title": f"t{i}",
              "body": f"b{i}", "trigger": "w", "tags": ["x"], "signal": "technique"}
             for i in range(100)]
    class FloodLLM:
        def complete(self, *, system, user): return _json.dumps(flood)
    s = Store(tmp_path / "p")
    res = distill([{"role": "user", "text": "x"}], personal_store=s,
                  llm=FloodLLM(), session_id="s", cwd=str(tmp_path))
    assert res.candidates <= MAX_CANDIDATES_PER_PASS    # bounded, not 100


# ── #5: corpus health / drift ───────────────────────────────────────────────

def test_corpus_health_flags_stale():
    learns = [
        L("fresh used", conf=0.8, reused=3, age_days=1),
        L("stale unused 1", conf=0.2, reused=0, age_days=120),
        L("stale unused 2", conf=0.1, reused=0, age_days=200),
    ]
    h = corpus_health(learns)
    assert h["active"] == 3
    assert h["stale_unused"] == 2
    assert h["stale_share"] == round(2 / 3, 2)
    assert h["never_reused"] == 2


def test_corpus_health_empty():
    h = corpus_health([])
    assert h["active"] == 0 and h["stale_share"] == 0.0
