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


# ── clustering (deterministic) ──────────────────────────────────────────────

_STOP_PREFIX = {"the", "a", "an", "use", "fix", "run", "how", "to", "in", "on", "for"}


def cluster(learnings: list[Learning]) -> list[ClusterProposal]:
    """Group procedural learnings that plausibly cover the same class of task.

    Cheap + deterministic (no embeddings in v1): two signals form a cluster —
    a shared first significant title word, or a shared tag. We only cluster
    *procedural* learnings (those that become skills); identity/semantic facts
    aren't umbrella-able. Protected learnings are excluded from clustering."""
    candidates = [l for l in learnings
                  if l.type == "procedural" and l.lifecycle.state == "active"
                  and not _is_protected(l)]

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
    proposals.sort(key=lambda p: p.size, reverse=True)
    seen_ids: set[str] = set()
    out: list[ClusterProposal] = []
    for p in proposals:
        fresh = [m for m in p.members if m.id not in seen_ids]
        if len(fresh) >= MIN_CLUSTER_SIZE:
            out.append(ClusterProposal(p.key, fresh))
            seen_ids.update(m.id for m in fresh)
    return out


def _cluster_keys(l: Learning) -> list[str]:
    keys: list[str] = []
    words = [w for w in (l.title or "").lower().replace("-", " ").split() if w.isalnum()]
    for w in words:
        if w not in _STOP_PREFIX and len(w) >= 3:
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
    dry_run: bool = False,
) -> CurationReport:
    """Run one curation pass. Deterministic pruning always; LLM consolidation only
    if a ``consolidator`` is given. ``dry_run`` reports without mutating."""
    rep = CurationReport()
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

    # 2. CONSOLIDATE (LLM, optional)
    rep.clusters = cluster(survivors)
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
        lines.append("## Clusters flagged (not merged — no LLM this run)")
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
    "ConsolidateLLM", "render_report",
    "DEFAULT_STALE_DAYS", "DEFAULT_CONFIDENCE_FLOOR", "MIN_CLUSTER_SIZE",
]
