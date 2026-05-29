"""GitHub-backed pool: .md format, git round-trip, dedup, tamper, CI verifier.

These run against a REAL local git repo (no network, no gh, no auth) so the whole
flow is verifiable today. The only thing the real GitHub deployment adds is the
remote transport + `gh pr create` step.
"""

import json
import subprocess

import pytest

from komi.engine.model import Learning, LearningType, Category, Scope
from komi.pool.identity import Contributor
from komi.pool.contribute import prepare_contribution
from komi.pool.repo_format import render_md, parse_md, repo_path_for, id_to_filename
from komi.pool.github_backend import GitHubPool, PoolConfig
from komi.pool import verify_cli


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not installed")


def G(c, **kw) -> dict:
    base = dict(type=LearningType.PROCEDURAL.value, category=Category.DEBUGGING.value,
                title="Read tracebacks bottom-up", body="Root cause is usually the deepest frame.",
                trigger="debugging a python exception", tags=["python"], scope=Scope.GLOBAL.value)
    base.update(kw)
    lng = Learning(**base).finalize()
    return prepare_contribution(lng, c).envelope


# ── format ────────────────────────────────────────────────────────────────

def test_md_roundtrip_preserves_verifiable_record(tmp_path):
    c = Contributor(tmp_path / "k")
    env = G(c)
    md = render_md(env)
    back = parse_md(md)
    assert back == env                                  # exact round-trip
    assert repo_path_for(env).startswith("learnings/debugging/")


def test_id_to_filename_is_path_safe():
    assert "/" not in id_to_filename("blake3:../../etc/passwd")
    assert id_to_filename("blake3:abc").endswith(".md")


# ── git round-trip ──────────────────────────────────────────────────────────

def _local_pool(tmp_path, c, name="cache") -> GitHubPool:
    pool = GitHubPool(PoolConfig(repo_url="", cache_dir=str(tmp_path / name),
                                 mode="local", require_signature=(c.algo == "ed25519")))
    pool._ensure_local_repo()
    return pool


def test_publish_then_pull_roundtrip(tmp_path):
    c = Contributor(tmp_path / "k")
    pool = _local_pool(tmp_path, c)
    r = pool.publish(G(c))
    assert r.ok, r.detail
    pulled = pool.pull()
    assert len(pulled) == 1
    assert pulled[0].scope == Scope.GLOBAL.value
    assert pulled[0].provenance.origin == "pool"


def test_publish_same_learning_is_noop(tmp_path):
    c = Contributor(tmp_path / "k")
    pool = _local_pool(tmp_path, c)
    env = G(c)
    pool.publish(env)
    r2 = pool.publish(env)                               # identical content → same path
    assert r2.ok and r2.extra.get("noop") is True
    assert len(pool.pull()) == 1                         # not duplicated


def test_category_sharding_and_filter(tmp_path):
    c = Contributor(tmp_path / "k")
    pool = _local_pool(tmp_path, c)
    pool.publish(G(c, category="debugging"))
    pool.publish(G(c, title="use rg", body="ripgrep is fast", category="tooling", tags=["rg"]))
    debug_only = pool.pull(categories=["debugging"])
    assert len(debug_only) == 1
    assert debug_only[0].category == "debugging"


def test_tampered_file_rejected_on_pull(tmp_path):
    c = Contributor(tmp_path / "k")
    pool = _local_pool(tmp_path, c)
    pool.publish(G(c))
    md = next((tmp_path / "cache" / "learnings").rglob("*.md"))
    md.write_text(md.read_text(encoding="utf-8").replace("deepest", "shallowest"),
                  encoding="utf-8")
    assert pool.pull() == []                             # integrity check fails → dropped


def test_scrub_blocks_publish_of_identifier(tmp_path):
    c = Contributor(tmp_path / "k")
    # A learning that slipped an identifier through must be blocked at contribution.
    lng = Learning(type="procedural", category="tooling", title="x",
                   body=r"run from C:\Users\bob\proj", trigger="t", tags=[],
                   scope="global").finalize()
    prep = prepare_contribution(lng, c)
    assert prep.ok is False
    assert "blocked-by-scrub" in prep.reason


# ── CI verifier ─────────────────────────────────────────────────────────────

def test_verify_cli_passes_clean_repo(tmp_path, capsys):
    c = Contributor(tmp_path / "k")
    pool = _local_pool(tmp_path, c, name="repo")
    pool.publish(G(c))
    rc = verify_cli.main(["--root", str(tmp_path / "repo"),
                          *([] if c.algo == "ed25519" else ["--no-signature"])])
    assert rc == 0


def test_verify_cli_fails_tampered_repo(tmp_path):
    c = Contributor(tmp_path / "k")
    pool = _local_pool(tmp_path, c, name="repo")
    pool.publish(G(c))
    md = next((tmp_path / "repo" / "learnings").rglob("*.md"))
    md.write_text(md.read_text(encoding="utf-8").replace("deepest", "shallowest"),
                  encoding="utf-8")
    rc = verify_cli.main(["--root", str(tmp_path / "repo"),
                          *([] if c.algo == "ed25519" else ["--no-signature"])])
    assert rc == 1                                       # CI would block the merge
