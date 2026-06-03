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


def test_gitignore_additive_preserves_user_lines_and_adds_required(tmp_path):
    # An existing (unrelated) .gitignore must be PRESERVED, with the required komi
    # patterns APPENDED — not skipped (which would leave private files committable).
    (tmp_path / ".gitignore").write_text("CUSTOM USER CONTENT\n", encoding="utf-8")
    Store(tmp_path).close()
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "CUSTOM USER CONTENT" in gi                 # user's line kept
    for must in ("*.local.md", "skills.local/", "keys/", "index.db"):
        assert must in gi                              # required lines added


def test_gitignore_no_duplicate_on_second_init(tmp_path):
    Store(tmp_path).close()
    Store(tmp_path).close()                            # second construction
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gi.count("skills.local/") == 1              # idempotent — no duplicate appends


def test_gitignore_has_recursive_local_glob(tmp_path):
    Store(tmp_path).close()
    assert "**/*.local.md" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


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


# ── review-gap tests (added after the 3-persona review BLOCK) ─────────────────

# Precision battery: ordinary engineering prose that must NOT be flagged confidential.
_FALSE_POSITIVE_PROBES = [
    "split the work into 4 shares across workers",
    "the GraphQL schema exposes preferred shares as a field",
    "the issued shares table in postgres",
    "the cache has a vesting period before eviction",
    "the dropdown stock options list",
    "lazy valuation of the expression tree",
    "the time series A/B test bucket",
    "plot series a against series b",
    "leave a safe note in the code about thread-safety",
    "the animation runway slides in",
    "the GPU burn rate during the stress test",
    "the gross margin of the layout box",
    "compensation for clock drift",
    "after the merger of the two git branches",
    "the M&A module handles mappings and aggregations",
    "do due diligence on the dependency licenses",
    "our competitive advantage over the baseline model is speed",
    "set the revenue of the mock store fixture to 100",
]
# Recall battery: plain-language confidential the OLD patterns missed.
_FALSE_NEGATIVE_PROBES = [
    "the founder keeps 90% of the company, investors split the rest",
    "we made about forty thousand dollars in revenue last quarter",
    "the company valuation is around 5 million dollars",
    "Google approached us about buying the company",
    "we are in talks to sell the company to a competitor",
    "our monthly recurring revenue is 12000",
    "the founder owns 80% of the company",
    "first hire gets 0.5% equity and a base salary of $150k",
    "we burn 50k a month",
    "we have 18 months of cash runway left",
    "raising a seed round; angel investor offered a SAFE note",
    "moat vs Anthropic: they won't open-source it",
]


@pytest.mark.parametrize("text", _FALSE_POSITIVE_PROBES)
def test_precision_no_false_positive(text):
    assert safety_floor(text).confidential is False, f"false positive: {text!r}"


@pytest.mark.parametrize("text", _FALSE_NEGATIVE_PROBES)
def test_recall_no_false_negative(text):
    assert safety_floor(text).confidential is True, f"missed confidential: {text!r}"


# ── no-LLM fail-safe default (the headline leak) ──────────────────────────────

def test_no_judge_defaults_private_not_shareable():
    """Regex-missed content with NO judge must default PRIVATE (fail safe), never
    shareable→committed."""
    L = Learning(type="semantic", category="domain-knowledge", title="plan",
                 body="We will quietly enter Europe next quarter and undercut on price.")
    c = classify(L, judge=None)
    assert c.visibility == Visibility.PRIVATE.value
    assert c.scope != Scope.GLOBAL.value


def test_nullllm_judge_defaults_private():
    from komi.adapters.claude_code.llm import NullLLM
    L = Learning(type="semantic", category="domain-knowledge", title="plan",
                 body="some general-sounding but unvetted content")
    c = classify(L, judge=NullLLM())
    assert c.visibility == Visibility.PRIVATE.value


def test_judge_global_is_exempt_from_failsafe():
    """A judge asserting global IS an explicit shareability judgment (global is
    impossible while private), so it must still globalize — not get failsafed."""
    L = Learning(type="semantic", category="tooling", title="t", body="general technique")
    c = classify(L, judge=lambda l, context: {"scope": "global", "category": l.category,
                                              "generalized_title": "t", "generalized_body": "general technique",
                                              "rationale": "general"})
    assert c.scope == Scope.GLOBAL.value
    assert c.visibility == Visibility.SHAREABLE.value


# ── model invariant: private ⇒ ¬global, enforced structurally ─────────────────

def test_finalize_demotes_global_private():
    L = Learning(type="semantic", category="domain-knowledge", title="x", body="y",
                 scope="global", visibility="private").finalize()
    assert L.scope == Scope.PROJECT.value and L.visibility == Visibility.PRIVATE.value


def test_from_dict_demotes_global_private():
    L = Learning.from_dict({"type": "semantic", "category": "domain-knowledge",
                            "title": "x", "body": "y", "scope": "global",
                            "visibility": "private", "schema": "komi.learning/1"})
    assert L.scope == Scope.PROJECT.value


# ── visibility flip = single residency (no leaked committable copy) ───────────

def test_flip_shareable_to_private_moves_not_duplicates(tmp_path):
    s = Store(tmp_path)
    sh = Learning(type="semantic", category="domain-knowledge", title="Note",
                  body="content", visibility="shareable").finalize()
    s.upsert(sh)
    pv = Learning(type="semantic", category="domain-knowledge", title="Note",
                  body="content", visibility="private").finalize()
    assert pv.id == sh.id
    s.upsert(pv)
    mem_path = tmp_path / "MEMORY.md"
    mem = mem_path.read_text(encoding="utf-8") if mem_path.exists() else ""
    assert "Note" not in mem                                   # moved out of committable
    assert "Note" in (tmp_path / "MEMORY.local.md").read_text(encoding="utf-8")
    assert [l.title for l in s.all()].count("Note") == 1       # surfaces once
    s.close()


# ── curator preserves visibility (no private→committable laundering) ──────────

def test_curator_umbrella_inherits_private_visibility():
    from komi.engine.curator import _build_umbrella
    members = [
        Learning(type="procedural", category="workflow", title="raise prep A",
                 body="pitch angels for the seed round", visibility="private").finalize(),
        Learning(type="procedural", category="workflow", title="raise prep B",
                 body="prepare the cap table", visibility="private").finalize(),
    ]
    merged = {"title": "fundraising prep", "body": "pitch angels and prep the cap table",
              "category": "workflow", "tags": []}
    u = _build_umbrella(merged, members)
    assert u.visibility == Visibility.PRIVATE.value            # most-restrictive wins


def test_curator_umbrella_refloors_merged_body():
    """Even if all members look shareable, a merge that surfaces confidential text
    must be forced private by the re-floor."""
    from komi.engine.curator import _build_umbrella
    members = [Learning(type="procedural", category="workflow", title="a", body="step one",
                        visibility="shareable").finalize()]
    merged = {"title": "ops", "body": "our monthly recurring revenue is 50000 so do X",
              "category": "workflow", "tags": []}
    u = _build_umbrella(merged, members)
    assert u.visibility == Visibility.PRIVATE.value


# ── reclassify migration ──────────────────────────────────────────────────────

def test_reclassify_moves_preexisting_confidential(tmp_path):
    s = Store(tmp_path)
    # simulate a pre-feature shareable learning that is actually confidential
    leak = Learning(type="semantic", category="domain-knowledge", title="cap table",
                    body="10M authorized shares; founder gets 9M", visibility="shareable").finalize()
    s.upsert(leak)
    moved = s.reclassify_visibility()
    assert leak.id in {m.id for m in moved}
    mem_path = tmp_path / "MEMORY.md"
    mem = mem_path.read_text(encoding="utf-8") if mem_path.exists() else ""
    assert "cap table" not in mem
    assert "cap table" in (tmp_path / "MEMORY.local.md").read_text(encoding="utf-8")
    s.close()


def test_reclassify_leaves_shareable_alone(tmp_path):
    s = Store(tmp_path)
    craft = Learning(type="semantic", category="tooling", title="pytest tip",
                     body="use fixtures for shared setup", visibility="shareable").finalize()
    s.upsert(craft)
    moved = s.reclassify_visibility()
    assert moved == []
    assert "pytest tip" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    s.close()


# ── suffix-anchored .local filename ───────────────────────────────────────────

def test_local_filename_suffix_anchored(tmp_path):
    s = Store(tmp_path)
    assert s._md_path("semantic", Visibility.PRIVATE.value).name == "MEMORY.local.md"
    assert s._md_path("identity", Visibility.PRIVATE.value).name == "USER.local.md"
    s.close()


# ── source-level detector parity (catches engine↔verify.py drift) ─────────────

def test_detector_patterns_byte_identical_with_verifier():
    """Every detector list must be byte-identical between the engine and the vendored
    pool CI verifier — a behavioral sample battery can't catch a drift in an
    individual pattern, but this does."""
    import importlib.util
    from pathlib import Path
    import komi.engine.classify as eng
    p = (Path(__file__).resolve().parents[1] / "pool-repo-template" / ".github" / "scripts" / "verify.py")
    spec = importlib.util.spec_from_file_location("vendored_verify_parity", p)
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    pairs = [
        (eng._SECRET_PATTERNS, v._SECRET),
        (eng._PII_PATTERNS, v._PII),
        (eng._IDENTIFIER_PATTERNS, v._IDENT),
        (eng._CONFIDENTIAL_PATTERNS, v._CONFIDENTIAL),
    ]
    for engine_set, vendored_set in pairs:
        assert [p.pattern for p in engine_set] == [p.pattern for p in vendored_set]
