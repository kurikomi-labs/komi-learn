#!/usr/bin/env python3
"""Self-contained verifier for komi-pool learning files (CI safety gate).

This script is VENDORED into the pool repo on purpose: it has no dependency on
the (private) komi-learn code package — only ``blake3`` and ``pynacl`` from PyPI.
That keeps the pool repo verifiable on its own and decoupled from the code repo.

It must stay in sync with komi-learn's canonicalization + verification logic
(komi/engine/model.py, komi/pool/contribute.py, komi/pool/identity.py,
komi/engine/classify.py). The pieces reproduced here are small and stable.

Checks, for every learning .md under learnings/ (or just the files passed):
  1. parses (valid fenced ``komi`` envelope with required fields)
  2. content-addressed id matches the content  (tamper-evidence)
  3. Ed25519 signature verifies against the embedded signer key
  4. safety scrub finds no secrets / PII / machine identifiers
  5. file lives at the correct content-addressed path

Exit non-zero if any file fails, so CI blocks the merge.

Usage:
  python verify.py                       # all files under learnings/
  python verify.py --changed a.md b.md   # only these
  python verify.py --no-signature        # skip sig check (unsigned pools only)
"""

from __future__ import annotations

import base64
import json
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath


LEARNINGS_DIR = "learnings"
SCHEMA = "komi.learning/1"


# ── canonicalization + content-addressing (mirror of komi/engine/model.py) ──

def canonical_json(obj) -> bytes:
    def _norm(x):
        if isinstance(x, str):
            return unicodedata.normalize("NFC", x)
        if isinstance(x, dict):
            return {k: _norm(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [_norm(v) for v in x]
        return x
    return json.dumps(_norm(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _content_view(rec: dict) -> dict:
    return {
        "schema": rec.get("schema", SCHEMA),
        "type": rec.get("type", ""),
        "category": rec.get("category", ""),
        "title": (rec.get("title") or "").strip(),
        "body": (rec.get("body") or "").strip(),
        "trigger": (rec.get("trigger") or "").strip(),
        "tags": sorted({t.strip().lower() for t in rec.get("tags", []) if t.strip()}),
    }


def verify_id(rec: dict) -> bool:
    declared = rec.get("id", "")
    if ":" not in declared:
        return False
    algo = declared.split(":", 1)[0]
    canon = canonical_json(_content_view(rec))
    if algo == "blake3":
        try:
            import blake3
            return declared == f"blake3:{blake3.blake3(canon).hexdigest()}"
        except Exception:
            return False
    if algo == "blake2b":
        import hashlib
        return declared == f"blake2b:{hashlib.blake2b(canon, digest_size=32).hexdigest()}"
    return False


def _signing_message(rec: dict, signer_public_key: str = "") -> bytes:
    # MUST mirror komi/pool/contribute.py::_signing_message exactly.
    prov = rec.get("provenance", {})
    root = {
        "id": rec["id"],
        "content": {k: rec.get(k) for k in
                    ("schema", "type", "category", "title", "body", "trigger", "tags")},
        "parent_ids": prov.get("parent_ids", []),
        "origin": prov.get("origin", ""),
        "signer": signer_public_key,
    }
    return canonical_json(root)


def verify_signature(message: bytes, signature_b64: str, public_key_b64: str) -> bool:
    if not signature_b64 or not public_key_b64:
        return False
    try:
        import nacl.signing
        vk = nacl.signing.VerifyKey(base64.b64decode(public_key_b64))
        vk.verify(message, base64.b64decode(signature_b64))
        return True
    except Exception:
        return False


# ── safety scrub (mirror of komi/engine/classify.py detectors) ──────────────

# MUST mirror komi/engine/classify.py exactly. A parity test (tests/test_review_fixes.py)
# fails if these drift from the engine's detectors.
_SECRET = [
    re.compile(r"\b(sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_\-]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxapp-[0-9]+-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bdop_v1_[a-f0-9]{32,}\b"),
    re.compile(r"\bAC[a-f0-9]{32}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s/]+", re.I),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token|bearer|client[_-]?secret)\b\s*[:=]\s*['\"]?\S{6,}"),
]
_PII = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,5}\d{2,4}\b"),
    re.compile(r"\b\d{1,5}\s+[A-Z][a-z]+\s+(St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive)\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
]
_IDENT = [
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+"),
    re.compile(r"/(?:home|Users)/[^/\s]+"),
    re.compile(r"/root/[^/\s]+"),
    re.compile(r"\bhttps?://(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.(?:\d{1,3}\.){1,2}\d{1,3}\b"),
    re.compile(r"\bhttps?://\[[0-9a-fA-F:]+\]"),
    re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{0,4}\b"),
    re.compile(r"(?i)\bhttps?://[a-z0-9-]+\.(?:internal|local|corp|intranet|lan)\b"),
    re.compile(r"(?i)\b[a-z0-9-]+\.onion\b"),
]


def scrub_problems(text: str) -> list[str]:
    out = []
    if any(p.search(text) for p in _SECRET):
        out.append("secret/credential")
    if any(p.search(text) for p in _PII):
        out.append("pii")
    if any(p.search(text) for p in _IDENT):
        out.append("machine-identifier")
    return out


# ── .md parsing + path (mirror of komi/pool/repo_format.py) ──────────────────

def parse_md(text: str):
    start = text.find("```komi")
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
    return obj if isinstance(obj, dict) and "learning" in obj else None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return s or "uncategorized"


def expected_path(env: dict) -> str:
    lng = env["learning"]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", lng["id"].replace(":", "_"))
    return str(PurePosixPath(LEARNINGS_DIR) / _slug(lng.get("category")) / f"{safe}.md")


# ── checks ───────────────────────────────────────────────────────────────

def check_file(path: Path, *, require_signature: bool, repo_root: Path) -> list[str]:
    problems: list[str] = []
    env = parse_md(path.read_text(encoding="utf-8", errors="replace"))
    if env is None:
        return [f"{path}: no valid `komi` envelope block"]
    lng = env.get("learning", {})

    for fld in ("id", "schema", "type", "category", "title", "body"):
        if not lng.get(fld):
            problems.append(f"{path}: missing required field '{fld}'")

    if not verify_id(lng):
        problems.append(f"{path}: id does not match content (tampered or malformed)")

    if require_signature:
        sig = lng.get("provenance", {}).get("signature")
        pk = env.get("signer", {}).get("public_key", "")
        if not verify_signature(_signing_message(lng, pk), sig or "", pk):
            problems.append(f"{path}: signature missing or invalid")

    joined = " \n ".join([lng.get("title", ""), lng.get("body", ""),
                          lng.get("trigger", ""), " ".join(lng.get("tags", []))])
    for r in scrub_problems(joined):
        problems.append(f"{path}: scrub failed ({r})")

    try:
        actual = path.relative_to(repo_root).as_posix()
        if actual != expected_path(env):
            problems.append(f"{path}: wrong path; expected {expected_path(env)}")
    except ValueError:
        pass
    return problems


def main(argv: list[str]) -> int:
    require_sig = "--no-signature" not in argv
    argv = [a for a in argv if a != "--no-signature"]
    repo_root = Path.cwd()

    if argv and argv[0] == "--changed":
        files = [Path(p) for p in argv[1:] if p.endswith(".md")]
    elif argv:
        files = [Path(p) for p in argv if p.endswith(".md")]
    else:
        base = repo_root / LEARNINGS_DIR
        files = sorted(base.rglob("*.md")) if base.exists() else []

    if not files:
        print("komi-pool verify: no learning files to check.")
        return 0

    problems: list[str] = []
    for f in files:
        if f.exists():
            problems.extend(check_file(f, require_signature=require_sig, repo_root=repo_root))

    if problems:
        print(f"komi-pool verify: FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  x {p}")
        return 1
    print(f"komi-pool verify: OK ({len(files)} file(s) checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
