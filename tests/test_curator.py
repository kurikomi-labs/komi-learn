"""Curator: pruning eligibility, clustering, consolidation, never-delete invariant,
protected exemption, and skills persistence (the gap that let procedural learnings
slip through before the curator existed)."""

import time

import pytest

from komi.engine.store import Store
from komi.engine.curator import (
    curate, cluster, is_prunable, render_report, corpus_health,
    DEFAULT_STALE_DAYS, DEFAULT_CONFIDENCE_FLOOR,
)
from komi.engine.model import Learning, LearningType, Category, Scope


def P(title, *, body=None, tags=None, conf=0.5, reused=0, age_days=0.0,
      pinned=False, scope=Scope.PROJECT.value, origin="agent",
      typ=LearningType.PROCEDURAL.value) -> Learning:
    l = Learning(type=typ, category=Category.TOOLING.value, title=title,
                 body=body or f"body of {title}", trigger=f"when {title}",
                 tags=tags or ["x"], scope=scope, confidence=conf).finalize()
    l.usage.reused = reused
    l.lifecycle.pinned = pinned
    l.provenance.origin = origin
    if age_days:
        l.lifecycle.created_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_days * 86400))
    return l


_F = dict(stale_days=DEFAULT_STALE_DAYS, confidence_floor=DEFAULT_CONFIDENCE_FLOOR)


# ── pruning eligibility (deterministic) ─────────────────────────────────────

def test_prune_stale_unused_lowconf():
    assert is_prunable(P("x", conf=0.2, reused=0, age_days=60), **_F) is True


def test_keep_high_confidence():
    assert is_prunable(P("x", conf=0.9, age_days=99), **_F) is False


def test_keep_if_reused():
    assert is_prunable(P("x", conf=0.1, reused=2, age_days=99), **_F) is False


def test_keep_if_recent():
    assert is_prunable(P("x", conf=0.1, age_days=3), **_F) is False


def test_pinned_is_protected():
    assert is_prunable(P("x", conf=0.1, age_days=99, pinned=True), **_F) is False


def test_pool_origin_is_protected():
    pool = P("x", conf=0.1, age_days=99, scope=Scope.GLOBAL.value, origin="pool")
    assert is_prunable(pool, **_F) is False


# ── clustering ──────────────────────────────────────────────────────────────

def test_cluster_groups_shared_tag_or_word():
    items = [P("pytest cache", tags=["pytest"]),
             P("pytest fixtures", tags=["pytest"]),
             P("docker build", tags=["docker"])]
    clusters = cluster(items)
    keys = {c.key for c in clusters}
    assert any("pytest" in k for k in keys)
    # the lone docker item forms no cluster
    assert all(c.size >= 2 for c in clusters)


def test_cluster_ignores_protected_and_nonprocedural():
    items = [P("pytest a", tags=["pytest"], pinned=True),
             P("pytest b", tags=["pytest"]),
             P("a fact", tags=["pytest"], typ=LearningType.SEMANTIC.value)]
    # only one eligible procedural pytest item → no cluster
    assert cluster(items) == []


# ── skills persistence (the closed gap) ─────────────────────────────────────

def test_procedural_learnings_persist_and_load(tmp_path):
    s = Store(tmp_path)
    s.upsert(P("use ripgrep"))
    s.upsert(P("read tracebacks bottom-up"))
    assert {l.title for l in s.all()} == {"use ripgrep", "read tracebacks bottom-up"}
    # round-trips through reindex (rebuild from disk)
    s.close()
    s2 = Store(tmp_path)
    assert s2.reindex() == 2
    assert len(s2.all()) == 2


def test_skill_archive_keeps_file(tmp_path):
    s = Store(tmp_path)
    sid = s.upsert(P("archive me"))
    assert s.archive(sid) is True
    skills = list((tmp_path / "skills").glob("*/SKILL.md"))
    assert len(skills) == 1                                  # file kept (archived, not deleted)
    assert all(l.lifecycle.state == "archived" for l in s.all())


# ── full pass ────────────────────────────────────────────────────────────────

def _mock_consolidator(members):
    return {"title": "Working with pytest",
            "body": "merged: " + "; ".join(m.title for m in members),
            "trigger": "using pytest", "tags": ["pytest"], "category": "tooling"}


def test_curate_prunes_and_consolidates_without_deleting(tmp_path):
    s = Store(tmp_path)
    s.upsert(P("pytest cache", tags=["pytest"], conf=0.5))
    s.upsert(P("pytest fixtures", tags=["pytest"], conf=0.5))
    s.upsert(P("stale junk", conf=0.1, reused=0, age_days=90))
    rep = curate(s, consolidator=_mock_consolidator, **_F)

    assert len(rep.pruned) == 1                              # the stale one
    assert len(rep.consolidated) == 1                        # pytest umbrella
    active = [l.title for l in s.all() if l.lifecycle.state == "active"]
    archived = [l for l in s.all() if l.lifecycle.state == "archived"]
    assert active == ["Working with pytest"]
    assert len(archived) == 3                                # 2 absorbed + 1 stale, all KEPT
    # nothing was deleted — every original is still retrievable (archived)
    assert len(s.all()) == 4                                 # 3 originals + 1 umbrella


def test_curate_without_llm_flags_but_does_not_merge(tmp_path):
    s = Store(tmp_path)
    s.upsert(P("pytest cache", tags=["pytest"]))
    s.upsert(P("pytest fixtures", tags=["pytest"]))
    rep = curate(s, consolidator=None, **_F)
    assert rep.consolidated == []
    assert len(rep.clusters) == 1                            # flagged, not merged
    assert all(l.lifecycle.state == "active" for l in s.all())  # untouched


def test_curate_dry_run_mutates_nothing(tmp_path):
    s = Store(tmp_path)
    s.upsert(P("stale", conf=0.1, age_days=90))
    rep = curate(s, **_F, dry_run=True)
    assert len(rep.pruned) == 1                              # reported
    assert all(l.lifecycle.state == "active" for l in s.all())  # but NOT archived


def test_protected_learnings_never_touched(tmp_path):
    s = Store(tmp_path)
    pinned = P("pinned old junk", conf=0.05, age_days=200, pinned=True)
    s.upsert(pinned)
    rep = curate(s, consolidator=_mock_consolidator, **_F)
    assert pinned.id not in rep.pruned
    assert all(l.lifecycle.state == "active" for l in s.all())


def test_render_report_is_readable(tmp_path):
    s = Store(tmp_path)
    s.upsert(P("stale", conf=0.1, age_days=90))
    rep = curate(s, **_F)
    text = render_report(rep)
    assert "Curation Report" in text
    assert "Archived as stale" in text


# ── corpus health: the "surfaced but never used" junk signal ────────────────
# Now that `recalled` is a live counter, corpus_health surfaces the sharpest
# usefulness metric: learnings recall served into context yet were never acted
# on. This is the data-grounded answer to "are my learnings actually useful?"

def _used(title, *, recalled, reused):
    l = P(title)
    l.usage.recalled = recalled
    l.usage.reused = reused
    return l


def test_corpus_health_counts_surfaced_never_used():
    learnings = [
        _used("junk-1", recalled=5, reused=0),   # surfaced, never used → noise
        _used("junk-2", recalled=3, reused=0),   # surfaced, never used → noise
        _used("good",   recalled=4, reused=2),   # surfaced AND used → keeper
        _used("unseen", recalled=0, reused=0),   # never surfaced → not counted
    ]
    h = corpus_health(learnings)
    # reuse IS instrumented here (one learning has reused>0), so the share is a real verdict
    assert h["reuse_instrumented"] is True
    # 3 of 4 active were surfaced; 2 of those 3 were never used
    assert h["surfaced_never_used"] == 2
    assert h["surfaced_never_used_share"] == round(2 / 3, 2)


def test_corpus_health_share_is_None_when_reuse_uninstrumented():
    """THE HONESTY GATE. When nothing was ever credited reuse (reused==0 everywhere —
    the real field state, because reuse-credit is unwired), surfaced_never_used_share
    must be None ('not measurable'), NOT a scary 100% uselessness verdict computed from
    a frozen counter. This is the fix for misreading the dead counter as a quality fact."""
    learnings = [
        _used("a", recalled=5, reused=0),
        _used("b", recalled=3, reused=0),
    ]
    h = corpus_health(learnings)
    assert h["reuse_instrumented"] is False
    assert h["surfaced_never_used"] == 2            # the raw count is still exposed
    assert h["surfaced_never_used_share"] is None    # but NOT as a 1.0 verdict


def test_corpus_health_no_surfaced_is_none_not_crash():
    # nothing recalled and nothing reused → uninstrumented → share None, no divide-by-zero
    h = corpus_health([_used("x", recalled=0, reused=0)])
    assert h["surfaced_never_used"] == 0
    assert h["surfaced_never_used_share"] is None
    assert h["reuse_instrumented"] is False


def test_corpus_health_empty():
    h = corpus_health([])
    assert h["surfaced_never_used"] == 0
    assert h["surfaced_never_used_share"] is None
    assert h["reuse_instrumented"] is False
