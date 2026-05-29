"""Pool: contribution scrub, content-addressed integrity, signature, pull."""

import json

from komi.engine.model import Learning, LearningType, Category, Scope
from komi.pool.identity import Contributor
from komi.pool.contribute import (
    prepare_contribution, publish, pull, ingest_verify, _signing_message,
)


def G(**kw) -> Learning:
    base = dict(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                title="Prefer rg over grep -r", body="ripgrep is faster, respects .gitignore.",
                trigger="code search", tags=["ripgrep"], scope=Scope.GLOBAL.value)
    base.update(kw)
    return Learning(**base).finalize()


def test_prepare_publish_pull_roundtrip(tmp_path):
    c = Contributor(tmp_path / "keys")
    r = prepare_contribution(G(), c)
    assert r.ok, r.reason
    outbox = tmp_path / "outbox"
    assert publish(r.envelope, outbox) is True
    pulled = pull(outbox, require_signature=(c.algo == "ed25519"))
    assert len(pulled) == 1
    assert pulled[0].scope == Scope.GLOBAL.value
    assert pulled[0].provenance.origin == "pool"


def test_scrub_blocks_machine_path(tmp_path):
    c = Contributor(tmp_path / "keys")
    r = prepare_contribution(G(body=r"run from C:\Users\bob\proj"), c)
    assert r.ok is False
    assert "blocked-by-scrub" in r.reason


def test_tampered_envelope_rejected(tmp_path):
    c = Contributor(tmp_path / "keys")
    r = prepare_contribution(G(), c)
    outbox = tmp_path / "outbox"
    publish(r.envelope, outbox)
    f = next(outbox.glob("*.json"))
    env = json.loads(f.read_text(encoding="utf-8"))
    env["learning"]["body"] = "rm -rf / everything"   # malicious post-sign edit
    rep = ingest_verify(env, require_signature=(c.algo == "ed25519"))
    assert rep.accepted is False
    assert rep.id_ok is False


def test_pull_category_filter(tmp_path):
    c = Contributor(tmp_path / "keys")
    outbox = tmp_path / "outbox"
    publish(prepare_contribution(G(category="tooling"), c).envelope, outbox)
    publish(prepare_contribution(G(title="other", body="a debugging trick for stack traces",
                                   category="debugging", tags=["debug"]), c).envelope, outbox)
    only_debug = pull(outbox, categories=["debugging"],
                      require_signature=(c.algo == "ed25519"))
    assert all(l.category == "debugging" for l in only_debug)
    assert len(only_debug) == 1
