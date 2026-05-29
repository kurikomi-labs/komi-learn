"""komi-learn — install-time REQUIREMENTS, verified for real.

Philosophy (decided 2026-05-29): no hacks, no silent degradation at install. We
state the prerequisites plainly, check each one — including a REAL model call, not
just "is a key present" — and if a required one is unmet, ``komi-learn install``
fails loudly with the exact fix. If install reports success, the full loop works.

(Runtime is a separate matter: a hook must never crash the user's live session, so
the distiller still no-ops gracefully at runtime. This module is the install gate.)

Each check returns a :class:`Requirement` result with: name, ok, required flag,
detail, and a copy-pasteable ``fix``.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from . import paths


@dataclass
class Requirement:
    name: str
    ok: bool
    required: bool
    detail: str = ""
    fix: str = ""


# ── individual checks ──────────────────────────────────────────────────────

def check_python() -> Requirement:
    try:
        import komi  # noqa: F401
        return Requirement("python", True, True,
                           f"komi-learn importable ({sys.executable})")
    except Exception as e:
        return Requirement("python", False, True, str(e),
                           "pip install komi-learn   (or: pip install -e . from the repo)")


def check_claude_cli() -> Requirement:
    """The claude CLI is required as the host (its hooks run komi-learn). Even
    API-key users need Claude Code present — it's the agent we plug into."""
    if shutil.which("claude"):
        return Requirement("claude-cli", True, True, "Claude Code CLI found")
    return Requirement("claude-cli", False, True, "claude CLI not on PATH",
                       "Install Claude Code: https://claude.com/claude-code")


def verify_model(*, api_key: Optional[str] = None) -> Requirement:
    """REQUIRED: prove a working model path with a real call (OAuth or API key).

    Order matches build_llm: try OAuth (claude CLI) first, then API key. We do an
    actual tiny completion — a valid login that can't reach the model still FAILS
    here, which is the whole point (it caught exactly that gap in testing)."""
    logged_in = False
    # 1. OAuth via the claude CLI
    try:
        from .llm_cli import ClaudeCLILLM
        cli = ClaudeCLILLM()
        if cli.available and cli.probe().ok:
            logged_in = True
            ok, detail = cli.test_call()
            if ok:
                return Requirement("model", True, True,
                                   f"{cli.probe().summary()} — verified ({detail})")
            # logged in but the call failed — report honestly, keep trying API key
            oauth_detail = f"OAuth login present but model call failed: {detail}"
        else:
            oauth_detail = "not logged in to the claude CLI"
    except Exception as e:
        oauth_detail = f"OAuth check error: {e}"

    # 2. API key (explicit, env, or stored)
    try:
        from .llm import AnthropicLLM, _load_komi_env
        _load_komi_env()
        a = AnthropicLLM(api_key=api_key) if api_key else AnthropicLLM()
        if a.available:
            ok, detail = a.test_call()
            if ok:
                return Requirement("model", True, True, f"Anthropic API key — verified ({detail})")
            api_detail = f"API key present but call failed: {detail}"
        else:
            api_detail = "no API key / anthropic SDK"
    except Exception as e:
        api_detail = f"API check error: {e}"

    # Context-aware fix: don't tell an already-logged-in user to log in.
    if logged_in:
        fix = (
            "You ARE logged in, but the model call didn't return. This usually means\n"
            "        the call is blocked in the current context (e.g. a sandbox/CI shell),\n"
            "        not a login problem. Try again from your normal terminal, or use a\n"
            "        key:  komi-learn install --api-key sk-ant-...   then re-run install."
        )
        detail_msg = f"logged in, but model call failed ({oauth_detail}; {api_detail})"
    else:
        fix = (
            "Choose ONE, then re-run  komi-learn install :\n"
            "        • free OAuth (uses your Claude.ai subscription):  claude auth login\n"
            "        • API key:  komi-learn install --api-key sk-ant-..."
        )
        detail_msg = f"no working model path ({oauth_detail}; {api_detail})"

    return Requirement("model", False, True, detail_msg, fix)


def check_git() -> Requirement:
    """Required only when joining the global pool (git is the transport)."""
    if shutil.which("git"):
        return Requirement("git", True, False, "git found")
    return Requirement("git", False, False, "git not on PATH (needed only for the global pool)",
                       "Install git to use the global pool: https://git-scm.com")


def check_signing() -> Requirement:
    """Required only to CONTRIBUTE to the pool (Ed25519 signing). Reading the pool
    works unsigned; publishing needs pynacl."""
    try:
        import nacl.signing  # noqa: F401
        return Requirement("signing", True, False, "Ed25519 signing available (pynacl)")
    except Exception:
        return Requirement("signing", False, False,
                           "pynacl not installed (needed only to CONTRIBUTE to the pool)",
                           "pip install pynacl")


# ── the gate ───────────────────────────────────────────────────────────────

def collect(*, api_key: Optional[str] = None, pool: bool = False) -> list[Requirement]:
    """Run all checks. ``pool=True`` promotes git/signing to relevant (still listed
    either way, but their required-ness is the pool's concern, not the core loop)."""
    reqs = [
        check_python(),
        check_claude_cli(),
        verify_model(api_key=api_key),
        check_git(),
        check_signing(),
    ]
    return reqs


def unmet_required(reqs: list[Requirement]) -> list[Requirement]:
    return [r for r in reqs if r.required and not r.ok]


__all__ = ["Requirement", "collect", "verify_model", "unmet_required",
           "check_python", "check_claude_cli", "check_git", "check_signing"]
