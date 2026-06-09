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
atomic temp-file + os.replace writes, deduped by content id.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from .model import Learning, LearningType, Scope, Visibility, _now_iso

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
        # Born safe-to-commit: drop a .gitignore so private (.local) learnings, the
        # derived index, the signing key, and state are NEVER version-controlled,
        # while the SHAREABLE MEMORY.md/USER.md/skills/ CAN be committed and travel
        # to clones. Idempotent + cheap (skips if present) — this is the structural
        # fix that lets project memory be shared without leaking confidential data.
        self._ensure_gitignore()
        # Stable key identifying this store's rows within a (possibly shared) index,
        # so reindex/clears only touch this store's own slice.
        self._root_key = str(self.root.resolve())
        # The index is shared across scopes by default (one brain), so it lives at
        # the personal root unless overridden. Each row records its own scope+source.
        self.index_path = Path(index_path).expanduser() if index_path else self.root / "index.db"
        self._db = self._open_db(self.index_path)

    # The gitignore that makes a komi root safe to version-control. The shareable
    # files are deliberately NOT ignored — committing them is the whole point of
    # project scope (a clone/teammate inherits the craft knowledge).
    # Patterns that MUST be ignored for a komi root to be safe to commit. `**/*.local.md`
    # is recursive (a nested private file can't escape `*.local.md`'s non-recursive
    # match). Shareable files (MEMORY.md/USER.md/skills/) are deliberately absent.
    _GITIGNORE_REQUIRED = [
        "*.local.md", "**/*.local.md", "skills.local/",
        "index.db", "index.db-shm", "index.db-wal",
        "state.json", "state.lock", "keys/", ".env",
    ]
    _GITIGNORE_HEADER = (
        "# komi-learn (managed) — private + derived data, never commit.\n"
        "# Shareable project memory (MEMORY.md, USER.md, skills/) IS meant to be committed.\n"
    )

    def _ensure_gitignore(self) -> None:
        """Make the komi root safe to commit. ADDITIVE: appends any required pattern
        that's missing to an existing .gitignore (preserving the user's own lines),
        rather than skipping when a .gitignore already exists — so an old root, or one
        with an unrelated .gitignore, still gets the private/derived patterns added."""
        gi = self.root / ".gitignore"
        try:
            existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
            present = {ln.strip() for ln in existing.splitlines()}
            missing = [p for p in self._GITIGNORE_REQUIRED if p not in present]
            if not missing:
                return
            block = self._GITIGNORE_HEADER + "\n".join(missing) + "\n"
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            gi.write_text(existing + sep + block, encoding="utf-8")
        except Exception:
            pass                         # best-effort; never fail store init on this

    # ── Markdown persistence ────────────────────────────────────────────

    def _md_path(self, learning_type: str, visibility: str = Visibility.SHAREABLE.value) -> Path:
        fname = _FILE_FOR_TYPE.get(learning_type)
        if not fname:
            raise ValueError(f"No markdown file for learning type {learning_type!r}")
        if visibility == Visibility.PRIVATE.value:
            # Private learnings live in a `.local` sibling (MEMORY.md -> MEMORY.local.md)
            # that install gitignores, so confidential knowledge is never committed —
            # while the shareable file CAN be committed and travels to clones.
            # Suffix-anchored (not str.replace) so a filename with '.md' elsewhere
            # can't be mangled.
            fname = (fname[:-3] + ".local.md") if fname.endswith(".md") else (fname + ".local.md")
        return self.root / fname

    def _md_paths_all(self, learning_type: str) -> list[Path]:
        """Both the shareable and private files for a type (for reads that must see
        everything locally — recall, reindex, all())."""
        return [self._md_path(learning_type, Visibility.SHAREABLE.value),
                self._md_path(learning_type, Visibility.PRIVATE.value)]

    def _purge_md_entry(self, learning_type: str, learning_id: str,
                        *, except_visibility: str) -> Optional[dict]:
        """Remove an entry with ``learning_id`` from the visibility file(s) OTHER than
        ``except_visibility`` (enforces single-residency on a visibility change).
        Returns the removed record (for telemetry carry-forward), or None."""
        removed = None
        for vis in (Visibility.SHAREABLE.value, Visibility.PRIVATE.value):
            if vis == except_visibility:
                continue
            path = self._md_path(learning_type, vis)
            entries = self._read_entries(path)
            kept = [e for e in entries if e.get("id") != learning_id]
            if len(kept) != len(entries):
                removed = next(e for e in entries if e.get("id") == learning_id)
                if kept:
                    text = ENTRY_DELIMITER.join(self._render_entry(e) for e in kept) + "\n"
                    self._atomic_write(path, text)
                elif path.exists():
                    path.unlink()      # last entry gone → remove the now-empty file
        return removed

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

    @staticmethod
    def _md_payload(rec: dict) -> dict:
        """Strip computed/transient fields before writing a record into Markdown (the
        source of truth). ``corroboration`` is a pull-time trust signal derived from
        pool signatures — it is NEVER content and must not be frozen into a local file,
        where it would go stale and (if later trusted) spoof a trust level. Recomputed
        on every pull; defaults to 1 for local learnings."""
        return {k: v for k, v in rec.items() if k != "corroboration"}

    def _render_entry(self, rec: dict) -> str:
        rec = self._md_payload(rec)
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
            # procedural learnings are persisted as skills/<slug>/SKILL.md
            return self._upsert_skill(learning)

        # Route by visibility: private → MEMORY.local.md (gitignored), shareable →
        # MEMORY.md (committable). A given id lives in exactly one of the two files.
        # If this learning's visibility changed since a prior write (e.g. the LLM
        # backstop reclassified it private on a second sighting), MOVE it: drop any
        # stale copy from the OTHER visibility file so a flip can't leave a leaked
        # copy behind in the committable file. Single-residency by construction.
        prior = self._purge_md_entry(learning.type, learning.id,
                                     except_visibility=learning.visibility)
        path = self._md_path(learning.type, learning.visibility)
        entries = self._read_entries(path)
        by_id = {e.get("id"): e for e in entries}
        # Carry forward ALL persistent telemetry from the moved copy (a re-distilled
        # learning is a FRESH object with zeroed usage/lifecycle), mirroring the
        # skill path's prior-merge — otherwise a visibility flip silently drops the
        # user's pin, true creation date, and reuse count (which the curator uses to
        # decide what's prunable).
        if prior and learning.id not in by_id:
            learning.confidence = max(learning.confidence, prior.get("confidence", 0.3))
            pu = prior.get("usage", {}) or {}
            learning.usage.reused = max(learning.usage.reused, pu.get("reused", 0) or 0)
            learning.usage.recalled = max(learning.usage.recalled, pu.get("recalled", 0) or 0)
            learning.usage.last_used = learning.usage.last_used or pu.get("last_used")
            pl = prior.get("lifecycle", {}) or {}
            if pl.get("created_at"):
                learning.lifecycle.created_at = pl["created_at"]
            learning.lifecycle.pinned = learning.lifecycle.pinned or bool(pl.get("pinned"))

        if learning.id in by_id:
            existing = by_id[learning.id]
            # corroboration: same content seen again → raise confidence, refresh ts
            existing["confidence"] = min(
                1.0, max(existing.get("confidence", 0.3), learning.confidence) + 0.1
            )
            existing.setdefault("lifecycle", {})["updated_at"] = _now_iso()
            # REUSE-CREDIT (observable, conservative): the agent independently re-derived
            # a lesson it already holds. If that lesson was ever SURFACED by recall
            # (recalled>0), this re-derivation is evidence it proved useful → credit reuse.
            # Gated on prior recall so we don't credit re-deriving something never shown
            # (that's mere corroboration, not reuse). This is the only place a non-zero
            # `reused` originates — it's what flips reuse_instrumented true.
            recalled_n, _ = self.recall_telemetry(learning.id)
            if recalled_n > 0:
                self.record_reused([learning.id])
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
            for path in self._md_paths_all(t):    # shareable + private (.local)
                out.extend(Learning.from_dict(e) for e in self._read_entries(path))
        out.extend(self._read_skills())
        return out

    def all_with_telemetry(self) -> list[Learning]:
        """``all()`` but with recall/reuse telemetry overlaid from the DB. Markdown is
        the content source of truth; usage counters (recalled/reused/last_used) live
        ONLY in the index (runtime state, not content — never written back to Markdown
        to avoid churn/leak). Any analytics that reasons about usefulness — corpus_health,
        the curator's keep-if-used / surfaced_never_used — MUST read through here, or it
        sees a frozen-0 counter and concludes (wrongly) that nothing is ever used."""
        tel: dict[str, tuple[int, int, Optional[str]]] = {}
        try:
            for r in self._db.execute(
                "SELECT id, MAX(COALESCE(recalled,0)) AS rc, MAX(COALESCE(reused,0)) AS ru, "
                "MAX(last_used) AS lu FROM learnings GROUP BY id"
            ):
                tel[r["id"]] = (int(r["rc"] or 0), int(r["ru"] or 0), r["lu"])
        except Exception:
            tel = {}
        out = self.all()
        for lng in out:
            rc, ru, lu = tel.get(lng.id, (0, 0, None))
            lng.usage.recalled = max(lng.usage.recalled, rc)
            lng.usage.reused = max(lng.usage.reused, ru)
            lng.usage.last_used = lng.usage.last_used or lu
        return out

    def reclassify_visibility(self) -> list[Learning]:
        """Re-run the confidential floor over existing SHAREABLE learnings and move
        any now-flagged-confidential ones to private (.local). Closes the migration
        gap: memory distilled before the visibility feature (or before a floor update)
        stays in the committable file until this is run. Returns the moved learnings.
        Deterministic (floor only — no LLM); a one-shot upgrade/audit step."""
        from .classify import safety_floor
        moved: list[Learning] = []
        for lng in self.all():
            if lng.visibility != Visibility.SHAREABLE.value:
                continue
            joined = " \n ".join([lng.title or "", lng.body or "", lng.trigger or "",
                                  " ".join(lng.tags or [])])
            if safety_floor(joined).confidential:
                lng.visibility = Visibility.PRIVATE.value
                lng.confidential = True    # so recall quarantines it too, not just commit/pool
                lng._normalize_visibility()   # keep the invariant: a global learning becomes project
                self.upsert(lng)          # single-residency moves it out of the committable file
                moved.append(lng)
        return moved

    # ── skills/ persistence (procedural learnings) ──────────────────────

    def _skills_dir(self, visibility: str = Visibility.SHAREABLE.value) -> Path:
        # Private procedural skills live in `skills.local/` (gitignored), shareable in
        # `skills/` (committable) — same split as MEMORY.md vs MEMORY.local.md.
        return self.root / ("skills.local" if visibility == Visibility.PRIVATE.value else "skills")

    def _skills_dirs_all(self) -> list[Path]:
        return [self._skills_dir(Visibility.SHAREABLE.value),
                self._skills_dir(Visibility.PRIVATE.value)]

    def _all_skill_files(self):
        """Every SKILL.md across skills/ and skills.local/ (for scan/erase paths)."""
        for d in self._skills_dirs_all():
            if d.exists():
                yield from d.glob("*/SKILL.md")

    def _skill_slug(self, learning: Learning) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", (learning.title or "skill").lower()).strip("-")[:48]
        return base or "skill"

    def _skill_dir_for(self, learning: Learning) -> Path:
        """Directory for a skill, keyed ONLY by the content id (not the title).

        Earlier this embedded the title-slug in the dir name, so editing a skill's
        title produced a NEW directory and orphaned the old one (two dirs, same id).
        Keying purely on the id makes the path stable across title edits — the
        human-readable name lives in the SKILL.md frontmatter, where edits are free."""
        short = learning.id.split(":", 1)[-1][:16]
        return self._skills_dir(learning.visibility) / short

    def _upsert_skill(self, learning: Learning) -> str:
        """Persist a procedural learning as ``skills/<id>/SKILL.md`` (id-keyed dir).

        The file carries agentskills.io-style frontmatter plus the verifiable JSON
        block, so it's a real skill on disk AND losslessly round-trippable."""
        existing = {l.id: l for l in self._read_skills()}
        if learning.id in existing:
            # corroboration: same content seen again — preserve telemetry + age
            prior = existing[learning.id]
            learning.confidence = min(1.0, max(prior.confidence, learning.confidence) + 0.1)
            learning.usage = prior.usage
            if prior.lifecycle.created_at:
                learning.lifecycle.created_at = prior.lifecycle.created_at
            learning.lifecycle.pinned = learning.lifecycle.pinned or prior.lifecycle.pinned
        # single-residency: if this skill's visibility changed, remove the stale dir
        # from the OTHER visibility tree so a flip can't leave a committable copy.
        import shutil as _shutil
        other = (Visibility.SHAREABLE.value if learning.visibility == Visibility.PRIVATE.value
                 else Visibility.PRIVATE.value)
        stale = self._skills_dir(other) / learning.id.split(":", 1)[-1][:16]
        if stale.exists():
            _shutil.rmtree(stale, ignore_errors=True)
        d = self._skill_dir_for(learning)
        d.mkdir(parents=True, exist_ok=True)
        self._atomic_write(d / "SKILL.md", self._render_skill(learning))
        self._index_one(learning, source="skill")
        return learning.id

    def _render_skill(self, lng: Learning) -> str:
        fm = {
            "name": self._skill_slug(lng),
            "description": (lng.title + (". " + lng.trigger if lng.trigger else "")).strip(),
            "komi_id": lng.id,
            "scope": lng.scope,
            "tags": list(lng.tags),
        }
        import json as _json
        skill_rec = self._md_payload(lng.to_dict())   # drop transient corroboration
        front = "\n".join(f"{k}: {_json.dumps(v) if isinstance(v, (list, dict)) else v}"
                          for k, v in fm.items())
        payload = _json.dumps(skill_rec, ensure_ascii=False, indent=2)
        return (
            f"---\n{front}\n---\n\n"
            f"# {lng.title}\n\n"
            f"{lng.body}\n\n"
            + (f"**Use when:** {lng.trigger}\n\n" if lng.trigger else "")
            + f"<!-- komi record (verifiable; do not hand-edit) -->\n"
            f"```komi\n{payload}\n```\n"
        )

    def _read_skills(self) -> list[Learning]:
        # Dedup by id, keeping the most-recently-modified file. This tolerates
        # legacy slug-named dirs (pre-id-keying) that may duplicate an id, so a
        # title edit in the old scheme can't surface two copies of one learning.
        # Reads BOTH skills/ (shareable) and skills.local/ (private).
        by_id: dict[str, tuple[float, Learning]] = {}
        for d in self._skills_dirs_all():
            if not d.exists():
                continue
            for skill_md in d.glob("*/SKILL.md"):
                rec = _extract_json_block(skill_md.read_text(encoding="utf-8", errors="replace"))
                if rec is None:
                    continue
                lng = Learning.from_dict(rec)
                mtime = skill_md.stat().st_mtime
                if lng.id not in by_id or mtime > by_id[lng.id][0]:
                    by_id[lng.id] = (mtime, lng)
        return [lng for _, lng in by_id.values()]

    def get(self, learning_id: str) -> Optional[Learning]:
        for lng in self.all():
            if lng.id == learning_id:
                return lng
        return None

    def archive(self, learning_id: str) -> bool:
        """Archive (never delete) — the maximum destructive action, per Hermes."""
        for t in _FILE_FOR_TYPE:
            for path in self._md_paths_all(t):     # shareable + private (.local)
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
        # skills: flip state in the SKILL.md record (file kept — archived, not deleted)
        for skill_md in self._all_skill_files():
            rec = _extract_json_block(skill_md.read_text(encoding="utf-8", errors="replace"))
            if rec and rec.get("id") == learning_id:
                lng = Learning.from_dict(rec)
                lng.lifecycle.state = "archived"
                self._atomic_write(skill_md, self._render_skill(lng))
                self._db.execute("UPDATE learnings SET state='archived' WHERE id=?",
                                 (learning_id,))
                self._db.commit()
                return True
        return False

    def delete(self, learning_id: str) -> bool:
        """TRUE erasure — remove the learning from Markdown, its skill dir, AND the
        index. This is the deliberate exception to the curator's "archive, never
        delete" rule: the user has an explicit right to permanently erase their own
        data (PAM "right to be forgotten" → ``komi-learn forget --hard``). Returns
        True if anything was removed."""
        import shutil
        removed = False
        # markdown-backed types: drop the matching entry entirely (both files)
        for t in _FILE_FOR_TYPE:
            for path in self._md_paths_all(t):
                entries = self._read_entries(path)
                kept = [e for e in entries if e.get("id") != learning_id]
                if len(kept) != len(entries):
                    if kept:
                        text = ENTRY_DELIMITER.join(self._render_entry(e) for e in kept) + "\n"
                        self._atomic_write(path, text)
                    elif path.exists():
                        path.unlink()      # last entry gone → remove the now-empty file
                    removed = True
        # skills: remove the whole skill directory (skills/ or skills.local/)
        for skill_md in self._all_skill_files():
            rec = _extract_json_block(skill_md.read_text(encoding="utf-8", errors="replace"))
            if rec and rec.get("id") == learning_id:
                shutil.rmtree(skill_md.parent, ignore_errors=True)
                removed = True
        # index: drop every row for this id (across origins)
        try:
            self._db.execute("DELETE FROM learnings WHERE id=?", (learning_id,))
            self._db.commit()
        except Exception:
            pass
        return removed

    # ── SQLite FTS index ────────────────────────────────────────────────

    @staticmethod
    def _open_db(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: block (don't instantly error) when another session holds a write
        # lock — several Claude Code windows share this one index.db.
        db = sqlite3.connect(str(path), timeout=10.0)
        db.row_factory = sqlite3.Row
        # WAL lets readers and a writer coexist (recall while the curator writes);
        # busy_timeout makes concurrent writers wait-and-retry instead of raising
        # "database is locked". Both are critical for multi-session safety.
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        db.executescript(
            """
            -- Row identity is (id, origin_root), NOT id alone. The index is shared
            -- across origins ("one brain": personal + project + each external mirror,
            -- namespaced by origin_root). The SAME content-addressed id can therefore
            -- legitimately appear once per origin — e.g. a lesson the user distilled
            -- locally (origin=local root) AND the same lesson pulled from the pool
            -- (origin='external:pool', carrying scope=global + a corroboration count).
            -- A single-column PK on id collapsed these: mirroring the pool copy would
            -- overwrite the local row's scope/origin_root, and the next pool sync's
            -- scoped DELETE would then evict the user's own learning. UNIQUE(id,
            -- origin_root) keeps them as distinct rows; recall dedups by id in Python.
            CREATE TABLE IF NOT EXISTS learnings (
                id TEXT,
                type TEXT, scope TEXT, category TEXT,
                title TEXT, body TEXT, trigger TEXT, tags TEXT,
                confidence REAL, reused INTEGER DEFAULT 0,
                recalled INTEGER DEFAULT 0,
                last_used TEXT, state TEXT DEFAULT 'active',
                source TEXT, origin_root TEXT, updated_at TEXT,
                embedding BLOB, embed_version TEXT,
                corroboration INTEGER DEFAULT 1,
                visibility TEXT DEFAULT 'shareable',
                confidential INTEGER DEFAULT 0,
                UNIQUE(id, origin_root)
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
        # Migrate older index.db files that predate newer columns.
        cols = {r[1] for r in db.execute("PRAGMA table_info(learnings)")}
        if "embedding" not in cols:
            db.execute("ALTER TABLE learnings ADD COLUMN embedding BLOB")
        if "embed_version" not in cols:
            db.execute("ALTER TABLE learnings ADD COLUMN embed_version TEXT")
        if "corroboration" not in cols:
            db.execute("ALTER TABLE learnings ADD COLUMN corroboration INTEGER DEFAULT 1")
        if "recalled" not in cols:
            db.execute("ALTER TABLE learnings ADD COLUMN recalled INTEGER DEFAULT 0")
        if "visibility" not in cols:
            db.execute("ALTER TABLE learnings ADD COLUMN visibility TEXT DEFAULT 'shareable'")
        if "confidential" not in cols:
            db.execute("ALTER TABLE learnings ADD COLUMN confidential INTEGER DEFAULT 0")
        db.commit()
        Store._migrate_row_identity(db)
        return db

    @staticmethod
    def _migrate_row_identity(db: sqlite3.Connection) -> None:
        """Migrate legacy index.db files whose `learnings` table had a single-column
        PRIMARY KEY on `id` to the (id, origin_root) identity. SQLite can't ALTER a
        primary key, so we rebuild the table. The derived index can always be
        regenerated from Markdown, so this is safe even if the copy is lossy — but we
        preserve rows (incl. DB-only telemetry + embeddings) to avoid a needless
        re-embed/re-sync. Idempotent: a no-op once the table already has the
        UNIQUE(id, origin_root) shape. FTS triggers key on rowid, untouched here."""
        try:
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='learnings'"
            ).fetchone()
            sql = (row[0] if row else "") or ""
            # Old shape had "id TEXT PRIMARY KEY"; new shape has UNIQUE(id, origin_root).
            if "PRIMARY KEY" not in sql.upper():
                return  # already migrated (or fresh table)

            db.execute("PRAGMA foreign_keys=OFF")
            db.executescript(
                """
                BEGIN;
                DROP TRIGGER IF EXISTS learnings_ai;
                DROP TRIGGER IF EXISTS learnings_ad;
                DROP TRIGGER IF EXISTS learnings_au;
                ALTER TABLE learnings RENAME TO learnings_old;
                CREATE TABLE learnings (
                    id TEXT,
                    type TEXT, scope TEXT, category TEXT,
                    title TEXT, body TEXT, trigger TEXT, tags TEXT,
                    confidence REAL, reused INTEGER DEFAULT 0,
                    recalled INTEGER DEFAULT 0,
                    last_used TEXT, state TEXT DEFAULT 'active',
                    source TEXT, origin_root TEXT, updated_at TEXT,
                    embedding BLOB, embed_version TEXT,
                    corroboration INTEGER DEFAULT 1,
                    visibility TEXT DEFAULT 'shareable',
                    confidential INTEGER DEFAULT 0,
                    UNIQUE(id, origin_root)
                );
                INSERT OR IGNORE INTO learnings
                    (id, type, scope, category, title, body, trigger, tags, confidence,
                     reused, recalled, last_used, state, source, origin_root, updated_at,
                     embedding, embed_version, corroboration, visibility, confidential)
                SELECT id, type, scope, category, title, body, trigger, tags, confidence,
                     reused, recalled, last_used, state, source, origin_root, updated_at,
                     embedding, embed_version, corroboration, visibility, confidential
                FROM learnings_old;
                DROP TABLE learnings_old;
                -- rebuild the FTS shadow + triggers (rowids changed on copy)
                INSERT INTO learnings_fts(learnings_fts) VALUES('delete-all');
                INSERT INTO learnings_fts(rowid, title, body, trigger, tags)
                    SELECT rowid, title, body, trigger, tags FROM learnings;
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
                COMMIT;
                """
            )
        except Exception:
            # Never let a migration failure brick the index — it's derived and can be
            # rebuilt from Markdown via reindex(). Roll back a partial migration.
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass

    # ── embeddings (semantic recall) ────────────────────────────────────

    @staticmethod
    def _pack_vec(vec: list[float]) -> bytes:
        import struct
        return struct.pack(f"<{len(vec)}f", *vec)

    @staticmethod
    def _unpack_vec(blob) -> list[float]:
        import struct
        if not blob:
            return []
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))

    def embed_pending(self, embedder) -> int:
        """Compute + store embeddings for active rows missing one (or stale version).
        Called lazily before semantic recall and after distill. Returns count embedded.
        Best-effort: a single encode failure skips that row, never raises."""
        if embedder is None:
            return 0
        ver = getattr(embedder, "version", "?")
        rows = self._db.execute(
            "SELECT id, title, body, trigger, tags FROM learnings "
            "WHERE state='active' AND (embedding IS NULL OR embed_version IS NOT ? OR embed_version != ?)",
            (ver, ver),
        ).fetchall()
        n = 0
        for r in rows:
            text = " \n ".join(filter(None, [r["title"], r["body"], r["trigger"], r["tags"]]))
            try:
                vec = embedder.encode(text)
            except Exception:
                vec = []
            if not vec:
                continue
            self._db.execute("UPDATE learnings SET embedding=?, embed_version=? WHERE id=?",
                             (self._pack_vec(vec), ver, r["id"]))
            n += 1
        if n:
            self._db.commit()
        return n

    def vector_search(self, query_vec: list[float], *, limit: int = 20,
                      scopes: Optional[list[str]] = None) -> list:
        """Rank active learnings by cosine similarity to ``query_vec``. Returns rows
        with an added ``sim`` float (1.0 = identical). Pure-Python cosine over the
        candidate set — fine for the thousands-of-learnings scale this targets."""
        from .embed import cosine
        if not query_vec:
            return []
        sql = "SELECT * FROM learnings WHERE state='active' AND embedding IS NOT NULL"
        params: list = []
        if scopes:
            sql += " AND scope IN (%s)" % ",".join("?" * len(scopes))
            params += scopes
        scored = []
        for r in self._db.execute(sql, params):
            sim = cosine(query_vec, self._unpack_vec(r["embedding"]))
            d = dict(r)
            d["sim"] = sim
            scored.append(d)
        scored.sort(key=lambda d: d["sim"], reverse=True)
        return scored[:limit]

    def _index_one(self, lng: Learning, *, source: str) -> None:
        self._db.execute(
            """
            INSERT INTO learnings (id, type, scope, category, title, body, trigger,
                                   tags, confidence, reused, recalled, last_used, state,
                                   source, origin_root, updated_at, corroboration,
                                   visibility, confidential)
            VALUES (:id,:type,:scope,:category,:title,:body,:trigger,:tags,
                    :confidence,:reused,:recalled,:last_used,:state,:source,:origin_root,
                    :updated_at,:corroboration,:visibility,:confidential)
            ON CONFLICT(id, origin_root) DO UPDATE SET
                scope=excluded.scope, category=excluded.category, title=excluded.title,
                body=excluded.body, trigger=excluded.trigger, tags=excluded.tags,
                confidence=excluded.confidence, state=excluded.state,
                source=excluded.source,
                updated_at=excluded.updated_at, corroboration=excluded.corroboration,
                visibility=excluded.visibility, confidential=excluded.confidential
            """,
            {
                "id": lng.id, "type": lng.type, "scope": lng.scope,
                "category": lng.category, "title": lng.title, "body": lng.body,
                "trigger": lng.trigger, "tags": " ".join(lng.tags),
                "confidence": lng.confidence, "reused": lng.usage.reused,
                "recalled": lng.usage.recalled,
                "last_used": lng.usage.last_used, "state": lng.lifecycle.state,
                "source": source, "origin_root": self._root_key,
                "updated_at": lng.lifecycle.updated_at,
                "corroboration": max(1, getattr(lng, "corroboration", 1) or 1),
                "visibility": getattr(lng, "visibility", Visibility.SHAREABLE.value),
                "confidential": 1 if getattr(lng, "confidential", False) else 0,
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

        IMPORTANT: usage telemetry (reused/last_used) lives only in the DB — it is
        runtime state, not content, so it isn't in the Markdown source. We must
        therefore PRESERVE it across a reindex (read it first, reapply it after),
        or every reindex would zero out recall/reuse counts and make active
        learnings look prunable. See the data-loss fix.
        """
        prior = {
            r["id"]: (r["reused"] or 0, r["last_used"], r["recalled"] or 0)
            for r in self._db.execute(
                "SELECT id, reused, last_used, recalled FROM learnings WHERE origin_root=?",
                (self._root_key,),
            )
        }
        self._db.execute("DELETE FROM learnings WHERE origin_root=?", (self._root_key,))
        self._db.commit()
        n = 0
        for lng in list(self.all()) + list(extra):
            self._reapply_usage(lng, prior)
            self._index_one(lng, source=_FILE_FOR_TYPE.get(lng.type, "skill"))
            n += 1
        return n

    @staticmethod
    def _reapply_usage(lng: Learning, prior: dict) -> None:
        """Carry forward DB-only telemetry onto a freshly-loaded learning so a
        reindex never loses it. Keeps the max of file vs prior (corroboration may
        have bumped the file's confidence; usage only ever grows)."""
        if lng.id in prior:
            reused, last_used, recalled = prior[lng.id]
            lng.usage.reused = max(lng.usage.reused, reused)
            lng.usage.last_used = lng.usage.last_used or last_used
            lng.usage.recalled = max(lng.usage.recalled, recalled)

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

    # ── public telemetry / mirror API ───────────────────────────────────
    # These exist so consumers (recall, the adapter) stop reaching into Store
    # internals (_db, _index_one, _root_key). The encapsulation lets the index
    # backend change later without hunting down private accesses.

    def record_recalled(self, ids: list[str], *, when: Optional[str] = None) -> None:
        """Mark learnings as recalled: bump the per-id recall counter and stamp
        last_used. Both live in the DB (runtime telemetry, not content) and are
        carried back into Markdown on the next reindex/upsert so the signal is
        visible and the curator can rank on it. De-dups ids within a single recall
        so one surfaced learning counts once per recall event, not once per origin
        row it happens to have."""
        if not ids:
            return
        ts = when or _now_iso()
        seen = list(dict.fromkeys(ids))   # preserve order, drop dup ids in this batch
        try:
            self._db.executemany(
                "UPDATE learnings SET recalled = COALESCE(recalled, 0) + 1, last_used=? "
                "WHERE id=?",
                [(ts, i) for i in seen],
            )
            self._db.commit()
        except Exception:
            pass

    def record_reused(self, ids: list[str], *, when: Optional[str] = None) -> int:
        """Credit reuse — the STRONGER usage signal — for learnings that were acted on.
        Bumps the per-id reuse counter and refreshes last_used. Like recall telemetry it
        lives in the DB and is carried back to Markdown on the next reindex/upsert.
        De-dups ids within a batch. Returns how many distinct ids were credited.

        Reuse is what `surfaced_never_used` and the curator's keep-if-used guard depend
        on; until something calls this, those read a frozen-0 counter. The distiller
        calls it when a recalled lesson is independently re-derived (see upsert)."""
        if not ids:
            return 0
        ts = when or _now_iso()
        seen = list(dict.fromkeys(ids))
        try:
            self._db.executemany(
                "UPDATE learnings SET reused = COALESCE(reused, 0) + 1, last_used=? "
                "WHERE id=?",
                [(ts, i) for i in seen],
            )
            self._db.commit()
            return len(seen)
        except Exception:
            return 0

    def recall_telemetry(self, learning_id: str) -> tuple[int, int]:
        """(recalled, reused) counters for an id from the DB (max across origin rows).
        Lets the distiller decide whether a re-derived lesson was ever SURFACED before
        crediting reuse — re-deriving a lesson the user was never shown isn't 'reuse'."""
        try:
            row = self._db.execute(
                "SELECT MAX(COALESCE(recalled,0)) AS r, MAX(COALESCE(reused,0)) AS u "
                "FROM learnings WHERE id=?",
                (learning_id,),
            ).fetchone()
            if row is None:
                return (0, 0)
            return (int(row["r"] or 0), int(row["u"] or 0))
        except Exception:
            return (0, 0)

    def embeddings_by_id(self) -> dict:
        """Map of learning id → its persisted embedding (unpacked), for active rows
        that have one. Lets the curator reuse vectors recall already computed instead
        of re-encoding every candidate. If the same id exists under multiple origins
        (local + pool), either copy's vector is fine — the content (hence embedding)
        is identical by content-addressing."""
        out: dict = {}
        try:
            for r in self._db.execute(
                "SELECT id, embedding FROM learnings WHERE state='active' AND embedding IS NOT NULL"
            ):
                if r["id"] not in out:
                    out[r["id"]] = self._unpack_vec(r["embedding"])
        except Exception:
            pass
        return out

    def mirror_external(self, learnings: Iterable[Learning], *, source: str) -> int:
        """Index externally-sourced learnings (e.g. the synced global pool) WITHOUT
        writing local Markdown — they live in their own origin_root namespace so a
        local reindex never clobbers them and vice-versa. Returns count."""
        ext = Store.__new__(Store)          # lightweight view sharing this db
        ext.root = self.root / "_external" / source
        ext._root_key = f"external:{source}"
        ext.index_path = self.index_path
        ext._db = self._db
        ext._db.execute("DELETE FROM learnings WHERE origin_root=?", (ext._root_key,))
        n = 0
        for lng in learnings:
            ext._index_one(lng, source=source)
            n += 1
        ext._db.commit()
        return n

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
