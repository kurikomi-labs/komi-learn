"""komi-learn pool — the on-repo `.md` file format for Global Learnings.

The pool is a GitHub repo of Markdown files (no server). Each learning is one
`.md` file that is BOTH human-readable (so a reviewer can read a PR diff and
understand exactly what's being shared) AND machine-verifiable (the full signed,
content-addressed envelope lives in a fenced ```komi block, byte-for-byte the
thing the BLAKE3 id and Ed25519 signature were computed over).

Path layout (content-addressed → natural dedup + corroboration):

    learnings/<category>/<id>.md

where ``<id>`` is the learning id with ':' → '_' (path-safe). Because the id is
the hash of the content, two people who independently distill the same lesson
produce the *same path* — so a duplicate is a no-op, and a second contributor
signing the same file is *corroboration*, not a conflict.

See docs/02-architecture.md §7 and the komi-pool repo template.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Optional


LEARNINGS_DIR = "learnings"
_FENCE = "komi"


def id_to_filename(learning_id: str) -> str:
    """``blake3:9f86…`` → ``blake3_9f86….md``. Path-safe and reversible."""
    safe = learning_id.replace(":", "_")
    # defensive: strip anything that isn't hash-ish so a crafted id can't escape the dir
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", safe)
    return f"{safe}.md"


def repo_path_for(envelope: dict) -> str:
    """POSIX repo-relative path for an envelope, e.g.
    ``learnings/debugging/blake3_9f86….md``. Category is slugified + whitelisted."""
    learning = envelope["learning"]
    category = _slug(learning.get("category") or "uncategorized")
    return str(PurePosixPath(LEARNINGS_DIR) / category / id_to_filename(learning["id"]))


def render_md(envelope: dict) -> str:
    """Render an approved, signed envelope to its `.md` file body.

    Layout: a short human-readable header (title, when, category, signer) followed
    by the body prose, then the canonical envelope JSON in a fenced block. The JSON
    block is the source of truth for verification; the prose above it is for humans.
    """
    learning = envelope["learning"]
    signer = envelope.get("signer", {})
    title = (learning.get("title") or "").strip()
    body = (learning.get("body") or "").strip()
    trigger = (learning.get("trigger") or "").strip()
    tags = learning.get("tags") or []

    payload = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)

    lines = [
        f"# {title}",
        "",
        f"> **Category:** {learning.get('category', '')}  ",
        f"> **Type:** {learning.get('type', '')}  ",
        f"> **Use when:** {trigger or '—'}  ",
        f"> **Tags:** {', '.join(tags) if tags else '—'}  ",
        f"> **Signer:** `{signer.get('public_key', '(unsigned)')[:16]}…` ({signer.get('algo', 'unsigned')})  ",
        f"> **ID:** `{learning.get('id', '')}`",
        "",
        body,
        "",
        "<!-- The block below is the verifiable record. Do not hand-edit; the id is",
        "     the hash of its content and edits will fail CI verification. -->",
        "",
        f"```{_FENCE}",
        payload,
        "```",
        "",
    ]
    return "\n".join(lines)


def parse_md(text: str) -> Optional[dict]:
    """Extract the envelope dict from a pool `.md` file. Returns None if absent
    or malformed (caller treats that as 'skip this file')."""
    start = text.find(f"```{_FENCE}")
    if start == -1:
        return None
    start = text.find("\n", start) + 1
    end = text.find("```", start)
    if end == -1:
        return None
    try:
        obj = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "learning" not in obj:
        return None
    return obj


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "uncategorized"


__all__ = ["LEARNINGS_DIR", "id_to_filename", "repo_path_for", "render_md", "parse_md"]
