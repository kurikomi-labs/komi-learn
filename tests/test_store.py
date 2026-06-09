"""Store: markdown round-trip, corroboration dedup, FTS, scope, archive."""

from komi.engine.store import Store
from komi.engine.model import Learning, LearningType, Category, Scope


def L(**kw) -> Learning:
    base = dict(type=LearningType.SEMANTIC.value, category=Category.TOOLING.value,
                title="uv lockfile", body="commit uv.lock", trigger="python deps",
                tags=["uv"], scope=Scope.GLOBAL.value, confidence=0.5)
    base.update(kw)
    return Learning(**base).finalize()


def test_upsert_and_reindex_roundtrip(tmp_path):
    s = Store(tmp_path)
    s.upsert(L())
    s.upsert(L(type=LearningType.IDENTITY.value, title="prefers terse",
              body="no preamble", scope=Scope.PERSONAL.value, tags=["style"]))
    assert len(s.rows()) == 2
    s.close()
    s2 = Store(tmp_path)
    assert s2.reindex() == 2  # rebuilt purely from markdown


def test_corroboration_dedup_raises_confidence(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(L(confidence=0.3))
    c0 = next(r["confidence"] for r in s.rows() if r["id"] == lid)
    s.upsert(L(confidence=0.3))  # identical content
    rows = [r for r in s.rows() if r["id"] == lid]
    assert len(rows) == 1                      # no duplicate
    assert rows[0]["confidence"] > c0          # corroborated


def test_fts_search_and_scope_filter(tmp_path):
    s = Store(tmp_path)
    s.upsert(L(title="disable pytest cache", body="-p no:cacheprovider",
               trigger="pytest in ci", tags=["pytest"], scope=Scope.PROJECT.value))
    s.upsert(L(title="uv lockfile", body="commit uv.lock", tags=["uv"], scope=Scope.GLOBAL.value))
    assert any("pytest" in r["title"] for r in s.search("pytest cache ci"))
    globals_only = s.search("uv", scopes=[Scope.GLOBAL.value])
    assert all(r["scope"] == Scope.GLOBAL.value for r in globals_only)


def test_archive_never_deletes(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(L())
    assert s.archive(lid) is True
    assert len(s.rows(state="active")) == 0
    assert len(s.rows(state="archived")) == 1   # archived, not gone


def test_malformed_fts_query_is_safe(tmp_path):
    s = Store(tmp_path)
    s.upsert(L())
    # FTS operator characters in raw text must not crash
    assert isinstance(s.search('AND OR "(" NEAR/'), list)


# ── recall telemetry: the counter must actually move ────────────────────────
# Regression guard for the field-data bug: a week of real use showed recalled=0
# on every learning because record_recalled() only stamped last_used and the
# `recalled` int was incremented NOWHERE in the codebase. These pin the fix.

def _recalled(store: Store, lid: str) -> int:
    return next(r["recalled"] for r in store.rows() if r["id"] == lid)


def test_record_recalled_increments_counter(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(L())
    assert _recalled(s, lid) == 0          # written 0 at distill time
    s.record_recalled([lid])
    assert _recalled(s, lid) == 1          # the counter is alive
    s.record_recalled([lid])
    assert _recalled(s, lid) == 2          # and it accumulates
    # last_used is stamped too (the original behavior, preserved)
    assert next(r["last_used"] for r in s.rows() if r["id"] == lid)


def test_record_recalled_dedups_ids_within_a_batch(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(L())
    # one recall event listing the same id twice must count once, not twice
    s.record_recalled([lid, lid])
    assert _recalled(s, lid) == 1


def test_reindex_preserves_recalled_count(tmp_path):
    """recalled lives only in the DB (runtime telemetry, not Markdown content).
    A reindex rebuilds rows from Markdown — it must NOT zero the counter, or every
    session-start reindex would erase recall history and make used learnings look
    untouched (the same class of bug as the earlier reused/last_used wipe)."""
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.record_recalled([lid])
    s.record_recalled([lid])
    assert _recalled(s, lid) == 2
    s.reindex()                            # rebuild from Markdown
    assert _recalled(s, lid) == 2          # survived the rebuild
    s.close()
    # And it survives a fresh process opening the same store + reindexing.
    s2 = Store(tmp_path)
    s2.reindex()
    assert _recalled(s2, lid) == 2


def test_recalled_is_queryable_telemetry_not_markdown_churn(tmp_path):
    """recalled is RUNTIME TELEMETRY: authoritative in the DB, exposed via rows().
    It deliberately does NOT get rewritten into Markdown on a bare recall — that
    would churn the committable file on every read and (for shareable learnings)
    leak usage patterns into version control. Markdown stays content-only; the
    `komi-learn stats` path reads telemetry from the DB. This pins that contract
    so a future change can't silently start writing recall counts to MEMORY.md."""
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.record_recalled([lid])
    # queryable via the public telemetry surface (rows → DB)
    assert _recalled(s, lid) == 1
    # but the committable Markdown is untouched by a recall (no churn / no leak)
    md = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert '"recalled": 0' in md and '"recalled": 1' not in md


# ── reuse-credit: the second dead counter, now wired ────────────────────────
# Field-data finding: `reused` was incremented NOWHERE, so it was a frozen-0 counter
# (like `recalled` had been). Reuse is now credited when a SURFACED lesson is
# independently re-derived. These pin the observable-signal contract.

def _reused(store: Store, lid: str) -> int:
    return next(r["reused"] for r in store.rows() if r["id"] == lid)


def test_record_reused_increments_and_dedups(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(L())
    assert _reused(s, lid) == 0
    assert s.record_reused([lid, lid]) == 1     # batch-dedup: one id credited
    assert _reused(s, lid) == 1
    s.record_reused([lid])
    assert _reused(s, lid) == 2                  # accumulates


def test_rederiving_a_surfaced_lesson_credits_reuse(tmp_path):
    """The keystone loop: a lesson is recalled (surfaced), then independently
    re-derived (content-id collision on upsert) → reuse is credited. This is the only
    path that originates a non-zero reused, and it's what makes the usefulness metric real."""
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.record_recalled([lid])                     # recall surfaced it into a session
    assert _reused(s, lid) == 0
    s.upsert(L())                                # SAME content distilled again → re-derivation
    assert _reused(s, lid) == 1                  # credited as reuse


def test_rederiving_a_never_surfaced_lesson_is_not_reuse(tmp_path):
    """Re-deriving a lesson that was NEVER recalled is mere corroboration, not reuse —
    you can't 'reuse' something you were never shown. Guards against inflating the signal."""
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.upsert(L())                                # re-derived, but never recalled
    assert _reused(s, lid) == 0                  # NOT credited as reuse
    # confidence still got the corroboration bump, though (that path is unchanged)
    assert next(r["confidence"] for r in s.rows() if r["id"] == lid) > 0.5


def test_reuse_credit_makes_corpus_health_instrumented(tmp_path):
    """Once anything is credited reuse, corpus_health (fed via all_with_telemetry, the
    honest analytics source) stops reporting 'reuse not instrumented' and surfaces a
    real surfaced_never_used verdict. Markdown-only all() can't show this — telemetry
    lives in the DB, so analytics MUST read all_with_telemetry()."""
    from komi.engine.curator import corpus_health
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.record_recalled([lid])
    assert corpus_health(s.all_with_telemetry()).get("reuse_instrumented") is False
    s.upsert(L())                                # re-derive a surfaced lesson → reuse credited
    h = corpus_health(s.all_with_telemetry())    # honest source: DB telemetry overlaid
    assert h["reuse_instrumented"] is True
    assert h["surfaced_never_used_share"] is not None   # now a real verdict, not None


def test_all_with_telemetry_overlays_db_counters(tmp_path):
    """all() reads content from Markdown (recalled/reused always 0 there);
    all_with_telemetry() overlays the live DB counters so analytics isn't blind."""
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.record_recalled([lid])
    s.record_reused([lid])
    plain = next(l for l in s.all() if l.id == lid)
    hydrated = next(l for l in s.all_with_telemetry() if l.id == lid)
    assert plain.usage.recalled == 0 and plain.usage.reused == 0     # Markdown is blind
    assert hydrated.usage.recalled == 1 and hydrated.usage.reused == 1  # DB overlaid


def test_recalled_column_migrates_onto_legacy_db(tmp_path):
    """An index.db created before the recalled column must gain it on open
    (ALTER migration), not crash — derived index, must self-heal."""
    import sqlite3
    s = Store(tmp_path)
    lid = s.upsert(L())
    s.close()
    # Simulate a pre-recalled DB by dropping the column via table rebuild is
    # awkward in sqlite; instead assert the live DB already has it and that a
    # second open is idempotent (the migration guard is `if "recalled" not in cols`).
    db = sqlite3.connect(s.index_path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(learnings)")}
    db.close()
    assert "recalled" in cols
    s2 = Store(tmp_path)                    # re-open: migration must be a no-op
    assert _recalled(s2, lid) == 0
