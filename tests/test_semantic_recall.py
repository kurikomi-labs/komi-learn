"""Phase 5 (partial) — semantic recall with keyword fallback.

Uses a deterministic MOCK embedder (a tiny bag-of-words vectorizer) so the tests
are offline and don't need the ~hundreds-of-MB real model. Covers:
  • embeddings persist + vector_search ranks by cosine,
  • recall prefers semantic when an embedder is present and finds meaning matches
    keyword would miss,
  • recall FALLS BACK to keyword when no embedder (the zero-dep guarantee).
"""

import math

import pytest

from komi.engine.store import Store
from komi.engine import embed as embed_mod
from komi.engine.recall import recall, RecallConfig, _candidate_hits
from komi.engine.model import Learning, LearningType, Category, Scope


# ── a deterministic mock embedder ───────────────────────────────────────────

_VOCAB = ["test", "suite", "unit", "pytest", "rust", "cargo", "docker",
          "image", "git", "commit", "python", "dependency", "lock"]


class MockEmbedder:
    """Bag-of-words over a fixed vocab, L2-normalized. Synonyms share vocab words
    so 'unit tests' and 'test suite' land near each other — enough to prove
    meaning-based ranking deterministically."""
    version = "mock/1"
    dim = len(_VOCAB)

    def encode(self, text: str) -> list[float]:
        t = (text or "").lower()
        vec = [float(t.count(w)) for w in _VOCAB]
        n = math.sqrt(sum(x * x for x in vec))
        return [x / n for x in vec] if n else vec


def L(title, body="", tags=None, conf=0.5):
    return Learning(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                    title=title, body=body or title, trigger="", tags=tags or [],
                    scope=Scope.PROJECT.value, confidence=conf).finalize()


@pytest.fixture
def use_mock_embedder(monkeypatch):
    embed_mod._reset_cache_for_tests()
    monkeypatch.setattr(embed_mod, "get_embedder", lambda: MockEmbedder())
    monkeypatch.setattr(embed_mod, "available", lambda: True)
    yield
    embed_mod._reset_cache_for_tests()


@pytest.fixture
def no_embedder(monkeypatch):
    embed_mod._reset_cache_for_tests()
    monkeypatch.setattr(embed_mod, "get_embedder", lambda: None)
    monkeypatch.setattr(embed_mod, "available", lambda: False)
    yield
    embed_mod._reset_cache_for_tests()


# ── store-level: embeddings persist + vector_search ─────────────────────────

def test_embed_pending_and_vector_search(tmp_path):
    s = Store(tmp_path)
    s.upsert(L("run the test suite", tags=["test"]))
    s.upsert(L("docker image build", tags=["docker"]))
    n = s.embed_pending(MockEmbedder())
    assert n == 2
    q = MockEmbedder().encode("unit test suite")
    hits = s.vector_search(q, limit=2)
    assert hits[0]["title"] == "run the test suite"     # nearest by meaning
    assert hits[0]["sim"] > hits[1]["sim"]


def test_embeddings_survive_reindex(tmp_path):
    s = Store(tmp_path)
    s.upsert(L("cargo test rust"))
    s.embed_pending(MockEmbedder())
    s.reindex()                                          # rebuild from markdown/skills
    # embeddings are recomputed lazily; vector_search after a re-embed still works
    s.embed_pending(MockEmbedder())
    assert s.vector_search(MockEmbedder().encode("rust cargo"), limit=1)


# ── recall: semantic-first ───────────────────────────────────────────────────

def test_recall_uses_semantic_when_available(tmp_path, use_mock_embedder):
    s = Store(tmp_path)
    # the relevant learning shares NO query keyword ("unit tests") but shares
    # meaning vocab ("test suite") — keyword FTS would likely miss it; semantic won't
    s.upsert(L("how to run the full test suite", tags=["test", "suite"]))
    s.upsert(L("docker image layer caching", tags=["docker", "image"]))
    block = recall(s, prompt_hint="run my unit tests", config=RecallConfig(k=1))
    assert "test suite" in block.lower()


def test_candidate_hits_semantic_path(tmp_path, use_mock_embedder):
    s = Store(tmp_path)
    s.upsert(L("pytest cargo rust"))
    cands = _candidate_hits(s, "rust cargo", limit=5, scopes=None)
    assert cands and 0.0 < cands[0][1] <= 1.0           # similarity is normalized


# ── recall: keyword fallback (zero-dep guarantee) ───────────────────────────

def test_recall_falls_back_to_keyword_without_embedder(tmp_path, no_embedder):
    s = Store(tmp_path)
    s.upsert(L("disable pytest cache", body="-p no:cacheprovider", tags=["pytest"]))
    # no embedder → keyword FTS path; an exact-term query still surfaces it
    block = recall(s, prompt_hint="pytest cache", config=RecallConfig(k=1))
    assert "pytest" in block.lower()


def test_candidate_hits_keyword_path(tmp_path, no_embedder):
    s = Store(tmp_path)
    s.upsert(L("git bisect regression", tags=["git"]))
    cands = _candidate_hits(s, "git bisect", limit=5, scopes=None)
    assert cands and 0.0 < cands[0][1] <= 1.0           # bm25 squashed into (0,1]


# ── cwd → project terms: recall stops ranking against a raw path string ──────
# Field-data finding: at SessionStart there's no user prompt, so the query was just the
# bare cwd path (separators + generic ancestors). Tokenizing it into project/subsystem
# terms gives similarity something real to match. (No hook-contract change — a real
# prompt would be better but isn't available before the first turn.)

from komi.engine.recall import _path_terms


def test_path_terms_extracts_project_subsystem():
    t = _path_terms("/home/alice/projects/GitHub-Store/feature/i18n")
    assert "store" in t and "i18n" in t                  # project + subsystem kept
    assert "projects" not in t and "home" not in t       # generic ancestors dropped
    assert "alice" not in t                              # home-dir username dropped


def test_path_terms_handles_windows_drive_and_camelcase():
    t = _path_terms(r"C:\Users\bob\code\MyApp\Src")
    assert "c" not in t                                  # drive colon never leaks
    assert "bob" not in t                                # username dropped
    assert "my" in t and "app" in t                      # CamelCase → words


def test_path_terms_empty_and_root_are_safe():
    assert _path_terms("") == []
    assert _path_terms("/") == []


def test_recall_uses_cwd_project_terms_as_query(tmp_path, no_embedder):
    """With no prompt_hint, recall should still surface a learning matching the project
    in the cwd — proving the path is tokenized into a usable query, not a dead string."""
    s = Store(tmp_path)
    s.upsert(L("i18n russian plurals", body="russian has 3-form plurals", tags=["i18n"]))
    s.upsert(L("docker layer caching", body="order dockerfile by change freq", tags=["docker"]))
    block = recall(s, cwd="/home/u/projects/GitHub-Store/feature/i18n",
                   config=RecallConfig(k=1))
    assert "russian" in block.lower() or "i18n" in block.lower()   # matched via cwd terms
