"""komi-learn pool — pseudonymous contributor identity (Ed25519).

A contributor signs every published learning with an Ed25519 key so the pool can
attribute corroboration to *distinct, stable* signers without ever learning who
the human is. The key is generated locally and stored under the komi root; the
public key is the only thing that travels. PyNaCl is preferred; if it's absent we
fall back to a clearly-labelled unsigned mode so the MVP still runs (the pool
server would reject unsigned entries — that's the point of the label).

The public key is the only thing that travels.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional


def _have_nacl() -> bool:
    try:
        import nacl.signing  # noqa: F401
        return True
    except Exception:
        return False


class Contributor:
    """Holds the local signing key. ``sign`` returns (algorithm, signature_b64)."""

    def __init__(self, key_dir: str | Path):
        self.key_dir = Path(key_dir).expanduser()
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = self.key_dir / "contributor.key.json"
        self._signing = None
        self._verify_b64 = ""
        self._algo = "unsigned"
        self._load_or_create()

    def _load_or_create(self) -> None:
        if not _have_nacl():
            # Unsigned fallback: derive a stable pseudonymous id from a random seed
            # so corroboration counting still works locally, but mark it unsigned.
            if self.key_path.exists():
                data = json.loads(self.key_path.read_text(encoding="utf-8"))
                self._verify_b64 = data.get("public", "")
            else:
                import os
                seed = base64.b64encode(os.urandom(32)).decode()
                self._verify_b64 = seed
                self.key_path.write_text(
                    json.dumps({"algo": "unsigned", "public": seed}, indent=2),
                    encoding="utf-8",
                )
            self._algo = "unsigned"
            return

        import os
        import nacl.signing
        if self.key_path.exists():
            # Fail CLOSED on insecure permissions: a world/group-readable private
            # key means anyone on the box can forge your signed contributions.
            _require_owner_only(self.key_path)
            data = json.loads(self.key_path.read_text(encoding="utf-8"))
            self._signing = nacl.signing.SigningKey(base64.b64decode(data["private"]))
        else:
            self._signing = nacl.signing.SigningKey.generate()
            data = {
                "algo": "ed25519",
                "private": base64.b64encode(bytes(self._signing)).decode(),
                "public": base64.b64encode(bytes(self._signing.verify_key)).decode(),
            }
            # Atomic write with 0600 set on the temp file BEFORE it's in place, so
            # the private key is never briefly world-readable (umask race).
            _atomic_write_private(self.key_path, json.dumps(data, indent=2))
        self._verify_b64 = base64.b64encode(bytes(self._signing.verify_key)).decode()
        self._algo = "ed25519"

    @property
    def public_key(self) -> str:
        return self._verify_b64

    @property
    def algo(self) -> str:
        return self._algo

    def sign(self, message: bytes) -> str:
        if self._algo == "ed25519" and self._signing is not None:
            sig = self._signing.sign(message).signature
            return base64.b64encode(sig).decode()
        return ""  # unsigned mode


def _require_owner_only(path) -> None:
    """Raise if the key file is readable by group/other (POSIX). On Windows the
    permission bits don't map the same way, so we skip the check there (NTFS ACLs
    aren't represented in st_mode) — but on any POSIX host an insecure key aborts."""
    import os
    import stat
    if os.name == "nt":
        return
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(
            f"Contributor key {path} is accessible by group/other. "
            f"Fix it: chmod 600 {path}"
        )


def _atomic_write_private(path, text: str) -> None:
    """Write a private key file atomically with 0600 set before it's in place."""
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".key-")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verify_signature(message: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Pool-side / on-pull verification. Returns False in unsigned mode (no sig to
    check) — callers decide whether to accept unsigned entries (default: no)."""
    if not signature_b64 or not public_key_b64:
        return False
    if not _have_nacl():
        return False
    try:
        import nacl.signing
        import nacl.exceptions
        vk = nacl.signing.VerifyKey(base64.b64decode(public_key_b64))
        vk.verify(message, base64.b64decode(signature_b64))
        return True
    except Exception:
        return False


__all__ = ["Contributor", "verify_signature"]
