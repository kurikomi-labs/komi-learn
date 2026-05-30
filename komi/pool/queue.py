"""komi-learn pool — the review queue → publish bridge.

Global-candidate learnings are written to a local review queue by the distiller
(``komi/engine/distill.py``). NOTHING is published from there automatically; the
human gate lives here. A queued item has a ``status``:

    pending-review  → awaiting the user's decision (default)
    approved        → user approved; eligible to publish to the pool
    rejected        → user declined; kept for audit, never published

This module lists the queue, lets a caller approve/reject, and publishes approved
items through a :class:`GitHubPool` (opening a PR, or committing in local mode).
Publishing re-prepares + re-signs the envelope so the freshest scrub runs at the
moment of contribution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..engine.model import Learning
from .identity import Contributor
from .contribute import prepare_contribution
from .github_backend import GitHubPool


@dataclass
class QueueItem:
    path: Path
    status: str
    learning: Learning

    @property
    def id(self) -> str:
        return self.learning.id


def list_queue(queue_dir: str | Path, *, status: Optional[str] = None) -> list[QueueItem]:
    d = Path(queue_dir).expanduser()
    if not d.exists():
        return []
    out: list[QueueItem] = []
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        lng = Learning.from_dict(rec.get("learning", {}))
        item = QueueItem(path=f, status=rec.get("status", "pending-review"), learning=lng)
        if status is None or item.status == status:
            out.append(item)
    return out


def set_status(item: QueueItem, status: str) -> None:
    rec = json.loads(item.path.read_text(encoding="utf-8"))
    rec["status"] = status
    item.path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    item.status = status


def publish_approved(
    queue_dir: str | Path,
    pool: GitHubPool,
    contributor: Contributor,
    *,
    project_terms: Optional[list[str]] = None,
    only_id: Optional[str] = None,
    github_user: str = "",
) -> list[dict]:
    """Publish every ``approved`` queue item (or just ``only_id``) to the pool.

    Returns a list of result dicts. Each item is re-prepared (scrub + sign) at
    publish time; if the scrub now blocks it, it is skipped with a reason (the
    floor still wins, even post-approval). ``github_user`` (Phase 7) is bound into
    the signature so the pool's CI can verify the PR author + count distinct accounts."""
    results: list[dict] = []
    for item in list_queue(queue_dir, status="approved"):
        if only_id and item.id != only_id:
            continue
        prep = prepare_contribution(item.learning, contributor,
                                    project_terms=project_terms, github_user=github_user)
        if not prep.ok:
            results.append({"id": item.id, "published": False, "reason": prep.reason})
            continue
        r = pool.publish(prep.envelope)
        results.append({
            "id": item.id, "published": r.ok, "detail": r.detail,
            "path": r.extra.get("path"), "pr_url": r.extra.get("pr_url"),
            "noop": r.extra.get("noop", False),
        })
        if r.ok:
            set_status(item, "published")
    return results


__all__ = ["QueueItem", "list_queue", "set_status", "publish_approved"]
