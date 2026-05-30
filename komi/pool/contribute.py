"""komi-learn pool — contribution & consumption pipeline (PAM-style).

Contribution (local → pool):
    scrub → generalize-check → canonicalize → content-address → sign → HUMAN GATE → outbox

Consumption (pool → local), §7.3:
    pull → re-verify (hash + signature) → cache as scope=global (untrusted-origin)

The network is stubbed: ``publish`` writes signed envelopes to a local *outbox*
directory (what a future server would ingest), and ``pull`` reads from a local
*inbox*. The envelope format and every verification step are exactly what the
real server boundary will use — only the transport is missing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..engine.model import Learning, verify_id, canonical_json
from ..engine.classify import safety_floor
from .identity import Contributor, verify_signature
from .corroboration import count_corroboration

# A pool learning is a short, general lesson — not a document. Cap the publishable
# text so one contribution can't bloat the shared repo (anti-DoS). ~8KB is generous.
MAX_CONTRIBUTION_CHARS = 8000


# ── Envelope ──────────────────────────────────────────────────────────────
# What actually travels to/from the pool. The signature covers the canonical
# publishable content (the same bytes the id is derived from) plus parent_ids,
# so neither the content nor its DAG position can be altered post-signature.

def _signing_message(publishable: dict, *, signer_public_key: str = "",
                     signer_github_user: str = "") -> bytes:
    """The bytes the contributor signs. Covers the content id, the full content,
    the DAG parents, the provenance.origin, the signer's public key, AND (when
    present) the signer's GitHub username.

    Including ``origin`` stops post-signature attribution forgery (claiming a
    learning came from a more-trusted source). Binding ``signer_public_key`` stops
    a valid signature being replayed under a different identity. Binding
    ``signer_github_user`` (Phase 7) ties the signature to a GitHub ACCOUNT so
    corroboration counts distinct *people* (Sybil-resistant), and so a username
    can't be swapped post-signature. Everything that affects trust is inside the
    signature.

    BACK-COMPAT (critical): ``github_user`` is added to the signed root ONLY when
    non-empty. An empty username produces the *exact* pre-Phase-7 bytes, so every
    signature made before account-binding (and the seeds, and the unsigned path)
    still verifies byte-identically — no re-signing, no scheme break."""
    prov = publishable.get("provenance", {})
    # Never hard-subscript producer-controlled data: a pool file with a `learning`
    # object that parses but lacks `id` (or any content field) must produce a
    # verification FAILURE, not a KeyError that crashes the whole pull. .get() makes
    # a missing id an empty string → the signature won't match → it simply doesn't count.
    root = {
        "id": publishable.get("id", ""),
        "content": {k: publishable.get(k) for k in
                    ("schema", "type", "category", "title", "body", "trigger", "tags")},
        "parent_ids": prov.get("parent_ids", []),
        "origin": prov.get("origin", ""),
        "signer": signer_public_key,
    }
    if signer_github_user:
        root["github_user"] = signer_github_user
    return canonical_json(root)


@dataclass
class ContributionResult:
    ok: bool
    reason: str = ""
    envelope: Optional[dict] = None


def prepare_contribution(
    learning: Learning,
    contributor: Contributor,
    *,
    project_terms: Optional[list[str]] = None,
    github_user: str = "",
) -> ContributionResult:
    """Produce a signed, scrubbed envelope ready for the human gate. Does NOT
    publish. Re-runs the safety floor as defense-in-depth (the classifier already
    ran, but a contribution is the last line before the data leaves the device).

    ``github_user`` (Phase 7, optional): the contributor's GitHub username, bound
    into the signature so corroboration can count distinct *accounts* and CI can
    enforce that the PR author matches. Empty → a plain (pre-Phase-7) signature
    that still verifies but carries no account identity."""
    pub = learning.publishable()

    # 0. SIZE CAP — a learning is a short, general lesson, not a document. Reject
    #    oversized payloads so a single contribution can't bloat the pool repo.
    body_len = len(pub.get("body", "")) + len(pub.get("title", "")) + len(pub.get("trigger", ""))
    if body_len > MAX_CONTRIBUTION_CHARS:
        return ContributionResult(ok=False,
                                  reason=f"too-large:{body_len}>{MAX_CONTRIBUTION_CHARS}")

    # 1. SCRUB — second, independent floor pass over the publishable text only.
    joined = " \n ".join([pub["title"], pub["body"], pub["trigger"], " ".join(pub["tags"])])
    floor = safety_floor(joined, project_terms=project_terms)
    if floor.blocked:
        return ContributionResult(ok=False, reason=f"blocked-by-scrub:{','.join(floor.reasons)}")

    # 2. (generalization already done by the classifier's rewrite; we only verify
    #     there is no residual evidence — publishable() guarantees this structurally)
    if "evidence" in pub:
        return ContributionResult(ok=False, reason="evidence-leak")

    # 3/4. canonicalize + content-address: verify the id matches the content.
    if not verify_id(pub):
        return ContributionResult(ok=False, reason="id-mismatch")

    # 5. SIGN the root (binds content + origin + the signer's pubkey + github_user).
    gh = (github_user or "").strip().lstrip("@")
    message = _signing_message(pub, signer_public_key=contributor.public_key,
                               signer_github_user=gh)
    signature = contributor.sign(message)
    pub.setdefault("provenance", {})["signature"] = signature or None

    # The envelope carries an explicit ``signatures`` array (Phase 5b: corroboration)
    # with this contributor as signature #1. The legacy top-level ``signer`` +
    # ``provenance.signature`` are kept as a mirror of signatures[0] so older
    # readers and the live pool's already-signed files stay valid (no re-signing).
    # ``github_user`` (Phase 7) is recorded on the entry only when present; it's part
    # of the signed message, so it can't be swapped after signing.
    sig_entry = {"algo": contributor.algo, "public_key": contributor.public_key,
                 "signature": signature or ""}
    if gh:
        sig_entry["github_user"] = gh
    envelope = {
        "envelope": "komi.pool/1",
        "learning": pub,
        "signer": {"algo": contributor.algo, "public_key": contributor.public_key},
        "signatures": [sig_entry],
        "status": "ready",
    }
    return ContributionResult(ok=True, envelope=envelope)


def publish(envelope: dict, outbox_dir: str | Path, *, require_signature: bool = False) -> bool:
    """STUBBED network: write the approved envelope to the local outbox. A real
    pool server's ingest endpoint would receive exactly this and run :func:`ingest_verify`.

    ``require_signature`` mirrors a strict server that rejects unsigned entries."""
    if require_signature and envelope.get("signer", {}).get("algo") == "unsigned":
        return False
    d = Path(outbox_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    lid = envelope["learning"]["id"].replace(":", "_")
    (d / f"{lid}.json").write_text(json.dumps(envelope, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return True


# ── Pool-side verification (defense in depth; runs on ingest AND on pull) ──

@dataclass
class VerifyReport:
    accepted: bool
    id_ok: bool = False
    signature_ok: bool = False
    scrub_ok: bool = False
    corroboration: int = 0          # count of DISTINCT valid signers
    reasons: list[str] = field(default_factory=list)


def ingest_verify(envelope: dict, *, require_signature: bool = True) -> VerifyReport:
    """Independently verify an envelope. NEVER trust the producer: recompute the
    content id, re-check EVERY signer's signature against their own key, count the
    distinct valid signers (corroboration), and re-scrub.

    Multi-signature aware (Phase 5b): the envelope may carry a ``signatures`` array
    or the legacy single-``signer`` shape; both are handled by
    :mod:`.corroboration`. ``signature_ok`` means at least one signer verified.

    Asymmetry by design vs. the CI verifier: this is the *consumer* (pull) path, so
    it COUNTS valid signers and silently ignores any invalid ones — a good 3-signer
    learning shouldn't be refused because one signature rotted (it just counts 2).
    The CI verifier (publish gate) is stricter: it FAILS on any claimed-but-invalid
    signature, so the pool never stores a bogus one. Don't "unify" these."""
    rep = VerifyReport(accepted=False)
    learning = envelope.get("learning", {})

    rep.id_ok = verify_id(learning)
    if not rep.id_ok:
        rep.reasons.append("id-mismatch")

    # Count distinct contributors (by GitHub account when bound) with a valid
    # signature over this content. github_user is inside the signed bytes, so it's
    # rebuilt here — a swapped username makes the signature fail to verify.
    rep.corroboration = count_corroboration(
        envelope,
        sign_message=lambda lng, pk, gh="": _signing_message(
            lng, signer_public_key=pk, signer_github_user=gh),
        verify=verify_signature,
    )
    rep.signature_ok = rep.corroboration >= 1
    if not rep.signature_ok:
        rep.reasons.append("signature-invalid-or-unsigned")

    joined = " \n ".join([learning.get("title", ""), learning.get("body", ""),
                          learning.get("trigger", ""), " ".join(learning.get("tags", []))])
    floor = safety_floor(joined)
    rep.scrub_ok = not floor.blocked
    if not rep.scrub_ok:
        rep.reasons.extend(floor.reasons)

    rep.accepted = rep.id_ok and rep.scrub_ok and (rep.signature_ok or not require_signature)
    return rep


def pull(
    inbox_dir: str | Path,
    *,
    categories: Optional[list[str]] = None,
    require_signature: bool = True,
    min_corroboration: int = 1,
) -> list[Learning]:
    """STUBBED network: read envelopes from a local inbox (what a real server's
    query endpoint would return), re-verify each locally, and return accepted
    learnings marked scope=global.

    ``min_corroboration`` (Phase 5b) is now a REAL gate: a learning is only
    returned if at least that many DISTINCT contributors have a valid signature
    over it. The verified count rides along on each ``Learning.corroboration`` so
    recall can rank well-corroborated community knowledge higher."""
    d = Path(inbox_dir).expanduser()
    if not d.exists():
        return []
    out: list[Learning] = []
    for f in sorted(d.glob("*.json")):
        # One hostile/corrupt file must NEVER sink the whole pull — wrap the entire
        # per-file body so a single bad entry is skipped, not fatal. (A crash here
        # would silently disable ALL community recall for the user.)
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            rep = ingest_verify(env, require_signature=require_signature)
            if not rep.accepted or rep.corroboration < min_corroboration:
                continue
            rec = env["learning"]
            if categories and rec.get("category") not in categories:
                continue
            lng = Learning.from_dict({**rec, "scope": "global"})
            lng.provenance.origin = "pool"
            lng.corroboration = max(1, rep.corroboration)
            out.append(lng)
        except Exception:
            continue
    return out


__all__ = [
    "prepare_contribution", "ContributionResult", "publish",
    "ingest_verify", "VerifyReport", "pull",
]
