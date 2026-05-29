"""komi-learn pool — GitHub-backed Global Learnings (a repo of `.md` files).

Replaces the stubbed outbox/inbox with a real git-backed pool. Three operations:

  • sync()         — refresh a local mirror of the pool repo (clone or pull).
  • publish()      — write an approved learning as a `.md` file and propose it:
                     PR mode (default)   → new branch + commit + ``gh pr create``
                     local mode          → commit straight to the local repo
                                           (used for tests + local-only pools)
  • pull()         — read every `.md` in the local mirror, re-verify each
                     (id + signature + scrub), return accepted Learnings.

Everything is verified LOCALLY on pull — the repo, like any remote, is never
trusted blindly. Designed to degrade: if ``git``/``gh`` or the network are
missing, operations return a clear failure instead of raising into a hook.

Config: a ``PoolConfig`` carries the repo URL + local cache path. Until you
create the real repo, point ``repo_url`` at a local path (``file://`` or a plain
dir) and use ``mode="local"`` — the full flow runs with zero network/auth.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..engine.model import Learning
from .contribute import ingest_verify
from .repo_format import render_md, parse_md, repo_path_for, LEARNINGS_DIR


@dataclass
class PoolConfig:
    repo_url: str = ""                       # https/ssh GitHub URL, or a local path/file:// for tests
    cache_dir: str = ""                      # local mirror, e.g. ~/.claude/komi/pool/repo
    branch: str = "main"
    mode: str = "pr"                         # "pr" (open PRs) | "local" (commit directly; tests/local pools)
    require_signature: bool = True
    author_name: str = "komi-learn"
    author_email: str = "komi-learn@users.noreply.github.com"


@dataclass
class GitResult:
    ok: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


def _git(args: list[str], cwd: Optional[str] = None, timeout: int = 60) -> GitResult:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        return GitResult(False, "git-not-installed")
    except subprocess.TimeoutExpired:
        return GitResult(False, "git-timeout")
    if p.returncode != 0:
        return GitResult(False, (p.stderr or p.stdout).strip()[:400])
    return GitResult(True, p.stdout.strip())


def _have(cmd: str) -> bool:
    try:
        subprocess.run([cmd, "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


class GitHubPool:
    def __init__(self, config: PoolConfig):
        self.cfg = config
        self.cache = Path(config.cache_dir).expanduser() if config.cache_dir else None

    # ── sync ────────────────────────────────────────────────────────────

    def sync(self) -> GitResult:
        """Clone the pool repo into the cache, or pull if already present."""
        if not self.cfg.repo_url or not self.cache:
            return GitResult(False, "pool-not-configured")
        if (self.cache / ".git").exists():
            r = _git(["-C", str(self.cache), "pull", "--ff-only", "origin", self.cfg.branch])
            return r if r.ok else _git(["-C", str(self.cache), "fetch", "origin", self.cfg.branch])
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        return _git(["clone", "--depth", "1", "--branch", self.cfg.branch,
                     self.cfg.repo_url, str(self.cache)])

    # ── publish ───────────────────────────────────────────────────────────

    def publish(self, envelope: dict) -> GitResult:
        """Write the learning as a `.md` file and propose it to the pool.

        PR mode: create a branch, commit the file, push, and open a PR via ``gh``.
        Local mode: commit directly to the cache repo's branch (no remote/auth).
        Idempotent by path: if the file already exists with identical content, this
        is a no-op success (the content-addressed path means same lesson → same file)."""
        if not self.cache:
            return GitResult(False, "pool-not-configured")
        if not (self.cache / ".git").exists():
            # nothing synced yet; for local pools we can init on demand
            if self.cfg.mode == "local":
                init = self._ensure_local_repo()
                if not init.ok:
                    return init
            else:
                return GitResult(False, "cache-not-synced")

        rel = repo_path_for(envelope)
        target = self.cache / rel
        body = render_md(envelope)

        if target.exists() and target.read_text(encoding="utf-8") == body:
            return GitResult(True, "already-present", {"path": rel, "noop": True})

        lid = envelope["learning"]["id"]
        if self.cfg.mode == "local":
            return self._commit_local(rel, body, lid)
        return self._open_pr(rel, body, lid)

    def _commit_local(self, rel: str, body: str, lid: str) -> GitResult:
        target = self.cache / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        _git(["-C", str(self.cache), "add", rel])
        r = _git(["-C", str(self.cache), "-c", f"user.name={self.cfg.author_name}",
                  "-c", f"user.email={self.cfg.author_email}",
                  "commit", "-m", f"learn: {lid}"])
        return GitResult(r.ok, r.detail, {"path": rel, "committed": r.ok})

    def _open_pr(self, rel: str, body: str, lid: str) -> GitResult:
        if not _have("gh"):
            return GitResult(False, "gh-not-installed",
                             {"hint": "install GitHub CLI or use mode=local"})
        branch = f"learn/{lid.replace(':', '_')[:40]}"
        # fresh branch off the synced base
        _git(["-C", str(self.cache), "checkout", "-B", branch, f"origin/{self.cfg.branch}"])
        target = self.cache / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        _git(["-C", str(self.cache), "add", rel])
        commit = _git(["-C", str(self.cache), "-c", f"user.name={self.cfg.author_name}",
                       "-c", f"user.email={self.cfg.author_email}",
                       "commit", "-m", f"learn: {lid}"])
        if not commit.ok:
            return commit
        push = _git(["-C", str(self.cache), "push", "-u", "origin", branch])
        if not push.ok:
            return push
        title = envelope_title(body)
        pr = _gh(["pr", "create", "--repo", _repo_slug(self.cfg.repo_url),
                  "--base", self.cfg.branch, "--head", branch,
                  "--title", f"Add learning: {title}",
                  "--body", _pr_body(rel)], cwd=str(self.cache))
        return GitResult(pr.ok, pr.detail, {"branch": branch, "path": rel, "pr_url": pr.detail})

    # ── pull ───────────────────────────────────────────────────────────────

    def pull(self, *, categories: Optional[list[str]] = None,
             limit: Optional[int] = None) -> list[Learning]:
        """Read + locally re-verify every learning in the synced mirror."""
        if not self.cache or not (self.cache / LEARNINGS_DIR).exists():
            return []
        out: list[Learning] = []
        for md in sorted((self.cache / LEARNINGS_DIR).rglob("*.md")):
            env = parse_md(md.read_text(encoding="utf-8", errors="replace"))
            if env is None:
                continue
            rep = ingest_verify(env, require_signature=self.cfg.require_signature)
            if not rep.accepted:
                continue
            rec = env["learning"]
            if categories and rec.get("category") not in categories:
                continue
            lng = Learning.from_dict({**rec, "scope": "global"})
            lng.provenance.origin = "pool"
            out.append(lng)
            if limit and len(out) >= limit:
                break
        return out

    # ── helpers ─────────────────────────────────────────────────────────

    def _ensure_local_repo(self) -> GitResult:
        """For mode=local with no clone yet: clone if repo_url is a real path, else
        init a fresh repo in the cache (purely local pool)."""
        self.cache.mkdir(parents=True, exist_ok=True)
        src = self.cfg.repo_url
        if src and (Path(src.replace("file://", "")).exists()):
            return _git(["clone", src.replace("file://", ""), str(self.cache)])
        r = _git(["init", "-b", self.cfg.branch, str(self.cache)])
        return r


def _gh(args: list[str], cwd: Optional[str] = None, timeout: int = 60) -> GitResult:
    try:
        p = subprocess.run(["gh", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        return GitResult(False, "gh-not-installed")
    except subprocess.TimeoutExpired:
        return GitResult(False, "gh-timeout")
    if p.returncode != 0:
        return GitResult(False, (p.stderr or p.stdout).strip()[:400])
    return GitResult(True, p.stdout.strip())


def _repo_slug(repo_url: str) -> str:
    import re
    m = re.search(r"[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo_url or "")
    return m.group(1) if m else repo_url


def envelope_title(md_body: str) -> str:
    for line in md_body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:80]
    return "new learning"


def _pr_body(rel: str) -> str:
    return (
        f"Automated contribution from komi-learn.\n\n"
        f"- File: `{rel}`\n"
        f"- The fenced `komi` block is the verifiable record (content-addressed id "
        f"+ signature).\n"
        f"- CI re-verifies the id, the signature, and re-runs the safety scrub.\n\n"
        f"This learning was approved locally by the contributor before submission."
    )


__all__ = ["PoolConfig", "GitHubPool", "GitResult"]
