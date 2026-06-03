"""Visibility axis: shareable vs private learnings.

The product gap this closes: komi-learn distilled confidential business data (cap
tables, fundraising, strategy) into the same MEMORY.md as shareable craft, so the
file could neither be safely committed nor safely gitignored. Now a `visibility`
axis (orthogonal to scope) routes private learnings to gitignored `.local` files,
a deterministic confidential floor + the LLM mark them private, and a safe
`.gitignore` ships by default. These tests pin every guarantee.
"""

import os
import tempfile
import importlib

import pytest

from komi.engine.model import Learning, Visibility, Scope
from komi.engine.classify import classify, safety_floor
from komi.engine.store import Store


# ── model: visibility is policy, not content (never in the id) ────────────────

def test_visibility_not_in_id():
    a = Learning(type="semantic", category="domain-knowledge", title="X", body="Y",
                 trigger="t", tags=["a"], visibility="shareable").finalize()
    b = Learning(type="semantic", category="domain-knowledge", title="X", body="Y",
                 trigger="t", tags=["a"], visibility="private").finalize()
    assert a.id == b.id                          # same content → same id
    assert "visibility" not in a.content_view()
    assert "visibility" not in b.publishable()


def test_visibility_back_compat_defaults_shareable():
    old = {"type": "semantic", "category": "domain-knowledge", "title": "X", "body": "Y",
           "schema": "komi.learning/1"}            # no visibility field (pre-feature record)
    l = Learning.from_dict(old)
    assert l.visibility == Visibility.SHAREABLE.value
    # and the id is unchanged vs a freshly-finalized equivalent
    fresh = Learning(type="semantic", category="domain-knowledge", title="X", body="Y").finalize()
    assert Learning.from_dict(old).finalize().id == fresh.id


# ── confidential floor → forces private, bars global ──────────────────────────

# real-world confidential samples (the exact classes that leaked)
_CONFIDENTIAL = [
    "Stripe Atlas defaults: 10M authorized shares of Common; sole founder receives ~9M shares.",
    "Our cap table lives in Carta. Option pool is 1M unissued.",
    "We're raising a seed round; the angel investor offered a SAFE note at a $5M valuation.",
    "Q3 ARR target is $40k; current burn rate is manageable.",
    "Strategic moat vs Anthropic: they won't open-source the community pool.",
    "Engineer salary band and equity compensation for the first hire.",
]
# shareable craft that must NOT be flagged
_SHAREABLE = [
    "Russian has 3-form plural rules; mirror the EN choice to drop the noun in UI strings.",
    "No hardcoded Color(0xFF...) literals; define semantic colors in Tokens.kt.",
    "Use pytest fixtures for shared setup; prefer parametrize over loops.",
    "Turkish nouns stay singular after numbers.",
]


@pytest.mark.parametrize("text", _CONFIDENTIAL)
def test_confidential_floor_flags(text):
    assert safety_floor(text).confidential is True


@pytest.mark.parametrize("text", _SHAREABLE)
def test_shareable_not_flagged_confidential(text):
    assert safety_floor(text).confidential is False


@pytest.mark.parametrize("text", _CONFIDENTIAL)
def test_confidential_forced_private_and_never_global(text):
    L = Learning(type="semantic", category="domain-knowledge", title="t", body=text)
    # even with a judge screaming "global", confidential can't reach the pool
    c = classify(L, judge=lambda l, context: {"scope": "global", "rationale": "x"})
    assert c.visibility == Visibility.PRIVATE.value
    assert c.scope != Scope.GLOBAL.value


def test_shareable_craft_can_globalize():
    L = Learning(type="semantic", category="tooling",
                 title="pytest fixtures", body="Use fixtures for shared setup.")
    c = classify(L, judge=lambda l, context: {
        "scope": "global", "visibility": "shareable",
        "generalized_title": "Use pytest fixtures",
        "generalized_body": "Use fixtures for shared setup.", "rationale": "general"})
    assert c.scope == Scope.GLOBAL.value
    assert c.visibility == Visibility.SHAREABLE.value


def test_llm_can_mark_private_when_regex_misses():
    # paraphrased strategy with no confidential *keyword* — the LLM is the backstop
    L = Learning(type="semantic", category="domain-knowledge", title="growth plan",
                 body="We will quietly enter the European market next quarter and undercut on price.")
    assert safety_floor(L.body).confidential is False     # regex misses it
    c = classify(L, judge=lambda l, context: {"scope": "global", "visibility": "private",
                                              "rationale": "confidential strategy"})
    assert c.visibility == Visibility.PRIVATE.value
    assert c.scope != Scope.GLOBAL.value


# ── storage routing: private → .local, shareable → committable ────────────────

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    yield s
    s.close()


def test_private_routes_to_local_file(store, tmp_path):
    pv = Learning(type="semantic", category="domain-knowledge", title="Cap table",
                  body="10M shares; 9M to founder.", visibility="private").finalize()
    sh = Learning(type="semantic", category="domain-knowledge", title="Token rule",
                  body="Use design tokens.", visibility="shareable").finalize()
    store.upsert(pv)
    store.upsert(sh)
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    memlocal = (tmp_path / "MEMORY.local.md").read_text(encoding="utf-8")
    assert "Token rule" in mem and "Cap table" not in mem        # shareable file is clean
    assert "Cap table" in memlocal                               # private went local
    # recall still sees BOTH locally
    assert {"Cap table", "Token rule"} <= {l.title for l in store.all()}


def test_private_skill_routes_to_skills_local(store, tmp_path):
    ps = Learning(type="procedural", category="workflow", title="Funding steps",
                  body="Pitch angels.", visibility="private").finalize()
    store.upsert(ps)
    assert (tmp_path / "skills.local").exists()
    assert not (tmp_path / "skills").exists()                    # no shareable skills dir created
    assert "Funding steps" in {l.title for l in store.all()}


def test_forget_hard_erases_private_entry(store, tmp_path):
    """`komi-learn forget --hard` (Store.delete) must cleanly erase a private entry
    from its .local file AND the index — the path for cleaning up a leaked cap table."""
    pv = Learning(type="semantic", category="domain-knowledge", title="Cap table",
                  body="10M shares; 9M to founder.", visibility="private").finalize()
    store.upsert(pv)
    assert store.delete(pv.id) is True
    assert "Cap table" not in {l.title for l in store.all()}
    # index row gone too
    rows = list(store._db.execute("SELECT 1 FROM learnings WHERE id=?", (pv.id,)))
    assert rows == []


# ── safe-by-default .gitignore ────────────────────────────────────────────────

def test_store_writes_safe_gitignore(tmp_path):
    Store(tmp_path).close()
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    ignore_lines = [l.strip() for l in gi.splitlines() if l.strip() and not l.strip().startswith("#")]
    for must in ("*.local.md", "skills.local/", "index.db", "keys/", "state.json", ".env"):
        assert must in ignore_lines, f"{must} not ignored"
    # the shareable files must NOT be ignored — committing them is the point
    for shareable in ("MEMORY.md", "USER.md", "skills/"):
        assert shareable not in ignore_lines, f"{shareable} should be committable"


def test_gitignore_idempotent_keeps_user_custom(tmp_path):
    (tmp_path / ".gitignore").write_text("CUSTOM USER CONTENT", encoding="utf-8")
    Store(tmp_path).close()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "CUSTOM USER CONTENT"


# ── verify.py parity: confidential blocked from the pool too ──────────────────

def test_vendored_verify_blocks_confidential():
    """The pool CI verifier must reject confidential content, matching the engine —
    a private learning can never reach the public pool even if mis-tagged."""
    import importlib.util
    from pathlib import Path
    p = (Path(__file__).resolve().parents[1] / "pool-repo-template" / ".github" / "scripts" / "verify.py")
    spec = importlib.util.spec_from_file_location("vendored_verify_vis", p)
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    for text in _CONFIDENTIAL:
        assert "business-confidential" in v.scrub_problems(text), text
        assert safety_floor(text).blocked == bool(v.scrub_problems(text))   # parity
