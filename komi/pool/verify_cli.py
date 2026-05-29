"""komi-learn pool — standalone verifier for pool `.md` files.

Used by the komi-pool repo's CI to gate every PR, and runnable locally. It checks,
for each `.md` under ``learnings/``:

  1. it parses (has a valid fenced ``komi`` envelope),
  2. the content-addressed id matches the content (tamper-evidence),
  3. the signature verifies against the embedded signer key (when required),
  4. the safety scrub finds NO secrets/PII/identifiers (defense-in-depth — the
     contributor scrubbed locally; CI scrubs again so nothing private merges),
  5. the file lives at the correct content-addressed path,
  6. the schema/fields are well-formed.

Exit code is non-zero if any checked file fails, so CI blocks the merge.

Usage:
    python -m komi.pool.verify_cli [PATH ...]      # default: learnings/
    python -m komi.pool.verify_cli --changed a.md b.md
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..engine.model import verify_id
from ..engine.classify import safety_floor
from .identity import verify_signature
from .contribute import _signing_message
from .repo_format import parse_md, repo_path_for, LEARNINGS_DIR


def check_file(path: Path, *, require_signature: bool = True, repo_root: Path | None = None) -> list[str]:
    """Return a list of problems (empty = OK)."""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    env = parse_md(text)
    if env is None:
        return [f"{path}: no valid `komi` envelope block"]

    learning = env.get("learning", {})

    # required fields
    for fld in ("id", "schema", "type", "category", "title", "body"):
        if not learning.get(fld):
            problems.append(f"{path}: missing required field '{fld}'")

    # content integrity
    if not verify_id(learning):
        problems.append(f"{path}: id does not match content (tampered or malformed)")

    # signature
    sig = learning.get("provenance", {}).get("signature")
    pk = env.get("signer", {}).get("public_key", "")
    if require_signature:
        if not verify_signature(_signing_message(learning, signer_public_key=pk), sig or "", pk):
            problems.append(f"{path}: signature missing or invalid")

    # scrub (no private data may merge)
    joined = " \n ".join([learning.get("title", ""), learning.get("body", ""),
                          learning.get("trigger", ""), " ".join(learning.get("tags", []))])
    floor = safety_floor(joined)
    if floor.blocked:
        problems.append(f"{path}: scrub failed ({', '.join(floor.reasons)})")

    # correct content-addressed location
    expected = repo_path_for(env)
    if repo_root is not None:
        actual = path.relative_to(repo_root).as_posix()
        if actual != expected:
            problems.append(f"{path}: wrong path; expected {expected}")

    return problems


def main(argv: list[str]) -> int:
    require_sig = "--no-signature" not in argv
    argv = [a for a in argv if a != "--no-signature"]

    # --root lets us verify a repo other than cwd (local checks, monorepos).
    repo_root = Path.cwd()
    if "--root" in argv:
        i = argv.index("--root")
        repo_root = Path(argv[i + 1]).resolve()
        del argv[i:i + 2]

    if argv and argv[0] == "--changed":
        files = [Path(p) for p in argv[1:] if p.endswith(".md")]
    elif argv:
        files = [Path(p) for p in argv if p.endswith(".md")]
    else:
        files = sorted((repo_root / LEARNINGS_DIR).rglob("*.md")) if (repo_root / LEARNINGS_DIR).exists() else []

    if not files:
        print("komi-pool verify: no learning files to check.")
        return 0

    all_problems: list[str] = []
    for f in files:
        if not f.exists():
            continue
        all_problems.extend(check_file(f, require_signature=require_sig, repo_root=repo_root))

    if all_problems:
        print(f"komi-pool verify: FAILED ({len(all_problems)} problem(s)):")
        for p in all_problems:
            print(f"  ✗ {p}")
        return 1
    print(f"komi-pool verify: OK ({len(files)} file(s) checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
