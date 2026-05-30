"""komi-learn — the Learning data model and content-addressing.

A *Learning* is the atom of the system: one durable unit of knowledge distilled
from a session. This module defines the record, its canonical serialization, and
its content-addressed id (BLAKE3 of the canonical form, with a graceful fallback
to hashlib's blake2b when the optional ``blake3`` wheel is not installed).

The id is computed over the *publishable* content only — never over local-only
provenance (``evidence``) or mutable bookkeeping (``usage``/``lifecycle``). This
means two agents that independently distill the same lesson arrive at the same
id, which is what makes pool dedup and cross-agent corroboration work.

The schema is kept JSON-trivial and forward-compatible.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


SCHEMA = "komi.learning/1"


# ── Controlled vocabularies ──────────────────────────────────────────────
# Kept as plain string enums so records stay JSON-trivial and forward-compatible
# (unknown values degrade to strings rather than blowing up old readers).

class LearningType(str, Enum):
    """PAM memory types we persist. Episodic is transient distill input only."""
    IDENTITY = "identity"      # who the user is / how they want to be served (PAM I)
    SEMANTIC = "semantic"      # a durable fact (PAM S)
    PROCEDURAL = "procedural"  # how to do a class of task — becomes/patches a skill (PAM P)


class Scope(str, Enum):
    PERSONAL = "personal"  # about this user/machine — never leaves the device
    PROJECT = "project"    # specific to this repo/project's conventions
    GLOBAL = "global"      # generally true; eligible (after review) for the public pool


class Category(str, Enum):
    TOOLING = "tooling"
    WORKFLOW = "workflow"
    PREFERENCE = "preference"
    DOMAIN_KNOWLEDGE = "domain-knowledge"
    PITFALL = "pitfall"
    DEBUGGING = "debugging"
    LANGUAGE_BEHAVIOR = "language-behavior"
    FORMATTING_STYLE = "formatting-style"
    META_AGENT = "meta-agent"          # how to work well with the agent itself
    ENVIRONMENT = "environment"        # local setup — always personal, never global


class Signal(str, Enum):
    """Why this learning was captured — mirrors the distill prompt's signal list."""
    USER_CORRECTION = "user-correction"
    TECHNIQUE = "technique"
    FIX = "fix"
    REPEATED_PATTERN = "repeated-pattern"
    DURABLE_FACT = "durable-fact"


# ── Sub-records ──────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """LOCAL-ONLY provenance. Stripped before any contribution to the pool."""
    session_id: str = ""
    observed_at: str = ""               # ISO 8601 UTC
    signal: str = Signal.TECHNIQUE.value
    transcript_span: Optional[list[int]] = None  # [start_line, end_line] in the JSONL


@dataclass
class Provenance:
    """Populated only when a learning is shared (PAM Merkle-DAG + signature)."""
    parent_ids: list[str] = field(default_factory=list)
    origin: str = "agent:unknown"
    signature: Optional[str] = None     # Ed25519 over the entry root, set at publish


@dataclass
class Usage:
    recalled: int = 0
    reused: int = 0
    last_used: Optional[str] = None


@dataclass
class Lifecycle:
    created_at: str = ""
    updated_at: str = ""
    state: str = "active"               # active | archived
    pinned: bool = False                # if True, the curator never archives/consolidates it


# ── The Learning ─────────────────────────────────────────────────────────

@dataclass
class Learning:
    """One durable unit of knowledge. ``id`` is derived; do not set it by hand —
    call :meth:`finalize` (or :func:`compute_id`) after the content is settled."""

    type: str
    category: str
    title: str
    body: str
    trigger: str = ""                   # "use when…" — the recall key
    tags: list[str] = field(default_factory=list)
    scope: str = Scope.PERSONAL.value
    confidence: float = 0.3

    id: str = ""
    schema: str = SCHEMA
    evidence: Evidence = field(default_factory=Evidence)
    provenance: Provenance = field(default_factory=Provenance)
    usage: Usage = field(default_factory=Usage)
    lifecycle: Lifecycle = field(default_factory=Lifecycle)

    # Transient trust signal for pool-sourced learnings: how many DISTINCT
    # contributors independently signed this exact content (computed at pull time,
    # see pool/corroboration.py). NOT part of content_view/the id — the same lesson
    # must hash identically regardless of how many people have endorsed it. 1 for
    # purely local learnings. Recall uses it as a small ranking nudge.
    corroboration: int = 1

    # ---- content addressing -------------------------------------------------

    def content_view(self) -> dict[str, Any]:
        """The *publishable* projection the id is computed over.

        Excludes id/signature and every local-only or mutable field, so the same
        lesson distilled by two different agents hashes identically. Tags are
        sorted and lowercased so trivial ordering/case differences don't fork
        the id. This is the canonical content.
        """
        return {
            "schema": self.schema,
            "type": self.type,
            "category": self.category,
            "title": self.title.strip(),
            "body": self.body.strip(),
            "trigger": self.trigger.strip(),
            "tags": sorted({t.strip().lower() for t in self.tags if t.strip()}),
        }

    def finalize(self) -> "Learning":
        """Compute and assign the content-addressed id. Returns self for chaining."""
        self.id = compute_id(self.content_view())
        now = _now_iso()
        if not self.lifecycle.created_at:
            self.lifecycle.created_at = now
        self.lifecycle.updated_at = now
        if not self.evidence.observed_at:
            self.evidence.observed_at = now
        return self

    # ---- (de)serialization --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Learning":
        d = dict(d)
        sub = {
            "evidence": Evidence,
            "provenance": Provenance,
            "usage": Usage,
            "lifecycle": Lifecycle,
        }
        for key, klass in sub.items():
            val = d.get(key)
            if isinstance(val, dict):
                # tolerate extra/missing keys across schema versions
                allowed = {f for f in klass.__dataclass_fields__}  # type: ignore[attr-defined]
                d[key] = klass(**{k: v for k, v in val.items() if k in allowed})
            elif val is None:
                d[key] = klass()
        # Normalize tags on load so a hand-edited file with empty/blank tags can't
        # produce a Learning whose id later diverges from the clean copy.
        if isinstance(d.get("tags"), list):
            d["tags"] = [t for t in d["tags"] if isinstance(t, str) and t.strip()]
        # ``corroboration`` is a COMPUTED trust signal, never content. Refuse to
        # deserialize it from a record — a pool file (or hand-edited local file)
        # claiming ``corroboration: 999`` must NOT be believed. Whoever legitimately
        # knows the count (the pull path) sets it explicitly AFTER from_dict from the
        # re-verified signer count. Defaults to 1 here.
        allowed_top = set(cls.__dataclass_fields__) - {"corroboration"}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed_top})

    def publishable(self) -> dict[str, Any]:
        """The form that may leave the device: content + DAG/signature provenance,
        with all local evidence and bookkeeping removed. Used by the pool pipeline."""
        return {
            "id": self.id,
            **self.content_view(),
            "provenance": {
                "parent_ids": list(self.provenance.parent_ids),
                "origin": self.provenance.origin,
                "signature": self.provenance.signature,
            },
        }


# ── Canonicalization + hashing ───────────────────────────────────────────

def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8, NFC.

    Determinism is the whole game for content addressing — the same logical
    content must always produce the same bytes regardless of dict ordering or
    platform. (PAM uses BLAKE3 over canonical JSON; we follow that.)
    """
    import unicodedata

    def _norm(x: Any) -> Any:
        if isinstance(x, str):
            return unicodedata.normalize("NFC", x)
        if isinstance(x, dict):
            return {k: _norm(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [_norm(v) for v in x]
        return x

    return json.dumps(
        _norm(obj),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _blake3_hex(data: bytes) -> tuple[str, str]:
    """Return (algorithm_label, hexdigest). Prefer real BLAKE3; fall back to
    blake2b so the engine runs with zero extra installs. The label is recorded
    in the id prefix so a reader always knows which function produced it and we
    never silently compare hashes across algorithms."""
    try:
        import blake3  # type: ignore
        return "blake3", blake3.blake3(data).hexdigest()
    except Exception:
        import hashlib
        # blake2b is in the stdlib and is a fine content-address fallback.
        return "blake2b", hashlib.blake2b(data, digest_size=32).hexdigest()


def compute_id(content_view: dict[str, Any]) -> str:
    """Content-addressed id over the canonical content view, e.g. ``blake3:9f86…``.

    Prefix carries the algorithm so blake3-built and fallback-built ids never
    collide or get mistaken for one another.
    """
    algo, digest = _blake3_hex(canonical_json(content_view))
    return f"{algo}:{digest}"


def verify_id(record: dict[str, Any]) -> bool:
    """True iff a (publishable) record's declared id matches its content.

    Recompute over the same content view the producer used. Tamper-evidence:
    any edit to content changes the recomputed id. Used pool-side and on pull.
    """
    declared = record.get("id", "")
    if ":" not in declared:
        return False
    algo = declared.split(":", 1)[0]
    view = {
        "schema": record.get("schema", SCHEMA),
        "type": record.get("type", ""),
        "category": record.get("category", ""),
        "title": (record.get("title") or "").strip(),
        "body": (record.get("body") or "").strip(),
        "trigger": (record.get("trigger") or "").strip(),
        "tags": sorted({t.strip().lower() for t in record.get("tags", []) if t.strip()}),
    }
    canon = canonical_json(view)
    if algo == "blake3":
        try:
            import blake3  # type: ignore
            return declared == f"blake3:{blake3.blake3(canon).hexdigest()}"
        except Exception:
            # Correct (not a bug): we will NOT "verify" a blake3 id by recomputing
            # a blake2b hash — that can't match, and pretending otherwise would
            # accept unverifiable content. A machine consuming a blake3 pool must
            # install blake3 (the `crypto` extra). doctor/requirements flag this.
            return False
    if algo == "blake2b":
        import hashlib
        return declared == f"blake2b:{hashlib.blake2b(canon, digest_size=32).hexdigest()}"
    return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    "SCHEMA",
    "LearningType", "Scope", "Category", "Signal",
    "Evidence", "Provenance", "Usage", "Lifecycle", "Learning",
    "canonical_json", "compute_id", "verify_id",
]
