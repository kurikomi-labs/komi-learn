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

from .model import Learning, Scope
from .store import Store


# ── tunables (mirror Hermes' curator thresholds) ───────────────────────────

DEFAULT_STALE_DAYS = 30.0          # candidate for pruning beyond this age
DEFAULT_CONFIDENCE_FLOOR = 0.35    # below this (and unused) = prunable
MIN_CLUSTER_SIZE = 2               # need >=2 overlapping to propose an umbrella

# Cosine-similarity threshold for SEMANTIC clustering (when an embedding model is
# available). Calibrated against the real model (all-MiniLM-L6-v2) on procedural
# learnings:
#   same-domain / rephrased lessons   ~0.73-0.74   (e.g. two pytest tips)
#   related concept, different tool   ~0.37-0.48   (venv↔poetry, rg↔ag)
#   genuinely unrelated               ≤0.09        (rg↔traceback, pytest↔css)
# 0.45 catches same-domain + closely-related pairs while sitting ~5x above the
# unrelated ceiling. Clustering only PROPOSES — the LLM consolidator is the real
# gate and can decline a bad grouping (return None) — so we err toward recall here
# rather than a high-precision cutoff. Override with KOMI_CLUSTER_THRESHOLD.
import os as _os
DEFAULT_CLUSTER_THRESHOLD = float(_os.environ.get("KOMI_CLUSTER_THRESHOLD", "0.45"))


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
    """Optional. Given a cluster, return {title, body, trigger, tags, rationale}
    for the merged umbrella, or None to leave the cluster alone."""
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
            threshold: float = DEFAULT_CLUSTER_THRESHOLD) -> list[ClusterProposal]:
    """Group procedural learnings that plausibly cover the same class of task.

    Semantic-first (mirrors recall): when an ``embedder`` is supplied, cluster by
    MEANING (cosine similarity) so lessons that overlap conceptually but share no
    title word or tag — e.g. "Prefer ripgrep over grep -r" and "Use ag for fast
    code search" — are caught. Without an embedder (zero-dep install, or the model
    disabled/absent) fall back to the cheap lexical signals: a shared first
    significant title word, or a shared tag. Either way only *procedural*,
    non-protected, active learnings are considered."""
    candidates = _cluster_candidates(learnings)
    if embedder is not None:
        sem = _cluster_semantic(candidates, embedder, threshold)
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


def _cluster_semantic(candidates: list[Learning], embedder,
                      threshold: float) -> Optional[list[ClusterProposal]]:
    """Greedy, deterministic, seed-based clustering by cosine similarity.

    For each not-yet-clustered learning (in stable id order) we open a cluster and
    pull in every other unclustered learning whose similarity to the SEED is
    >= threshold. Seed-anchored (not full transitive closure) so one borderline
    link can't chain unrelated lessons into a runaway megacluster, and so the
    output is stable across runs. Returns None on any embedding failure so the
    caller falls back to lexical — never raises into a curation pass."""
    from .embed import cosine
    try:
        vecs: list[list[float]] = [embedder.encode(_embed_text(l)) for l in candidates]
    except Exception:
        return None
    if any(not v for v in vecs):
        return None  # an empty vector means the model didn't really encode → fall back

    used = [False] * len(candidates)
    out: list[ClusterProposal] = []
    for i, seed in enumerate(candidates):
        if used[i]:
            continue
        members = [seed]
        member_idx = [i]
        for j in range(i + 1, len(candidates)):
            if used[j]:
                continue
            if cosine(vecs[i], vecs[j]) >= threshold:
                members.append(candidates[j])
                member_idx.append(j)
        if len(members) >= MIN_CLUSTER_SIZE:
            for k in member_idx:
                used[k] = True
            # key = the seed's first significant title word, for a readable report
            key = next((f"w:{w}" for w in _title_words(seed)), f"sem:{seed.id[:12]}")
            out.append(ClusterProposal(key, members))
    return out


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
        learnings = store.all()
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
    rep.clusters = cluster(survivors, embedder=embedder)
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
    u = Learning(
        type="procedural",
        category=merged.get("category") or members[0].category,
        title=merged["title"].strip(),
        body=merged["body"].strip(),
        trigger=(merged.get("trigger") or members[0].trigger or "").strip(),
        tags=tags,
        scope=Scope.PROJECT.value if any(m.scope == Scope.PROJECT.value for m in members)
              else members[0].scope,
        confidence=min(1.0, conf),
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
                "never_reused": 0, "avg_confidence": 0.0}
    stale_unused = sum(
        1 for l in active
        if l.usage.reused == 0 and _age_days(l) > DEFAULT_STALE_DAYS
        and (l.confidence or 0) < 0.6
    )
    never_reused = sum(1 for l in active if l.usage.reused == 0)
    avg_conf = sum((l.confidence or 0) for l in active) / n
    return {
        "active": n,
        "stale_unused": stale_unused,
        "stale_share": round(stale_unused / n, 2),
        "never_reused": never_reused,
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
