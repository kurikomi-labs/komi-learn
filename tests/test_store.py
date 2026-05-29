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
