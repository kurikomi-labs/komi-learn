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
  3. EVERY embedded signature verifies against its own signer key, and at least
     one distinct signer is valid (corroboration ≥ 1). A learning may carry a
     ``signatures`` array (multiple independent endorsers) or the legacy single
     ``signer`` shape — both are accepted.
  4. safety scrub finds no secrets / PII / machine identifiers
  5. file lives at the correct content-addressed path

Exit non-zero if any file fails, so CI blocks the merge. A claimed-but-invalid
signature is a failure: the pool must never carry a signature that doesn't verify.

Usage:
  python verify.py                       # all files under learnings/
  python verify.py --changed a.md b.md   # only these
  python verify.py --no-signature        # skip sig check (unsigned pools only)
"""

from __future__ import annotations

import base64
import json
import os
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


def _signing_message(rec: dict, signer_public_key: str = "", signer_github_user: str = "") -> bytes:
    # MUST mirror komi/pool/contribute.py::_signing_message exactly, INCLUDING the
    # back-compat rule: github_user is added to the root ONLY when non-empty, so a
    # pre-Phase-7 signature (no github_user) still verifies byte-identically.
    prov = rec.get("provenance", {})
    root = {
        "id": rec.get("id", ""),   # .get not subscript: a missing id must fail verify, not crash
        "content": {k: rec.get(k) for k in
                    ("schema", "type", "category", "title", "body", "trigger", "tags")},
        "parent_ids": prov.get("parent_ids", []),
        "origin": prov.get("origin", ""),
        "signer": signer_public_key,
    }
    if signer_github_user:
        root["github_user"] = signer_github_user
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


# ── corroboration (mirror of komi/pool/corroboration.py) ────────────────────
# MUST mirror that module: how distinct endorsers are extracted + counted, AND the
# two safety bounds (array cap + counted-signer clamp). Keys are free to mint, so a
# distinct-key count is Sybil-forgeable — the clamp keeps a flood from inflating it.
MAX_SIGNATURES = 64        # mirror corroboration.MAX_SIGNATURES (anti-DoS array cap)
MAX_COUNTED_SIGNERS = 3    # mirror corroboration.MAX_COUNTED_SIGNERS (anti-Sybil clamp)


def envelope_signatures(envelope: dict) -> list:
    """Normalize to [{algo, public_key, signature, github_user}], handling both the
    new ``signatures`` array and the legacy single-``signer`` shape. De-dupes by key.
    Bounded to MAX_SIGNATURES entries (anti-flood; mirrors the engine)."""
    out, seen = [], set()
    raw = envelope.get("signatures")
    if isinstance(raw, list) and raw:
        for s in raw[:MAX_SIGNATURES]:
            if not isinstance(s, dict):
                continue
            pk = s.get("public_key") or ""
            if not pk or pk in seen:
                continue
            seen.add(pk)
            out.append({"algo": s.get("algo", "unsigned"), "public_key": pk,
                        "signature": s.get("signature") or "",
                        "github_user": (s.get("github_user") or "").strip().lstrip("@")})
        return out
    signer = envelope.get("signer", {}) or {}
    pk = signer.get("public_key") or ""
    sig = (envelope.get("learning", {}).get("provenance", {}) or {}).get("signature") or ""
    if pk:
        out.append({"algo": signer.get("algo", "unsigned"), "public_key": pk,
                    "signature": sig, "github_user": ""})
    return out


def _identity(sig: dict) -> str:
    """De-dup key for a signature: GitHub account if bound, else public key
    (mirror of komi/pool/corroboration._identity — Sybil resistance by account)."""
    gh = (sig.get("github_user") or "").strip().lstrip("@").lower()
    return f"gh:{gh}" if gh else f"pk:{sig.get('public_key', '')}"


def assert_append_only(old_env: dict, new_env: dict) -> list:
    """Corroboration may only GROW. Given the BASE version of a modified learning
    file and the NEW version, the new signer set must be a SUPERSET of the old —
    otherwise the PR is dropping/replacing prior endorsers (a corroboration
    downgrade or signer-replacement), which CI must reject. (CI verifies each file
    in isolation otherwise, so without this a hostile PR could strip signers and
    still pass.) Compared by identity (account when bound, else key). Returns a list
    of problems (empty = OK)."""
    old = {_identity(s) for s in envelope_signatures(old_env)}
    new = {_identity(s) for s in envelope_signatures(new_env)}
    removed = old - new
    if removed:
        return [f"signers removed (corroboration may only grow): {sorted(removed)}"]
    return []


def signature_problems(envelope: dict) -> tuple:
    """Return (counted_valid_signers, [problems]). EVERY claimed signature must
    verify — a present-but-invalid signature is a hard failure (the pool must never
    carry a bogus signature), even if other signers are valid. So unlike the engine's
    lenient consumer path, CI keeps checking ALL inspected signatures and reports each
    invalid one. The returned *count* is clamped to MAX_COUNTED_SIGNERS to stay in
    parity with the engine's count_corroboration."""
    learning = envelope.get("learning", {})
    sigs = envelope_signatures(envelope)
    if not sigs:
        return 0, ["no signature present"]
    counted, problems = set(), []
    for s in sigs:
        pk, sig, gh = s["public_key"], s["signature"], s.get("github_user", "")
        if not sig:
            problems.append(f"signer {pk[:12]}… has no signature")
            continue
        if verify_signature(_signing_message(learning, pk, gh), sig, pk):
            counted.add(_identity(s))   # distinct by account when bound, else key
        else:
            problems.append(f"signature for signer {pk[:12]}… is invalid")
    if not counted and not problems:
        problems.append("no valid signature")
    return min(len(counted), MAX_COUNTED_SIGNERS), problems


# ── safety scrub (mirror of komi/engine/classify.py detectors) ──────────────

# MUST mirror komi/engine/classify.py exactly. A parity test (tests/test_review_fixes.py)
# fails if these drift from the engine's detectors.
_SECRET = [
    re.compile(r"\b(sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,120}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,80}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{10,400}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,120}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_\-]{20,120}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,120}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,120}\b"),
    re.compile(r"\bxapp-[0-9]+-[A-Za-z0-9-]{10,120}\b"),
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,80}\.[A-Za-z0-9_\-]{16,80}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,120}\b"),
    re.compile(r"\bdop_v1_[a-f0-9]{32,120}\b"),
    re.compile(r"\bAC[a-f0-9]{32}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,120}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,400}\.[A-Za-z0-9_-]{8,400}\.[A-Za-z0-9_-]{8,400}\b"),
    re.compile(r"\b[a-z][a-z0-9+.\-]{0,20}://[^\s:/@]{1,100}:[^\s:/@]{1,100}@[^\s]{1,200}", re.I),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token|bearer|client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"]{6,120}"),
]
_PII = [
    re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,100}\.[A-Za-z]{2,10}\b"),
    re.compile(r"\b(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,5}\d{2,4}\b"),
    re.compile(r"\b\d{1,5}\s+[A-Z][a-z]{1,20}\s+(St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive)\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]{0,2}){13,16}\b"),
]
_IDENT = [
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]{1,200}"),
    re.compile(r"/(?:home|Users)/[^/\s]{1,200}"),
    re.compile(r"/root/[^/\s]{1,200}"),
    re.compile(r"\bhttps?://(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.(?:\d{1,3}\.){1,2}\d{1,3}\b"),
    re.compile(r"\bhttps?://\[[0-9a-fA-F:]{1,100}\]"),
    re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{0,4}\b"),
    re.compile(r"(?i)\bhttps?://[a-z0-9-]{1,100}\.(?:internal|local|corp|intranet|lan)\b"),
    re.compile(r"(?i)\b[a-z0-9-]{1,100}\.onion\b"),
]


def scrub_problems(text: str) -> list[str]:
    if text and len(text) > 20000:
        text = text[:20000]
    out = []
    if any(p.search(text) for p in _SECRET):
        out.append("secret/credential")
    if any(p.search(text) for p in _PII):
        out.append("pii")
    if any(p.search(text) for p in _IDENT):
        out.append("machine-identifier")
    return out


# ── .md parsing + path (mirror of komi/pool/repo_format.py) ──────────────────

MAX_BLOCK_CHARS = 64 * 1024   # mirror repo_format.MAX_BLOCK_CHARS (anti-DoS)


def parse_md(text: str):
    start = text.find("```komi")
    if start == -1:
        return None
    start = text.find("\n", start) + 1
    end = text.find("```", start)
    if end == -1:
        return None
    if end - start > MAX_BLOCK_CHARS:
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
        valid, sig_probs = signature_problems(env)
        for sp in sig_probs:
            problems.append(f"{path}: {sp}")
        if valid < 1:
            problems.append(f"{path}: no valid signature (corroboration 0)")

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


def _append_only_mode(argv: list[str]) -> int:
    """`--append-only BASE.md NEW.md [BASE2.md NEW2.md ...]`: for each (base, new)
    pair, assert the new file's signer set is a superset of the base's. A base path
    of '-' or a missing file means the learning is newly ADDED (no prior signers to
    preserve) → always OK."""
    pairs = argv[1:]
    if len(pairs) % 2 != 0:
        print("komi-pool verify: --append-only needs BASE NEW pairs")
        return 2
    problems = []
    for i in range(0, len(pairs), 2):
        base_p, new_p = pairs[i], pairs[i + 1]
        new_env = parse_md(Path(new_p).read_text(encoding="utf-8", errors="replace"))
        if new_env is None:
            problems.append(f"{new_p}: unparseable")
            continue
        if base_p == "-" or not Path(base_p).exists():
            continue  # newly added file — nothing to preserve
        old_env = parse_md(Path(base_p).read_text(encoding="utf-8", errors="replace"))
        if old_env is None:
            continue  # base wasn't a valid learning (e.g. brand new path)
        for p in assert_append_only(old_env, new_env):
            problems.append(f"{new_p}: {p}")
    if problems:
        print(f"komi-pool verify (append-only): FAILED ({len(problems)}):")
        for p in problems:
            print(f"  x {p}")
        return 1
    print("komi-pool verify (append-only): OK")
    return 0


# ── signer↔account binding (Phase 7: Sybil resistance) ──────────────────────
# A contributor key is free to mint, so corroboration counts distinct *accounts*
# (github_user), and CI enforces that each NEWLY ADDED signature's github_user is
# the PR author who added it — so you can't sign as someone else, and a fresh
# account is the real cost of a fake endorsement. An optional account-age/activity
# bar (via the gh API) raises that cost further.

MIN_ACCOUNT_AGE_DAYS = int(os.environ.get("KOMI_MIN_ACCOUNT_AGE_DAYS", "30"))


def newly_added_identities(old_env, new_env) -> list:
    """github_users present on the NEW file but not the base (the signers this PR
    is adding). Empty base → all signers are new."""
    old = {s.get("github_user", "") for s in envelope_signatures(old_env)} if old_env else set()
    return [s.get("github_user", "") for s in envelope_signatures(new_env)
            if s.get("github_user", "") and s.get("github_user", "") not in old]


def check_author_binding(new_env, pr_author: str, old_env=None) -> list:
    """Every signature this PR ADDS must be bound to the PR author's GitHub account.
    Pure function (no network) — the testable core of the identity gate. Returns
    problems (empty = OK)."""
    author = (pr_author or "").strip().lstrip("@").lower()
    problems = []
    added = newly_added_identities(old_env, new_env)
    if not added:
        # PR adds no account-bound signature. Allowed (e.g. a legacy unbound
        # contribution), but such signatures don't earn account-verified corroboration.
        return problems
    for gh in added:
        if gh.lower() != author:
            problems.append(
                f"signature added under github_user '{gh}' but PR author is "
                f"'{author or '(unknown)'}' — you may only add YOUR OWN signature")
    return problems


def _account_ok(username: str) -> tuple:
    """Best-effort account-age/activity check via the gh API. Returns (ok, note).
    Graceful: if gh/network is unavailable we DON'T fail the PR on this (the
    author-binding check above is the hard gate); we just note it couldn't run."""
    import subprocess
    import datetime as _dt
    try:
        r = subprocess.run(["gh", "api", f"users/{username}", "--jq", ".created_at"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return True, f"account-age check skipped ({(r.stderr or '').strip()[:60]})"
        created = (r.stdout or "").strip()
        # created_at like 2019-07-01T00:00:00Z
        t = _dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        age_days = (_dt.datetime.now(_dt.timezone.utc) - t).days
        if age_days < MIN_ACCOUNT_AGE_DAYS:
            return False, f"account '{username}' is {age_days}d old (< {MIN_ACCOUNT_AGE_DAYS}d)"
        return True, f"account '{username}' age {age_days}d OK"
    except FileNotFoundError:
        return True, "account-age check skipped (gh not available)"
    except Exception as e:
        return True, f"account-age check skipped ({type(e).__name__})"


def _identity_mode(argv: list[str]) -> int:
    """`--identity <pr_author> BASE NEW [BASE2 NEW2 ...]`: enforce that every
    signature each changed file ADDS is bound to the PR author's account, and that
    the account clears the age bar. Author-binding is a HARD failure; the age check
    is best-effort (skips cleanly without gh)."""
    if len(argv) < 4:
        print("komi-pool verify: --identity needs <pr_author> then BASE NEW pairs")
        return 2
    author = argv[1]
    pairs = argv[2:]
    if len(pairs) % 2 != 0:
        print("komi-pool verify: --identity needs BASE NEW pairs after the author")
        return 2
    problems, notes = [], []
    for i in range(0, len(pairs), 2):
        base_p, new_p = pairs[i], pairs[i + 1]
        new_env = parse_md(Path(new_p).read_text(encoding="utf-8", errors="replace"))
        if new_env is None:
            problems.append(f"{new_p}: unparseable")
            continue
        old_env = None
        if base_p != "-" and Path(base_p).exists():
            old_env = parse_md(Path(base_p).read_text(encoding="utf-8", errors="replace"))
        for p in check_author_binding(new_env, author, old_env):
            problems.append(f"{new_p}: {p}")
        for gh in newly_added_identities(old_env, new_env):
            ok, note = _account_ok(gh)
            notes.append(f"{new_p}: {note}")
            if not ok:
                problems.append(f"{new_p}: {note}")
    for n in notes:
        print(f"  · {n}")
    if problems:
        print(f"komi-pool verify (identity): FAILED ({len(problems)}):")
        for p in problems:
            print(f"  x {p}")
        return 1
    print("komi-pool verify (identity): OK")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--append-only":
        return _append_only_mode(argv)
    if argv and argv[0] == "--identity":
        return _identity_mode(argv)

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
