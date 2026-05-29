"""Regression tests for the issues found in the adversarial code review.

Each test pins a specific defect that was found and fixed, so it can't silently
come back. Grouped by the fix.
"""

import importlib.util
import time
from pathlib import Path

import pytest

from komi.engine.model import Learning, LearningType, Category, Scope, verify_id
from komi.engine.store import Store
from komi.engine import classify as clf


# ── #38: reindex must NOT wipe usage telemetry ──────────────────────────────

def test_reindex_preserves_usage(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(Learning(type=LearningType.SEMANTIC.value, category=Category.TOOLING.value,
                            title="t", body="b", trigger="w", tags=["x"]).finalize())
    s._db.execute("UPDATE learnings SET reused=7, last_used='2026-01-01T00:00:00Z' WHERE id=?", (lid,))
    s._db.commit()
    s.reindex()
    row = next(r for r in s.rows() if r["id"] == lid)
    assert row["reused"] == 7                          # was being zeroed
    assert row["last_used"] == "2026-01-01T00:00:00Z"


def test_reindex_preserves_skill_usage(tmp_path):
    s = Store(tmp_path)
    lid = s.upsert(Learning(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                            title="skill", body="b", trigger="w", tags=["x"]).finalize())
    s._db.execute("UPDATE learnings SET reused=3 WHERE id=?", (lid,))
    s._db.commit()
    s.reindex()
    assert next(r for r in s.rows() if r["id"] == lid)["reused"] == 3


# ── #39: WAL + busy_timeout enabled ─────────────────────────────────────────

def test_wal_mode_enabled(tmp_path):
    s = Store(tmp_path)
    assert s._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# ── #44a: skill dir is id-keyed, title edit doesn't orphan ──────────────────

def test_skill_dir_is_id_keyed(tmp_path):
    s = Store(tmp_path)
    s.upsert(Learning(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                      title="My Long Skill Title", body="b", trigger="w", tags=["x"]).finalize())
    dirs = [p.name for p in (tmp_path / "skills").iterdir()]
    assert len(dirs) == 1
    # dir name must NOT contain the title slug — purely id-keyed
    assert "my-long-skill" not in dirs[0]


def test_read_skills_dedups_by_id(tmp_path):
    s = Store(tmp_path)
    lng = Learning(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                   title="t", body="b", trigger="w", tags=["x"]).finalize()
    # simulate a legacy duplicate: write the same id under a second (slug-style) dir
    s.upsert(lng)
    legacy = tmp_path / "skills" / "legacy-slug-dir"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text((tmp_path / "skills").glob("*/SKILL.md").__next__().read_text(encoding="utf-8"),
                                     encoding="utf-8")
    seen = [l for l in s.all() if l.id == lng.id]
    assert len(seen) == 1                              # deduped, not two copies


# ── #41: signature covers origin + signer identity ──────────────────────────

def test_signature_covers_origin_and_identity(tmp_path):
    from komi.pool.identity import Contributor
    from komi.pool.contribute import prepare_contribution, ingest_verify
    import copy
    c = Contributor(tmp_path / "k")
    g = Learning(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                 title="t", body="b", trigger="w", tags=["x"], scope=Scope.GLOBAL.value).finalize()
    env = prepare_contribution(g, c).envelope
    rs = (c.algo == "ed25519")
    assert ingest_verify(env, require_signature=rs).accepted is True
    # origin tamper rejected
    e2 = copy.deepcopy(env); e2["learning"]["provenance"]["origin"] = "agent:FORGED"
    assert ingest_verify(e2, require_signature=rs).accepted is False
    # signer-pubkey swap rejected (no replay under another identity)
    c2 = Contributor(tmp_path / "k2")
    e3 = copy.deepcopy(env); e3["signer"]["public_key"] = c2.public_key
    assert ingest_verify(e3, require_signature=rs).accepted is False


# ── #42: expanded secret/identifier detectors ──────────────────────────────

@pytest.mark.parametrize("secret", [
    "AIzaSyDaIfnotarealkeybutstilllikeit12345",       # Google API key
    "sk_live_abcdefghijklmnopqrstuvwx",               # Stripe live key (underscore)
    "SG.abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuv",  # SendGrid
    "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoP",          # GitHub fine-grained PAT
    "npm_abcdefghijklmnopqrstuvwxyz0123456789",       # npm token
    "redis://user:s3cretpw@cache.example.com:6379",   # connection string w/ password
    "hf_abcdefghijklmnopqrstuvwxyz",                  # HuggingFace
])
def test_secret_patterns_catch_more(secret):
    assert clf.safety_floor(f"the value is {secret}").secret is True


@pytest.mark.parametrize("ident", [
    "ssh to 2001:0db8:85a3:0000:0000:8a2e:0370:7334",  # IPv6
    "see http://[fe80::1]/admin",                       # IPv6 URL
    "/root/secretproject/config",                       # root home path
    "visit deadbeefcafe1234.onion for the service",     # tor
])
def test_identifier_patterns_catch_more(ident):
    fl = clf.safety_floor(ident)
    assert fl.blocked is True


# ── #43: from_dict normalizes empty tags ────────────────────────────────────

def test_from_dict_strips_empty_tags():
    a = Learning(type="procedural", category="tooling", title="t", body="b",
                 trigger="w", tags=["pytest", "debug"]).finalize()
    # a hand-edited record with blank tags must hash identically to the clean one
    d = a.to_dict(); d["tags"] = ["pytest", "", "  ", "debug"]
    b = Learning.from_dict(d).finalize()
    assert b.id == a.id


# ── #43: pull defaults to require_signature=True ────────────────────────────

def test_pull_requires_signature_by_default():
    import inspect
    from komi.pool.contribute import pull
    assert inspect.signature(pull).parameters["require_signature"].default is True


# ── #43: contribution size cap ──────────────────────────────────────────────

def test_oversized_contribution_rejected(tmp_path):
    from komi.pool.identity import Contributor
    from komi.pool.contribute import prepare_contribution, MAX_CONTRIBUTION_CHARS
    c = Contributor(tmp_path / "k")
    big = Learning(type="procedural", category="tooling", title="t",
                   body="A" * (MAX_CONTRIBUTION_CHARS + 100), trigger="w", tags=["x"],
                   scope=Scope.GLOBAL.value).finalize()
    r = prepare_contribution(big, c)
    assert r.ok is False and "too-large" in r.reason


# ── #40: state.json concurrent updates don't lose data ──────────────────────

def test_update_state_atomic_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths as P
    importlib.import_module("komi.adapters.claude_code.paths")
    # two independent keys updated in sequence both survive (no clobber)
    P.update_state(lambda s: s.__setitem__("a", 1))
    P.update_state(lambda s: s.__setitem__("b", 2))
    import json
    st = json.loads((tmp_path / "komi" / "state.json").read_text(encoding="utf-8"))
    assert st == {"a": 1, "b": 2}


def test_update_state_tolerates_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths as P
    sp = P.state_path(); sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("{ this is not json", encoding="utf-8")     # corrupt
    result = P.update_state(lambda s: s.setdefault("ok", True))
    assert result is True                                     # recovered, didn't crash


# ── #45: vendored CI verifier must mirror the engine ────────────────────────

def _load_vendored_verify():
    p = (Path(__file__).resolve().parents[1]
         / "pool-repo-template" / ".github" / "scripts" / "verify.py")
    spec = importlib.util.spec_from_file_location("vendored_verify", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vendored_canonical_json_matches_engine():
    from komi.engine.model import canonical_json
    v = _load_vendored_verify()
    sample = {"b": 2, "a": [1, "x"], "u": "café"}
    assert v.canonical_json(sample) == canonical_json(sample)


def test_vendored_verify_id_matches_engine():
    v = _load_vendored_verify()
    g = Learning(type="procedural", category="tooling", title="t", body="b",
                 trigger="w", tags=["x"], scope=Scope.GLOBAL.value).finalize()
    rec = g.publishable()
    assert v.verify_id(rec) == verify_id(rec) is True


def test_vendored_detectors_match_engine():
    """The vendored scrub must flag exactly what the engine flags on a battery of
    inputs — drift here = CI accepting what the engine rejects (a real hole)."""
    v = _load_vendored_verify()
    samples = [
        "AIzaSyDaIfnotarealkeybutstilllikeit12345",
        "redis://user:pw@host:6379",
        "/root/x", "http://[fe80::1]/", "x.onion",
        "just a normal general learning about pytest",
        "email me at a@b.com", "sk_live_abcdefghijklmnopqr",
        r"C:\Users\bob\proj",
    ]
    for s in samples:
        engine_blocked = clf.safety_floor(s).blocked
        vendored_blocked = bool(v.scrub_problems(s))
        assert engine_blocked == vendored_blocked, f"drift on: {s!r}"


def test_vendored_signing_message_matches_engine():
    v = _load_vendored_verify()
    from komi.pool.contribute import _signing_message
    g = Learning(type="procedural", category="tooling", title="t", body="b",
                 trigger="w", tags=["x"], scope=Scope.GLOBAL.value).finalize()
    rec = g.publishable()
    rec["provenance"]["origin"] = "agent:test"
    assert v._signing_message(rec, "PUBKEY") == _signing_message(rec, signer_public_key="PUBKEY")
