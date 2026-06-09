"""komi-learn — the Curator: the slow consolidation pass that keeps the library
from rotting.

Inspired directly by Hermes' 7-day curator. Two jobs, deliberately separated so
the safety net needs no model:

  1. PRUNE (deterministic, no LLM) — archive (NEVER delete) learnings that have
     gone stale: low confidence, never reused, older than a threshold. Pinned and
     pool-origin learnings are exempt.
  2. CONSOLIDATE (LLM, pluggable) — find clusters of overlapping learnings and
     merge them into a single rich "umbrella", archiving the now-redundant
     siblings. Without a model this step is skipped — we still REPORT the clusters
     so nothing is lost.

Invariants (match the architecture spec §4.3):
  • Archiving is the maximum destructive action. We never delete.
  • Pinned learnings (lifecycle.pinned) are never archived or consolidated.
  • Pool-origin (scope=global, pulled) learnings are never modified locally.
  • The pass writes a human-readable CURATION_REPORT.md.

Runtime-safe: a failure anywhere returns a partial report, never raises into a hook.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .model import Learning, Scope, Visibility
from .store import Store


# ── tunables (mirror Hermes' curator thresholds) ───────────────────────────

DEFAULT_STALE_DAYS = 30.0          # candidate for pruning beyond this age
DEFAULT_CONFIDENCE_FLOOR = 0.35    # below this (and unused) = prunable
MIN_CLUSTER_SIZE = 2               # need >=2 overlapping to propose an umbrella

# Cosine-similarity threshold for SEMANTIC clustering (when an embedding model is
# available). RE-CALIBRATED (precision-biased) against the real model
# (all-MiniLM-L6-v2) on a labeled set of procedural pairs — pinned by
# tests/test_semantic_clustering.py::test_real_model_threshold_separates_labeled_set
# so it can't silently rot again:
#
#   SHOULD merge (true positives):
#     two pytest tips (rephrased)         0.758
#     git rebase, two phrasings           0.738
#     venv <-> poetry (env mgmt)          0.475   # below 0.58: NOT merged (acceptable)
#     ripgrep <-> ag (same task, diff tool) 0.368 # below 0.58: NOT merged (acceptable)
#   SHOULD NOT merge (the costly false positives):
#     pytest -k <-> pytest -x (distinct!) 0.548   # the FP that 0.45 wrongly merged
#     git rebase <-> git bisect           0.400
#     ripgrep <-> git bisect              0.334
#     pytest <-> css                     -0.057
#
# Consolidation is DESTRUCTIVE (merge, then archive the siblings), so precision must
# beat recall: a false merge folds two genuinely-distinct skills into one and archives
# both. 0.45 (the original guess) merged the pytest -k/-x pair (distinct skills). 0.58
# clears the worst FP (0.548) with margin and sits below the true-merge cluster
# (0.738+). The price is missing the weak "related but different tool" merges
# (~0.37-0.48) — exactly the borderline cases where auto-merging is debatable anyway,
# and a consolidator can still catch the genuine ones. The earlier "recall bias / the
# LLM is the gate" framing was wrong: the consolidator is an optional backstop, and is
# asked to MERGE a cluster, not to adjudicate whether its members belong together.
# Override with KOMI_CLUSTER_THRESHOLD.
import os as _os
DEFAULT_CLUSTER_THRESHOLD = float(_os.environ.get("KOMI_CLUSTER_THRESHOLD", "0.58"))


@dataclass
class ClusterProposal:
    key: str                       # the shared prefix/tag the cluster formed on
    members: list[Learning]

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class CurationReport:
    scanned: int = 0
    pruned: list[str] = field(default_factory=list)        # ids archived as stale
    clusters: list[ClusterProposal] = field(default_factory=list)
    consolidated: list[dict] = field(default_factory=list)  # {umbrella, absorbed:[ids]}
    skipped_protected: int = 0
    cluster_mode: str = "lexical"                           # "semantic" | "lexical"
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"scanned {self.scanned}"]
        if self.pruned:
            bits.append(f"archived {len(self.pruned)} stale")
        if self.consolidated:
            absorbed = sum(len(c["absorbed"]) for c in self.consolidated)
            bits.append(f"consolidated {absorbed} into {len(self.consolidated)} umbrella(s)")
        elif self.clusters:
            bits.append(f"{len(self.clusters)} cluster(s) flagged")
        return " · ".join(bits) if len(bits) > 1 else "nothing to curate"


class ConsolidateLLM(Protocol):
    """Optional adjudicating gate over a proposed cluster. Given the cluster members,
    return {title, body, trigger, tags, rationale} for a merged umbrella ONLY if they
    are genuinely facets of one skill — or None to leave them alone. It MUST decline
    (return None) for members that are related/same-domain but distinct skills (e.g.
    two different pytest flags), because consolidation archives the originals. The
    clusterer proposes on *similarity*; this gate decides on *should-they-be-one*."""
    def __call__(self, members: list[Learning]) -> Optional[dict]: ...


# ── eligibility (deterministic) ─────────────────────────────────────────────

def _age_days(lng: Learning) -> float:
    ts = lng.lifecycle.created_at or lng.lifecycle.updated_at
    if not ts:
        return 0.0
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return 0.0
    return max(0.0, (time.time() - t) / 86400.0)


def _is_protected(lng: Learning) -> bool:
    """Pinned learnings and pool-origin (global, pulled) learnings are off-limits to
    the curator — the user pinned them, or they belong to the shared pool."""
    if lng.lifecycle.pinned:
        return True
    if lng.scope == Scope.GLOBAL.value and lng.provenance.origin == "pool":
        return True
    return False


def is_prunable(lng: Learning, *, stale_days: float, confidence_floor: float) -> bool:
    """Stale = low confidence AND never reused AND old. All three must hold, so a
    valuable-but-old learning (high confidence, or actually used) is never pruned."""
    if _is_protected(lng):
        return False
    if lng.lifecycle.state != "active":
        return False
    # Keep anything that's been acted on. NOTE: `reused` has no engine write path yet
    # (reuse-credit is unwired), so this guard is currently UNREACHABLE in practice —
    # it's the correct rule for when reuse lands, not a live protection today. Until
    # then, pruning is effectively governed by confidence + age below, which is why the
    # confidence signal (now distiller-scored, not a constant 0.3) matters so much here.
    if lng.usage.reused > 0:
        return False
    if (lng.confidence or 0.0) >= confidence_floor:
        return False
    return _age_days(lng) > stale_days


# ── clustering ───────────────────────────────────────────────────────────────

_STOP_PREFIX = {"the", "a", "an", "use", "fix", "run", "how", "to", "in", "on", "for"}


def _cluster_candidates(learnings: list[Learning]) -> list[Learning]:
    """The learnings eligible for umbrella consolidation: active, procedural (the
    ones that become skills — identity/semantic facts aren't umbrella-able), and
    not protected. Sorted by id for deterministic clustering output."""
    cands = [l for l in learnings
             if l.type == "procedural" and l.lifecycle.state == "active"
             and not _is_protected(l)]
    return sorted(cands, key=lambda l: l.id)


def cluster(learnings: list[Learning], *, embedder=None,
            threshold: float = DEFAULT_CLUSTER_THRESHOLD,
            vectors_by_id: Optional[dict] = None) -> list[ClusterProposal]:
    """Group procedural learnings that plausibly cover the same class of task.

    Semantic-first (mirrors recall): when an ``embedder`` is supplied, cluster by
    MEANING (cosine similarity) so conceptually-overlapping lessons are caught even
    if they share no title word or tag. Without an embedder (zero-dep install, or the
    model disabled/absent) fall back to the cheap lexical signals (shared first
    significant title word, or a shared tag). Only *procedural*, non-protected,
    active learnings are considered either way.

    ``vectors_by_id`` (optional) supplies precomputed embeddings keyed by learning id
    — the curator passes the store's persisted vectors so we don't re-encode every
    candidate every pass. Any candidate missing from the map is encoded on the fly;
    if that yields a bad vector we fall back to lexical."""
    candidates = _cluster_candidates(learnings)
    if embedder is not None:
        vectors = None
        if vectors_by_id is not None:
            try:
                vectors = [vectors_by_id.get(l.id) or embedder.encode(_embed_text(l))
                           for l in candidates]
            except Exception:
                vectors = None
        sem = _cluster_semantic(candidates, embedder, threshold, vectors=vectors)
        if sem is not None:
            return sem
    return _cluster_lexical(candidates)


def _cluster_lexical(candidates: list[Learning]) -> list[ClusterProposal]:
    """Deterministic, zero-dependency clustering on shared title-word / tag."""
    buckets: dict[str, list[Learning]] = {}
    for l in candidates:
        for key in _cluster_keys(l):
            buckets.setdefault(key, [])
            if l not in buckets[key]:
                buckets[key].append(l)

    # Keep clusters with >=MIN_CLUSTER_SIZE, dedup overlapping clusters by keeping
    # the largest, and don't let one learning anchor two reported clusters.
    proposals = [ClusterProposal(k, m) for k, m in buckets.items()
                 if len(m) >= MIN_CLUSTER_SIZE]
    proposals.sort(key=lambda p: (p.size, p.key), reverse=True)
    seen_ids: set[str] = set()
    out: list[ClusterProposal] = []
    for p in proposals:
        fresh = [m for m in p.members if m.id not in seen_ids]
        if len(fresh) >= MIN_CLUSTER_SIZE:
            out.append(ClusterProposal(p.key, fresh))
            seen_ids.update(m.id for m in fresh)
    return out


def _embed_text(l: Learning) -> str:
    """The text a learning is embedded from. MUST match Store.embed_pending's join
    so on-the-fly vectors here are comparable to the store's persisted ones."""
    return " \n ".join(filter(None, [l.title, l.body, l.trigger, " ".join(l.tags or [])]))


def _cluster_semantic(candidates: list[Learning], embedder, threshold: float,
                      *, vectors: Optional[list] = None) -> Optional[list[ClusterProposal]]:
    """Greedy, deterministic clustering by cosine similarity with a MUTUAL-similarity
    rule.

    For each not-yet-clustered learning (in stable id order) we open a cluster seeded
    by it, then admit another unclustered learning ONLY if it is >= threshold to
    *every member already in the cluster* — not merely to the seed. This is stricter
    than seed-anchoring: it prevents a "star" false-positive where a seed S is similar
    to both A and B (S~A, S~B) but A and B are unrelated (A!~B) — seed-anchoring would
    merge {S,A,B} and archive all three; mutual-similarity refuses to add B once A is
    in (or vice-versa), keeping clusters genuinely cohesive. Still bounded (no
    transitive chaining) and deterministic for a fixed corpus (id-sorted input).

    ``vectors`` lets the caller pass precomputed embeddings (the store persists them)
    to avoid re-encoding every candidate every pass. Returns None on any embedding
    failure so the caller falls back to lexical — never raises into a curation pass."""
    from .embed import cosine
    try:
        vecs = vectors if vectors is not None else [embedder.encode(_embed_text(l))
                                                    for l in candidates]
    except Exception:
        return None
    if len(vecs) != len(candidates) or any(not v for v in vecs):
        return None  # missing/empty vector ⇒ model didn't really encode ⇒ fall back

    # Pairwise similarity matrix. Use numpy if present (vectorized, fast at scale);
    # otherwise pure-Python cosine. Same numbers either way.
    sim = _similarity_matrix(vecs, cosine)

    used = [False] * len(candidates)
    out: list[ClusterProposal] = []
    for i, seed in enumerate(candidates):
        if used[i]:
            continue
        member_idx = [i]
        for j in range(i + 1, len(candidates)):
            if used[j]:
                continue
            # admit j only if it's >= threshold to EVERY current member (mutual)
            if all(sim[j][k] >= threshold for k in member_idx):
                member_idx.append(j)
        if len(member_idx) >= MIN_CLUSTER_SIZE:
            for k in member_idx:
                used[k] = True
            key = next((f"w:{w}" for w in _title_words(seed)), f"sem:{seed.id[:12]}")
            out.append(ClusterProposal(key, [candidates[k] for k in member_idx]))
    return out


def _similarity_matrix(vecs: list, cosine_fn) -> list:
    """NxN cosine similarity. Vectorized with numpy when available (vectors from the
    embedder are L2-normalized, so it's a single matmul); pure-Python fallback keeps
    the engine's zero-required-dep guarantee. Returns a list-of-lists either way."""
    try:
        import numpy as np
        m = np.asarray(vecs, dtype="float32")
        # normalize defensively (mixed sources); then S = m @ m.T is cosine.
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        m = m / norms
        return (m @ m.T).tolist()
    except Exception:
        n = len(vecs)
        return [[cosine_fn(vecs[a], vecs[b]) for b in range(n)] for a in range(n)]


def _title_words(l: Learning) -> list[str]:
    return [w for w in (l.title or "").lower().replace("-", " ").split()
            if w.isalnum() and w not in _STOP_PREFIX and len(w) >= 3]


def _cluster_keys(l: Learning) -> list[str]:
    keys: list[str] = []
    for w in _title_words(l):
        keys.append(f"w:{w}")
        break
    for t in (l.tags or [])[:3]:
        keys.append(f"t:{t.strip().lower()}")
    return keys


# ── the pass ────────────────────────────────────────────────────────────────

def curate(
    store: Store,
    *,
    consolidator: Optional[ConsolidateLLM] = None,
    stale_days: float = DEFAULT_STALE_DAYS,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    embedder=None,
    dry_run: bool = False,
) -> CurationReport:
    """Run one curation pass. Deterministic pruning always; LLM consolidation only
    if a ``consolidator`` is given. ``dry_run`` reports without mutating.

    Clustering is semantic when an embedding model is available: ``embedder`` is
    resolved from :func:`engine.embed.get_embedder` unless one is passed in (tests
    inject a mock). Falls back to lexical clustering when absent — same zero-dep
    guarantee as recall."""
    rep = CurationReport()
    if embedder is None:
        try:
            from .embed import get_embedder
            embedder = get_embedder()
        except Exception:
            embedder = None
    try:
        # Telemetry-hydrated: is_prunable's keep-if-reused guard reads usage.reused,
        # which lives in the DB, not Markdown. Plain all() would make that guard see 0
        # for everything and prune learnings the user actually reused.
        learnings = store.all_with_telemetry()
    except Exception as e:
        rep.notes.append(f"could not load learnings: {e}")
        return rep
    rep.scanned = len(learnings)
    rep.skipped_protected = sum(1 for l in learnings if _is_protected(l))

    # 1. PRUNE (deterministic)
    for l in learnings:
        if is_prunable(l, stale_days=stale_days, confidence_floor=confidence_floor):
            rep.pruned.append(l.id)
            if not dry_run:
                try:
                    store.archive(l.id)
                except Exception as e:
                    rep.notes.append(f"archive failed for {l.id[:16]}: {e}")

    # refresh after pruning so consolidation sees the survivors
    survivors = [l for l in (store.all() if not dry_run else learnings)
                 if l.lifecycle.state == "active"]

    # 2. CONSOLIDATE (LLM, optional). Cluster by meaning when a model is present.
    # Reuse the store's PERSISTED embeddings (backfill any missing) instead of
    # re-encoding every candidate every pass — recall already paid to compute them.
    vectors_by_id = None
    if embedder is not None:
        try:
            store.embed_pending(embedder)
            vectors_by_id = store.embeddings_by_id()
        except Exception:
            vectors_by_id = None
    rep.clusters = cluster(survivors, embedder=embedder, vectors_by_id=vectors_by_id)
    rep.cluster_mode = "semantic" if embedder is not None else "lexical"
    if consolidator is not None:
        for cl in rep.clusters:
            try:
                merged = consolidator(cl.members)
            except Exception as e:
                rep.notes.append(f"consolidator error on '{cl.key}': {e}")
                continue
            if not merged or not merged.get("title") or not merged.get("body"):
                continue
            umbrella = _build_umbrella(merged, cl.members)
            absorbed = [m.id for m in cl.members]
            if not dry_run:
                try:
                    store.upsert(umbrella)              # the new/updated umbrella
                    for mid in absorbed:
                        if mid != umbrella.id:
                            store.archive(mid)          # fold siblings in (archive, not delete)
                except Exception as e:
                    rep.notes.append(f"consolidation write failed for '{cl.key}': {e}")
                    continue
            rep.consolidated.append({"umbrella": umbrella.title,
                                     "absorbed": [a for a in absorbed if a != umbrella.id]})
    elif rep.clusters:
        rep.notes.append("no consolidator (LLM) available — clusters flagged but not merged")

    return rep


def _build_umbrella(merged: dict, members: list[Learning]) -> Learning:
    """Construct the umbrella Learning from the LLM's merge + the cluster. It keeps
    the highest confidence of its members and unions their tags."""
    tags = sorted({t for m in members for t in (m.tags or [])} |
                  {t.strip().lower() for t in merged.get("tags", []) if t.strip()})
    conf = max([m.confidence for m in members] + [0.5])
    # A merged umbrella is at least as sensitive as its most-sensitive member: if ANY
    # member is private, the umbrella is private — otherwise consolidation would
    # launder confidential content from a member's body into a committable skill.
    visibility = (Visibility.PRIVATE.value
                  if any(getattr(m, "visibility", Visibility.SHAREABLE.value)
                         == Visibility.PRIVATE.value for m in members)
                  else Visibility.SHAREABLE.value)
    body = merged["body"].strip()
    title = merged["title"].strip()
    trigger = (merged.get("trigger") or members[0].trigger or "").strip()
    # Re-run the confidential floor on the SYNTHESIZED text: the LLM merge can surface
    # confidential phrasing even from shareable-looking members. The floor must win on
    # consolidated content too (it does on freshly-distilled content).
    from .classify import safety_floor
    if safety_floor(" \n ".join([title, body, trigger, " ".join(tags)])).confidential:
        visibility = Visibility.PRIVATE.value
    u = Learning(
        type="procedural",
        category=merged.get("category") or members[0].category,
        title=title,
        body=body,
        trigger=trigger,
        tags=tags,
        scope=Scope.PROJECT.value if any(m.scope == Scope.PROJECT.value for m in members)
              else members[0].scope,
        confidence=min(1.0, conf),
        visibility=visibility,
    )
    u.finalize()
    # Record lineage: which learnings were folded in. (Provenance, not content, so
    # it doesn't affect the content-addressed id — set after finalize.) Makes the
    # consolidation traceable/reversible, mirroring the PAM derivation DAG.
    u.provenance.parent_ids = [m.id for m in members]
    return u


# ── corpus health / drift surfacing ──────────────────────────────────────────

def corpus_health(learnings: list[Learning]) -> dict:
    """A cheap 'is the learning corpus drifting/going stale?' snapshot — the honest
    analogue of model-drift monitoring for a system that has no model weights.

    Returns counts + the share of active learnings that are stale-and-unused. A high
    stale share means recall is increasingly serving aged knowledge that's never
    used — a signal to curate or that the user's focus has moved on."""
    active = [l for l in learnings if l.lifecycle.state == "active"]
    n = len(active)
    if n == 0:
        return {"active": 0, "stale_unused": 0, "stale_share": 0.0,
                "never_reused": 0, "surfaced_never_used": 0,
                "surfaced_never_used_share": None, "reuse_instrumented": False,
                "avg_confidence": 0.0}
    # HONESTY GATE: the `reused` counter has no engine write path yet (it is credited
    # only when a learning is *acted on*, and that crediting is not wired). So every
    # reuse-derived metric below is, until reuse is instrumented, computed from a field
    # frozen at 0 — which would make "surfaced but never used" read as a 100% uselessness
    # verdict that is really a measurement artifact (the same trap the dead `recalled`
    # counter set). We detect instrumentation by whether ANY active learning was ever
    # credited reuse, and refuse to emit the share as a quality verdict when it wasn't.
    reuse_instrumented = any((l.usage.reused or 0) > 0 for l in active)
    stale_unused = sum(
        1 for l in active
        if l.usage.reused == 0 and _age_days(l) > DEFAULT_STALE_DAYS
        and (l.confidence or 0) < 0.6
    )
    never_reused = sum(1 for l in active if l.usage.reused == 0)
    # Learnings recall actually SURFACED into context (recalled>0) yet were never acted
    # on (reused==0). When reuse IS instrumented, a high share is the fingerprint of
    # low-signal memory. When it is NOT, the share is meaningless (→ None), not 100%.
    surfaced = [l for l in active if (l.usage.recalled or 0) > 0]
    surfaced_never_used = sum(1 for l in surfaced if l.usage.reused == 0)
    avg_conf = sum((l.confidence or 0) for l in active) / n
    return {
        "active": n,
        "stale_unused": stale_unused,
        "stale_share": round(stale_unused / n, 2),
        "never_reused": never_reused,
        "surfaced_never_used": surfaced_never_used,
        # Only a real quality verdict once reuse is wired; otherwise None ("not measurable").
        "surfaced_never_used_share": (
            (round(surfaced_never_used / len(surfaced), 2) if surfaced else 0.0)
            if reuse_instrumented else None
        ),
        "reuse_instrumented": reuse_instrumented,
        "avg_confidence": round(avg_conf, 2),
    }


# ── report rendering ────────────────────────────────────────────────────────

def render_report(rep: CurationReport) -> str:
    lines = [
        "# komi-learn — Curation Report",
        "",
        f"**Summary:** {rep.summary()}",
        f"- scanned: {rep.scanned}",
        f"- protected (pinned/pool, untouched): {rep.skipped_protected}",
        "",
    ]
    if rep.consolidated:
        lines.append("## Consolidated into umbrellas")
        for c in rep.consolidated:
            lines.append(f"- **{c['umbrella']}** ← absorbed {len(c['absorbed'])} learning(s)")
        lines.append("")
    if rep.clusters and not rep.consolidated:
        lines.append(f"## Clusters flagged (not merged — no LLM this run) [{rep.cluster_mode}]")
        lines.append("(Suggestions only. These are *similar*, not necessarily one "
                     "skill — nothing is merged or archived without the LLM "
                     "consolidator confirming they belong together.)")
        for cl in rep.clusters:
            titles = ", ".join(m.title for m in cl.members[:5])
            lines.append(f"- `{cl.key}` ({cl.size}): {titles}")
        lines.append("")
    if rep.pruned:
        lines.append(f"## Archived as stale ({len(rep.pruned)})")
        lines.append("(archived, never deleted — recoverable)")
        lines.append("")
    if rep.notes:
        lines.append("## Notes")
        for n in rep.notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "curate", "cluster", "is_prunable", "CurationReport", "ClusterProposal",
    "ConsolidateLLM", "render_report", "corpus_health",
    "DEFAULT_STALE_DAYS", "DEFAULT_CONFIDENCE_FLOOR", "MIN_CLUSTER_SIZE",
]
