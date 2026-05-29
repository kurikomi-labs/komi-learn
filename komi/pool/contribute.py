"""komi-learn pool — contribution & consumption pipeline (PAM-style).

Contribution (local → pool), per docs/02-architecture.md §7.1:
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


# ── Envelope ──────────────────────────────────────────────────────────────
# What actually travels to/from the pool. The signature covers the canonical
# publishable content (the same bytes the id is derived from) plus parent_ids,
# so neither the content nor its DAG position can be altered post-signature.

def _signing_message(publishable: dict) -> bytes:
    root = {
        "id": publishable["id"],
        "content": {k: publishable[k] for k in
                    ("schema", "type", "category", "title", "body", "trigger", "tags")},
        "parent_ids": publishable.get("provenance", {}).get("parent_ids", []),
    }
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
) -> ContributionResult:
    """Produce a signed, scrubbed envelope ready for the human gate. Does NOT
    publish. Re-runs the safety floor as defense-in-depth (the classifier already
    ran, but a contribution is the last line before the data leaves the device)."""
    pub = learning.publishable()

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

    # 5. SIGN the root.
    message = _signing_message(pub)
    signature = contributor.sign(message)
    pub.setdefault("provenance", {})["signature"] = signature or None

    envelope = {
        "envelope": "komi.pool/1",
        "learning": pub,
        "signer": {"algo": contributor.algo, "public_key": contributor.public_key},
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
    reasons: list[str] = field(default_factory=list)


def ingest_verify(envelope: dict, *, require_signature: bool = True) -> VerifyReport:
    """Independently verify an envelope. NEVER trust the producer: recompute the
    content id, re-check the signature against the signer's key, and re-scrub."""
    rep = VerifyReport(accepted=False)
    learning = envelope.get("learning", {})

    rep.id_ok = verify_id(learning)
    if not rep.id_ok:
        rep.reasons.append("id-mismatch")

    sig = learning.get("provenance", {}).get("signature")
    pk = envelope.get("signer", {}).get("public_key", "")
    rep.signature_ok = verify_signature(_signing_message(learning), sig or "", pk)
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
    require_signature: bool = False,
    min_corroboration: int = 1,
) -> list[Learning]:
    """STUBBED network: read envelopes from a local inbox (what a real server's
    query endpoint would return), re-verify each locally, and return accepted
    learnings marked scope=global. ``min_corroboration`` would gate on distinct
    signers server-side; here it's a placeholder the server contract honors."""
    d = Path(inbox_dir).expanduser()
    if not d.exists():
        return []
    out: list[Learning] = []
    for f in sorted(d.glob("*.json")):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rep = ingest_verify(env, require_signature=require_signature)
        if not rep.accepted:
            continue
        rec = env["learning"]
        if categories and rec.get("category") not in categories:
            continue
        lng = Learning.from_dict({**rec, "scope": "global"})
        lng.provenance.origin = "pool"
        out.append(lng)
    return out


__all__ = [
    "prepare_contribution", "ContributionResult", "publish",
    "ingest_verify", "VerifyReport", "pull",
]
