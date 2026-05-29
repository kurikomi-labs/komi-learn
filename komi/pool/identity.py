"""komi-learn pool — pseudonymous contributor identity (Ed25519).

A contributor signs every published learning with an Ed25519 key so the pool can
attribute corroboration to *distinct, stable* signers without ever learning who
the human is. The key is generated locally and stored under the komi root; the
public key is the only thing that travels. PyNaCl is preferred; if it's absent we
fall back to a clearly-labelled unsigned mode so the MVP still runs (the pool
server would reject unsigned entries — that's the point of the label).

See docs/02-architecture.md §7.1 step 5.
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

        import nacl.signing
        if self.key_path.exists():
            data = json.loads(self.key_path.read_text(encoding="utf-8"))
            self._signing = nacl.signing.SigningKey(base64.b64decode(data["private"]))
        else:
            self._signing = nacl.signing.SigningKey.generate()
            data = {
                "algo": "ed25519",
                "private": base64.b64encode(bytes(self._signing)).decode(),
                "public": base64.b64encode(bytes(self._signing.verify_key)).decode(),
            }
            self.key_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                import os
                os.chmod(self.key_path, 0o600)
            except Exception:
                pass
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
