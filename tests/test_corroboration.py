"""Phase 5b — corroboration-based trust.

Corroboration = how many DISTINCT contributors independently signed the SAME
content-addressed learning. These tests cover the whole path:

  • counting distinct VALID signers (and rejecting bogus/duplicate ones)
  • appending a second signer to an existing file (merge_signature)
  • the min_corroboration gate in pull (stubbed + GitHub-backed local)
  • the .md render/parse round-trip carrying a signatures array
  • recall ranking: a well-corroborated pool item beats an equally-relevant
    single-signer one, but corroboration never overrides relevance
  • parity: the vendored CI verifier counts corroboration like the engine

Runs fully offline. Signature tests are skipped automatically if PyNaCl is
absent (unsigned mode) — there's nothing to verify there.
"""

import copy
import importlib.util
import tempfile
from pathlib import Path

import pytest

from komi.engine.model import Learning, LearningType, Category, Scope
from komi.engine.store import Store
from komi.engine.recall import _rank_score
from komi.pool.identity import Contributor
from komi.pool import contribute as C
from komi.pool import corroboration as corro
from komi.pool.repo_format import render_md, parse_md
from komi.pool.github_backend import GitHubPool, PoolConfig


def _detect_nacl() -> bool:
    with tempfile.TemporaryDirectory() as d:
        return Contributor(Path(d) / "probe").algo == "ed25519"


HAVE_NACL = _detect_nacl()
needs_nacl = pytest.mark.skipif(not HAVE_NACL, reason="PyNaCl not installed (unsigned mode)")


def _learning(**kw) -> Learning:
    base = dict(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                title="Prefer rg over grep -r", body="ripgrep is faster and respects .gitignore.",
                trigger="code search", tags=["ripgrep"], scope=Scope.GLOBAL.value)
    base.update(kw)
    return Learning(**base).finalize()


def _envelope(c: Contributor, lng: Learning, github_user: str = "") -> dict:
    """A signed envelope from one contributor (signatures array, length 1)."""
    return C.prepare_contribution(lng, c, github_user=github_user).envelope


def _co_sign(envelope: dict, c2: Contributor, github_user: str = "") -> dict:
    """Produce the signature a SECOND contributor would make over the same learning
    and merge it in — exactly what publish() does on an already-present file."""
    pub = envelope["learning"]
    gh = github_user.strip().lstrip("@")
    msg = C._signing_message(pub, signer_public_key=c2.public_key, signer_github_user=gh)
    sig = c2.sign(msg)
    new_sig = {"algo": c2.algo, "public_key": c2.public_key, "signature": sig,
               "github_user": gh}
    return corro.merge_signature(envelope, new_sig)


# ── distinct-signer counting ─────────────────────────────────────────────────

@needs_nacl
def test_single_signer_corroboration_is_one(tmp_path):
    c = Contributor(tmp_path / "k")
    rep = C.ingest_verify(_envelope(c, _learning()), require_signature=True)
    assert rep.accepted and rep.corroboration == 1


@needs_nacl
def test_two_distinct_signers_corroboration_is_two(tmp_path):
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _co_sign(_envelope(c1, _learning()), c2)
    rep = C.ingest_verify(env, require_signature=True)
    assert rep.accepted and rep.corroboration == 2
    # both legacy mirror + array agree on the primary signer
    assert env["signer"]["public_key"] == c1.public_key
    assert {s["public_key"] for s in env["signatures"]} == {c1.public_key, c2.public_key}


@needs_nacl
def test_same_signer_twice_does_not_inflate(tmp_path):
    c1 = Contributor(tmp_path / "k1")
    env = _envelope(c1, _learning())
    # a second endorsement by the SAME signer is a no-op (merge returns None)
    assert _co_sign(env, c1) is None
    # and even a hand-crafted duplicate entry is de-duped to corroboration 1
    env2 = copy.deepcopy(env)
    env2["signatures"] = env2["signatures"] + env2["signatures"]
    assert C.ingest_verify(env2, require_signature=True).corroboration == 1


@needs_nacl
def test_bogus_signature_does_not_count(tmp_path):
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _co_sign(_envelope(c1, _learning()), c2)
    # corrupt the second signer's signature → only the first counts
    env["signatures"][1]["signature"] = "AAAA" + env["signatures"][1]["signature"][4:]
    rep = C.ingest_verify(env, require_signature=True)
    assert rep.corroboration == 1


# ── min_corroboration gate ───────────────────────────────────────────────────

@needs_nacl
def test_malformed_file_does_not_crash_pull(tmp_path):
    """HIGH-1 regression: a pool envelope that parses but lacks `id` must be SKIPPED,
    not crash the whole pull (which would silently disable all community recall)."""
    import json as _json
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    c = Contributor(tmp_path / "k")
    # one good learning
    C.publish(_envelope(c, _learning(title="good", body="a fine general tip")), outbox)
    # one malformed: learning present but NO id (would KeyError in _signing_message)
    (outbox / "broken.json").write_text(
        _json.dumps({"envelope": "komi.pool/1",
                     "learning": {"type": "semantic", "title": "x", "body": "y"},
                     "signatures": []}),
        encoding="utf-8")
    pulled = C.pull(outbox, require_signature=True)   # must not raise
    assert [l.title for l in pulled] == ["good"]      # good kept, broken skipped


@needs_nacl
def test_pull_gate_filters_below_threshold(tmp_path):
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    outbox = tmp_path / "outbox"
    # one single-signer learning, one two-signer learning
    single = _envelope(c1, _learning(title="single", body="a lone tip about tmux splits"))
    pair = _co_sign(_envelope(c1, _learning(title="pair", body="a corroborated tip about vim")), c2)
    C.publish(single, outbox)
    C.publish(pair, outbox)

    all_pulled = C.pull(outbox, require_signature=True, min_corroboration=1)
    assert {l.title for l in all_pulled} == {"single", "pair"}
    assert {l.title: l.corroboration for l in all_pulled} == {"single": 1, "pair": 2}

    only_corroborated = C.pull(outbox, require_signature=True, min_corroboration=2)
    assert [l.title for l in only_corroborated] == ["pair"]


@needs_nacl
def test_github_pull_attaches_corroboration_and_gates(tmp_path):
    """End-to-end on the real GitHubPool (local mode): a second contributor's
    publish() APPENDS a signature to the same file, raising its corroboration."""
    cache = tmp_path / "repo"
    pool = GitHubPool(PoolConfig(cache_dir=str(cache), mode="local", branch="main",
                                 require_signature=True, min_corroboration=1))
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    lng = _learning()

    r1 = pool.publish(_envelope(c1, lng))
    assert r1.ok and r1.extra.get("action") == "learn"
    # second contributor independently distills + signs the same lesson
    r2 = pool.publish(_envelope(c2, lng))
    assert r2.ok and r2.extra.get("action") == "corroborate"
    # a third publish by c1 again is a true no-op
    r3 = pool.publish(_envelope(c1, lng))
    assert r3.ok and r3.extra.get("noop") is True

    pulled = pool.pull()
    assert len(pulled) == 1 and pulled[0].corroboration == 2

    # raising the gate to 3 now filters it out
    pool.cfg.min_corroboration = 3
    assert pool.pull() == []


# ── .md round-trip with a signatures array ───────────────────────────────────

@needs_nacl
def test_render_parse_roundtrip_preserves_signatures(tmp_path):
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _co_sign(_envelope(c1, _learning()), c2)
    md = render_md(env)
    assert "corroborated" in md.lower()                 # human header shows it
    parsed = parse_md(md)
    assert C.ingest_verify(parsed, require_signature=True).corroboration == 2


# ── recall ranking bonus ─────────────────────────────────────────────────────

def test_rank_corroboration_breaks_ties_for_community():
    """Equal relevance/recency/confidence: the more-corroborated pool item ranks
    higher. A personal item (corroboration 1) gets no bonus."""
    base = dict(reused=0, confidence=0.5, updated_at="", scope="global")
    low = {**base, "corroboration": 1}
    high = {**base, "corroboration": 5}
    assert _rank_score(high, 0.5) > _rank_score(low, 0.5)


def test_rank_corroboration_never_overrides_relevance():
    """A highly-corroborated but IRRELEVANT item must not outrank a highly-relevant
    one. The bonus is a tie-breaker, capped well below the relevance weight."""
    relevant = {"reused": 0, "confidence": 0.5, "updated_at": "", "scope": "global",
                "corroboration": 1}
    corroborated_irrelevant = {"reused": 0, "confidence": 0.5, "updated_at": "",
                               "scope": "global", "corroboration": 50}
    assert _rank_score(relevant, 0.95) > _rank_score(corroborated_irrelevant, 0.10)


def test_store_carries_corroboration_into_index(tmp_path):
    """mirror_external must persist the corroboration count so recall can read it."""
    s = Store(tmp_path)
    g = _learning()
    g.corroboration = 4
    s.mirror_external([g], source="pool")
    row = next(r for r in s.rows() if r["id"] == g.id)
    assert row["corroboration"] == 4


# ── CI parity: vendored verifier counts corroboration like the engine ─────────

def _load_vendored_verify():
    p = (Path(__file__).resolve().parents[1]
         / "pool-repo-template" / ".github" / "scripts" / "verify.py")
    spec = importlib.util.spec_from_file_location("vendored_verify_corro", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@needs_nacl
def test_template_seed_files_still_verify():
    """Guard against seed-signature rot: every seed `.md` shipped in the pool
    template must validate under the current scheme. This is exactly what a fresh
    pool's CI runs on its first commit — if it's red here, new pools ship broken.
    (Signing-scheme changes require re-running .github/scripts/resign_seeds.py.)"""
    seeds = (Path(__file__).resolve().parents[1] / "pool-repo-template" / "learnings")
    files = sorted(seeds.rglob("*.md"))
    assert files, "no template seed files found"
    for f in files:
        env = parse_md(f.read_text(encoding="utf-8"))
        assert env is not None, f"unparseable seed: {f.name}"
        rep = C.ingest_verify(env, require_signature=True)
        assert rep.accepted, f"seed {f.name} fails verification: {rep.reasons}"
        assert rep.corroboration >= 1


@needs_nacl
def test_vendored_corroboration_matches_engine(tmp_path):
    v = _load_vendored_verify()
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _co_sign(_envelope(c1, _learning()), c2)

    # same distinct-signer normalization
    assert v.envelope_signatures(env) == corro.envelope_signatures(env)
    # same valid-signer count, via the vendored crypto
    valid, problems = v.signature_problems(env)
    assert valid == 2 and not problems
    # a bogus signature is a hard failure in CI (must never carry an invalid sig)
    env["signatures"][1]["signature"] = "AAAA" + env["signatures"][1]["signature"][4:]
    valid2, problems2 = v.signature_problems(env)
    assert valid2 == 1 and problems2     # one still valid, but the bad one is reported


@needs_nacl
def test_vendored_verify_signature_matches_engine(tmp_path):
    """Parity hole the architect flagged: the vendored verify.py re-implements
    verify_signature independently of identity.verify_signature. Pin that they agree
    on a matrix of {valid, wrong-key, tampered-msg, empty}."""
    v = _load_vendored_verify()
    from komi.pool.identity import verify_signature as eng_verify
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    msg = b"the signed bytes"
    sig = c1.sign(msg)
    cases = [
        (msg, sig, c1.public_key),          # valid
        (msg, sig, c2.public_key),          # wrong key
        (b"tampered", sig, c1.public_key),  # tampered message
        (msg, "", c1.public_key),           # empty sig
        (msg, sig, ""),                     # empty key
    ]
    for m, s, pk in cases:
        assert v.verify_signature(m, s, pk) == eng_verify(m, s, pk)


@needs_nacl
def test_count_asymmetry_consumer_vs_ci(tmp_path):
    """Pin the DELIBERATE asymmetry (ADR-9): when all signatures are valid, the
    consumer's count == the CI gate's valid count. When one is invalid, the consumer
    still ACCEPTS (counts the valid ones) while CI REPORTS A PROBLEM (fails). A
    refactor that made the consumer strict, or the gate lenient, must break here."""
    v = _load_vendored_verify()
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _co_sign(_envelope(c1, _learning()), c2)

    # all valid: consumer count == CI valid count, CI has no problems
    consumer = C.ingest_verify(env, require_signature=True)
    ci_valid, ci_problems = v.signature_problems(env)
    assert consumer.corroboration == ci_valid and not ci_problems

    # one invalid: consumer still accepts (counts the 1 valid), CI flags a problem
    env["signatures"][1]["signature"] = "AAAA" + env["signatures"][1]["signature"][4:]
    consumer2 = C.ingest_verify(env, require_signature=True)
    ci_valid2, ci_problems2 = v.signature_problems(env)
    assert consumer2.accepted and consumer2.corroboration == 1   # lenient consumer
    assert ci_valid2 == 1 and ci_problems2                       # strict CI gate


@needs_nacl
def test_append_only_check_blocks_signer_removal(tmp_path):
    """The CI append-only check must reject a modified file that drops a prior signer
    (corroboration downgrade / signer replacement), but allow adding signers."""
    v = _load_vendored_verify()
    c1, c2, c3 = (Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2"),
                  Contributor(tmp_path / "k3"))
    base = _co_sign(_envelope(c1, _learning()), c2)         # 2 signers
    grown = _co_sign(base, c3)                              # 3 signers (append)
    downgraded = _envelope(c1, _learning())                # back to 1 signer

    assert v.assert_append_only(base, grown) == []          # adding is fine
    assert v.assert_append_only(base, base) == []           # no change is fine
    assert v.assert_append_only(base, downgraded)           # dropping c2 → problem


@needs_nacl
def test_signature_flood_is_clamped_and_bounded(tmp_path):
    """Sybil/DoS interim mitigation: a huge signatures array is bounded
    (MAX_SIGNATURES) and the counted corroboration is clamped (MAX_COUNTED_SIGNERS)."""
    c = Contributor(tmp_path / "k")
    env = _envelope(c, _learning())
    real = env["signatures"][0]
    # pad with 5000 junk (invalid) entries + the one real signer
    env["signatures"] = [real] + [
        {"algo": "ed25519", "public_key": f"fakekey{i}", "signature": "AA=="} for i in range(5000)
    ]
    # envelope_signatures never returns more than MAX_SIGNATURES
    assert len(corro.envelope_signatures(env)) <= corro.MAX_SIGNATURES
    # count is clamped and only the real signer is valid here → 1
    rep = C.ingest_verify(env, require_signature=True)
    assert rep.corroboration <= corro.MAX_COUNTED_SIGNERS and rep.corroboration == 1


# ── Phase 7: signer↔GitHub-account binding (Sybil resistance) ────────────────

@needs_nacl
def test_github_user_bound_into_signature(tmp_path):
    """The username is inside the signed bytes: a valid signature verifies, but
    swapping the github_user (without re-signing) breaks verification."""
    c = Contributor(tmp_path / "k")
    env = _envelope(c, _learning(), github_user="alice")
    assert env["signatures"][0]["github_user"] == "alice"
    assert C.ingest_verify(env, require_signature=True).corroboration == 1
    # tamper the username → signature no longer matches → not counted
    env["signatures"][0]["github_user"] = "mallory"
    assert C.ingest_verify(env, require_signature=True).corroboration == 0


def _sign_entry(c: Contributor, learning: dict, github_user: str) -> dict:
    """One signature entry over `learning` by contributor `c` as `github_user`."""
    gh = github_user.strip().lstrip("@")
    msg = C._signing_message(learning, signer_public_key=c.public_key, signer_github_user=gh)
    e = {"algo": c.algo, "public_key": c.public_key, "signature": c.sign(msg)}
    if gh:
        e["github_user"] = gh
    return e


@needs_nacl
def test_sybil_one_account_many_keys_counts_once(tmp_path):
    """The core Sybil fix: one person minting N keys, all signing under ONE GitHub
    account, counts as 1 — distinctness is by account, not key. We assemble the
    signatures array directly (merge_signature would refuse to even append a 2nd
    same-account key — itself part of the defense; tested separately)."""
    env = _envelope(Contributor(tmp_path / "k0"), _learning(), github_user="attacker")
    learning = env["learning"]
    # 4 MORE distinct keys, all claiming the same account, each a VALID signature
    for i in range(1, 5):
        ci = Contributor(tmp_path / f"k{i}")
        env["signatures"].append(_sign_entry(ci, learning, "attacker"))
    assert len(env["signatures"]) == 5                  # 5 valid sigs, 5 distinct keys
    # ...but ONE account → corroboration collapses to 1
    assert C.ingest_verify(env, require_signature=True).corroboration == 1


@needs_nacl
def test_distinct_accounts_count_distinctly(tmp_path):
    """Two genuinely different GitHub accounts → corroboration 2."""
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _co_sign(_envelope(c1, _learning(), github_user="alice"), c2, github_user="bob")
    assert C.ingest_verify(env, require_signature=True).corroboration == 2


@needs_nacl
def test_merge_same_account_is_noop(tmp_path):
    """Appending a second key under an account that already endorses is a no-op."""
    c1, c2 = Contributor(tmp_path / "k1"), Contributor(tmp_path / "k2")
    env = _envelope(c1, _learning(), github_user="alice")
    assert _co_sign(env, c2, github_user="alice") is None   # same account → no-op


@needs_nacl
def test_legacy_unbound_signatures_still_verify(tmp_path):
    """Back-compat: a signature made with NO github_user (pre-Phase-7, and the
    seeds) still verifies and counts by key — the scheme didn't break."""
    c = Contributor(tmp_path / "k")
    env = _envelope(c, _learning())                  # no github_user
    assert "github_user" not in env["signatures"][0]
    assert C.ingest_verify(env, require_signature=True).corroboration == 1


@needs_nacl
def test_vendored_verify_handles_github_user(tmp_path):
    """Parity: the CI verifier counts account-bound signatures exactly like the
    engine (one account/many keys → 1; swapped username → invalid)."""
    v = _load_vendored_verify()
    base = _learning()
    env = _envelope(Contributor(tmp_path / "k1"), base, github_user="alice")
    # second valid key, same account, appended directly (merge would refuse it)
    env["signatures"].append(_sign_entry(Contributor(tmp_path / "k2"),
                                         env["learning"], "alice"))
    valid, problems = v.signature_problems(env)
    assert valid == 1 and not problems              # 2 keys, 1 account → 1
    # distinct accounts → 2, still parity with engine
    env2 = _co_sign(_envelope(Contributor(tmp_path / "k3"), base, github_user="x"),
                    Contributor(tmp_path / "k4"), github_user="y")
    v_valid, _ = v.signature_problems(env2)
    assert v_valid == C.ingest_verify(env2, require_signature=True).corroboration == 2


# ── CI identity gate: PR author must == the signature it adds ─────────────────

@needs_nacl
def test_ci_author_binding_rejects_signing_as_someone_else(tmp_path):
    """The hard CI gate (pure-function part): a PR that adds a signature under a
    github_user that isn't the PR author is rejected."""
    v = _load_vendored_verify()
    new_env = _envelope(Contributor(tmp_path / "k"), _learning(), github_user="alice")
    # PR author 'alice' adding alice's signature → OK
    assert v.check_author_binding(new_env, "alice", None) == []
    # PR author 'mallory' adding alice's signature → REJECTED
    probs = v.check_author_binding(new_env, "mallory", None)
    assert probs and "may only add YOUR OWN" in probs[0]


@needs_nacl
def test_ci_author_binding_only_checks_newly_added(tmp_path):
    """A PR that corroborates (adds bob to alice's existing learning) is checked
    only on bob — alice's pre-existing signature isn't re-attributed to the PR author."""
    v = _load_vendored_verify()
    base_env = _envelope(Contributor(tmp_path / "k1"), _learning(), github_user="alice")
    new_env = _co_sign(base_env, Contributor(tmp_path / "k2"), github_user="bob")
    # PR author bob, base had alice → only bob is "newly added", and bob == author → OK
    assert v.check_author_binding(new_env, "bob", base_env) == []
    # but if the PR author were carol (not bob), the added bob-sig is rejected
    assert v.check_author_binding(new_env, "carol", base_env)


def test_ci_newly_added_identities_diff():
    """newly_added_identities returns only accounts added vs base (no network)."""
    v = _load_vendored_verify()
    old = {"signatures": [{"public_key": "k1", "signature": "s", "github_user": "alice"}]}
    new = {"signatures": [
        {"public_key": "k1", "signature": "s", "github_user": "alice"},
        {"public_key": "k2", "signature": "s2", "github_user": "bob"},
    ]}
    assert v.newly_added_identities(old, new) == ["bob"]
    assert set(v.newly_added_identities(None, new)) == {"alice", "bob"}
