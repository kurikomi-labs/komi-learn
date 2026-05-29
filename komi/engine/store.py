"""komi-learn — the Store API.

Two layers, deliberately:

* **Markdown files** are the human-readable source of truth, in Claude Code's own
  conventions so they stay useful even with the plugin off:
    - ``USER.md``         — identity learnings (who the user is)
    - ``MEMORY.md``       — semantic learnings (durable facts)
    - ``skills/<n>/SKILL.md`` — procedural learnings (handled by skills.py later)
  Entries are separated by the section sign ``§`` on its own line, exactly like
  Hermes, so a human (or the host) can read and hand-edit them.

* **``index.db`` (SQLite + FTS5)** is a *derived* cache: every learning mirrored
  as a row plus a full-text row, so Recall and the Curator can query fast. It can
  always be rebuilt from the Markdown by :meth:`reindex`.

Writes are atomic (temp file + os.replace) and deduped by content id, following
the patterns in docs/02-architecture.md §3.2.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from .model import Learning, LearningType, Scope, _now_iso

ENTRY_DELIMITER = "\n§\n"          # U+00A7, matches Hermes' MEMORY/USER format
_FILE_FOR_TYPE = {
    LearningType.IDENTITY.value: "USER.md",
    LearningType.SEMANTIC.value: "MEMORY.md",
}


class Store:
    """Owns one komi root (e.g. ``~/.claude/komi`` for personal, or
    ``<proj>/.claude/komi`` for project scope) plus its slice of the index."""

    def __init__(self, root: str | Path, *, index_path: Optional[str | Path] = None):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        # Stable key identifying this store's rows within a (possibly shared) index,
        # so reindex/clears only touch this store's own slice.
        self._root_key = str(self.root.resolve())
        # The index is shared across scopes by default (one brain), so it lives at
        # the personal root unless overridden. Each row records its own scope+source.
        self.index_path = Path(index_path).expanduser() if index_path else self.root / "index.db"
        self._db = self._open_db(self.index_path)

    # ── Markdown persistence ────────────────────────────────────────────

    def _md_path(self, learning_type: str) -> Path:
        fname = _FILE_FOR_TYPE.get(learning_type)
        if not fname:
            raise ValueError(f"No markdown file for learning type {learning_type!r}")
        return self.root / fname

    def _read_entries(self, path: Path) -> list[dict]:
        """Each entry is a fenced JSON block between § delimiters. We store the
        full record as JSON inside the Markdown so the file is both human-readable
        (title/body rendered) and losslessly round-trippable. Format per entry:

            <!-- komi:<id> -->
            ### <title>
            <body>
            ```komi
            {full json record}
            ```
        """
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8")
        out: list[dict] = []
        for chunk in raw.split(ENTRY_DELIMITER):
            chunk = chunk.strip()
            if not chunk:
                continue
            rec = _extract_json_block(chunk)
            if rec is not None:
                out.append(rec)
        return out

    def _render_entry(self, rec: dict) -> str:
        title = (rec.get("title") or "").strip()
        body = (rec.get("body") or "").strip()
        lid = rec.get("id", "")
        payload = json.dumps(rec, ensure_ascii=False, indent=2)
        return (
            f"<!-- komi:{lid} -->\n"
            f"### {title}\n"
            f"{body}\n"
            f"```komi\n{payload}\n```"
        )

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ── Public CRUD ─────────────────────────────────────────────────────

    def upsert(self, learning: Learning) -> str:
        """Insert or merge a learning. Dedup is by content id: if an entry with
        the same id exists, we *corroborate* it (bump confidence, keep the higher)
        rather than duplicate — this is how a repeated lesson gains trust over
        time instead of cluttering the file."""
        if not learning.id:
            learning.finalize()
        if learning.type not in _FILE_FOR_TYPE:
            # procedural → skills, handled elsewhere; index it but don't write MD here
            self._index_one(learning, source="skill")
            return learning.id

        path = self._md_path(learning.type)
        entries = self._read_entries(path)
        by_id = {e.get("id"): e for e in entries}

        if learning.id in by_id:
            existing = by_id[learning.id]
            # corroboration: same content seen again → raise confidence, refresh ts
            existing["confidence"] = min(
                1.0, max(existing.get("confidence", 0.3), learning.confidence) + 0.1
            )
            existing.setdefault("lifecycle", {})["updated_at"] = _now_iso()
        else:
            by_id[learning.id] = learning.to_dict()

        ordered = list(by_id.values())
        text = ENTRY_DELIMITER.join(self._render_entry(e) for e in ordered) + "\n"
        self._atomic_write(path, text)
        self._index_one(Learning.from_dict(by_id[learning.id]),
                         source=_FILE_FOR_TYPE[learning.type])
        return learning.id

    def all(self) -> list[Learning]:
        out: list[Learning] = []
        for t in _FILE_FOR_TYPE:
            out.extend(Learning.from_dict(e) for e in self._read_entries(self._md_path(t)))
        return out

    def get(self, learning_id: str) -> Optional[Learning]:
        for lng in self.all():
            if lng.id == learning_id:
                return lng
        return None

    def archive(self, learning_id: str) -> bool:
        """Archive (never delete) — the maximum destructive action, per Hermes."""
        for t in _FILE_FOR_TYPE:
            path = self._md_path(t)
            entries = self._read_entries(path)
            changed = False
            for e in entries:
                if e.get("id") == learning_id:
                    e.setdefault("lifecycle", {})["state"] = "archived"
                    changed = True
            if changed:
                text = ENTRY_DELIMITER.join(self._render_entry(e) for e in entries) + "\n"
                self._atomic_write(path, text)
                self._db.execute("UPDATE learnings SET state='archived' WHERE id=?",
                                 (learning_id,))
                self._db.commit()
                return True
        return False

    # ── SQLite FTS index ────────────────────────────────────────────────

    @staticmethod
    def _open_db(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path))
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS learnings (
                id TEXT PRIMARY KEY,
                type TEXT, scope TEXT, category TEXT,
                title TEXT, body TEXT, trigger TEXT, tags TEXT,
                confidence REAL, reused INTEGER DEFAULT 0,
                last_used TEXT, state TEXT DEFAULT 'active',
                source TEXT, origin_root TEXT, updated_at TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5(
                title, body, trigger, tags,
                content='learnings', content_rowid='rowid'
            );
            -- keep the FTS shadow table in sync with the base table
            CREATE TRIGGER IF NOT EXISTS learnings_ai AFTER INSERT ON learnings BEGIN
                INSERT INTO learnings_fts(rowid, title, body, trigger, tags)
                VALUES (new.rowid, new.title, new.body, new.trigger, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS learnings_ad AFTER DELETE ON learnings BEGIN
                INSERT INTO learnings_fts(learnings_fts, rowid, title, body, trigger, tags)
                VALUES ('delete', old.rowid, old.title, old.body, old.trigger, old.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS learnings_au AFTER UPDATE ON learnings BEGIN
                INSERT INTO learnings_fts(learnings_fts, rowid, title, body, trigger, tags)
                VALUES ('delete', old.rowid, old.title, old.body, old.trigger, old.tags);
                INSERT INTO learnings_fts(rowid, title, body, trigger, tags)
                VALUES (new.rowid, new.title, new.body, new.trigger, new.tags);
            END;
            """
        )
        db.commit()
        return db

    def _index_one(self, lng: Learning, *, source: str) -> None:
        self._db.execute(
            """
            INSERT INTO learnings (id, type, scope, category, title, body, trigger,
                                   tags, confidence, reused, last_used, state, source,
                                   origin_root, updated_at)
            VALUES (:id,:type,:scope,:category,:title,:body,:trigger,:tags,
                    :confidence,:reused,:last_used,:state,:source,:origin_root,:updated_at)
            ON CONFLICT(id) DO UPDATE SET
                scope=excluded.scope, category=excluded.category, title=excluded.title,
                body=excluded.body, trigger=excluded.trigger, tags=excluded.tags,
                confidence=excluded.confidence, state=excluded.state,
                source=excluded.source, origin_root=excluded.origin_root,
                updated_at=excluded.updated_at
            """,
            {
                "id": lng.id, "type": lng.type, "scope": lng.scope,
                "category": lng.category, "title": lng.title, "body": lng.body,
                "trigger": lng.trigger, "tags": " ".join(lng.tags),
                "confidence": lng.confidence, "reused": lng.usage.reused,
                "last_used": lng.usage.last_used, "state": lng.lifecycle.state,
                "source": source, "origin_root": self._root_key,
                "updated_at": lng.lifecycle.updated_at,
            },
        )
        self._db.commit()

    def reindex(self, extra: Iterable[Learning] = ()) -> int:
        """Rebuild THIS store's slice of the index from its Markdown files (+ any
        extra learnings, e.g. scanned skills). Returns row count.

        The index may be shared across stores (personal + project share one
        ``index.db`` — the "one brain"). So we only clear rows that belong to this
        store's root, never the whole table — otherwise a project reindex would
        wipe personal rows (and vice-versa). Rows are namespaced by ``origin_root``.
        """
        self._db.execute("DELETE FROM learnings WHERE origin_root=?", (self._root_key,))
        self._db.commit()
        n = 0
        for lng in self.all():
            self._index_one(lng, source=_FILE_FOR_TYPE.get(lng.type, "?"))
            n += 1
        for lng in extra:
            self._index_one(lng, source="skill")
            n += 1
        return n

    def search(self, query: str, *, limit: int = 20,
               scopes: Optional[list[str]] = None) -> list[sqlite3.Row]:
        """FTS5 search over active learnings, optionally scoped. Returns rows with a
        ``rank`` column (lower = better match). Recall layers its own scoring on top."""
        q = _fts_query(query)
        if not q:
            return []
        sql = (
            "SELECT l.*, bm25(learnings_fts) AS rank "
            "FROM learnings_fts JOIN learnings l ON l.rowid = learnings_fts.rowid "
            "WHERE learnings_fts MATCH ? AND l.state='active' "
        )
        params: list = [q]
        if scopes:
            sql += "AND l.scope IN (%s) " % ",".join("?" * len(scopes))
            params += scopes
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            return list(self._db.execute(sql, params))
        except sqlite3.OperationalError:
            return []

    def rows(self, *, state: str = "active") -> list[sqlite3.Row]:
        return list(self._db.execute("SELECT * FROM learnings WHERE state=?", (state,)))

    def close(self) -> None:
        self._db.close()


# ── helpers ──────────────────────────────────────────────────────────────

def _extract_json_block(chunk: str) -> Optional[dict]:
    """Pull the ```komi … ``` JSON payload out of a rendered entry."""
    start = chunk.find("```komi")
    if start == -1:
        return None
    start = chunk.find("\n", start) + 1
    end = chunk.find("```", start)
    if end == -1:
        return None
    try:
        return json.loads(chunk[start:end])
    except json.JSONDecodeError:
        return None


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of the salient terms.

    We quote each token to neutralize FTS5 operators and keep only word-ish
    tokens, so arbitrary prompt text can't crash the match parser.
    """
    import re
    toks = re.findall(r"[A-Za-z0-9_+#./-]{2,}", text.lower())
    toks = [t for t in toks if t not in _STOP][:24]
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))


_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "is", "are", "was", "this", "that", "it", "as", "at", "by", "be", "you", "i",
    "do", "how", "what", "when", "can", "will", "should", "would", "please",
}


__all__ = ["Store", "ENTRY_DELIMITER"]
