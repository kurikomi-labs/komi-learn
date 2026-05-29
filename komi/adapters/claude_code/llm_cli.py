"""komi-learn — distiller/judge backed by the local ``claude`` CLI (OAuth session).

This lets the background distiller use the user's existing Claude.ai subscription
auth instead of a separate ANTHROPIC_API_KEY, by shelling out to the ``claude``
CLI in headless mode (``claude -p``). It implements the same ``LLMClient`` /
``ScopeJudge`` interface as the API-backed client.

Robustness is the priority: a detached hook may not always have a working auth
context, so every failure (CLI missing, auth error, timeout, junk output) returns
an empty / conservative result rather than raising. The loop then degrades to
"recall works, distill no-ops this turn" — never a crash, never a blocked session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from ...engine.model import Learning, Scope


# Marker substrings that mean "auth/access failed" rather than "model said this".
_AUTH_FAIL_MARKERS = (
    "does not have access",
    "Please login",
    "Invalid API key",
    "authentication",
    "Unauthorized",
)


@dataclass
class AuthProbe:
    """Result of the cheap ``claude auth status`` health check (no model call)."""
    ok: bool                  # True = logged in; OAuth distillation expected to work
    reason: str               # logged-in | not-logged-in | claude-cli-not-found | auth-status-failed
    method: str = ""          # e.g. "claude.ai"
    subscription: str = ""    # e.g. "max", "pro"
    email: str = ""

    def summary(self) -> str:
        if self.ok:
            sub = f" ({self.subscription})" if self.subscription else ""
            return f"OAuth via {self.method or 'claude CLI'}{sub}"
        return {
            "not-logged-in": "claude CLI present but not logged in",
            "claude-cli-not-found": "claude CLI not installed",
            "auth-status-failed": "claude CLI auth check failed",
        }.get(self.reason, self.reason)


class ClaudeCLILLM:
    """LLMClient + ScopeJudge backed by ``claude -p``.

    Parameters
    ----------
    model : alias like "haiku"/"sonnet" or a full model id. Distillation is a
        summarization task, so a small model is the sensible default.
    timeout : per-call seconds; a stuck CLI must never hang a hook.
    """

    def __init__(self, *, model: str = "haiku", timeout: int = 90,
                 claude_bin: Optional[str] = None):
        self.model = model
        self.timeout = timeout
        self.claude_bin = claude_bin or shutil.which("claude") or "claude"
        self._healthy = shutil.which(self.claude_bin) is not None or os.path.exists(self.claude_bin)
        self._probe_cache: Optional[AuthProbe] = None

    @property
    def available(self) -> bool:
        """True if the CLI binary exists. Use :meth:`probe` to confirm OAuth works."""
        return self._healthy

    def probe(self, *, force: bool = False) -> "AuthProbe":
        """Cheap, cost-free auth health check via ``claude auth status --json``.

        This makes NO model call (no tokens, no rate-limit hit) — it just asks the
        CLI whether it's logged in. Result is cached per-process so the hook only
        probes once. Returns an :class:`AuthProbe` describing whether distillation
        via OAuth is expected to work and why.
        """
        if self._probe_cache is not None and not force:
            return self._probe_cache
        if not self._healthy:
            self._probe_cache = AuthProbe(ok=False, reason="claude-cli-not-found",
                                          method="", subscription="")
            return self._probe_cache
        try:
            proc = subprocess.run(
                [self.claude_bin, "auth", "status", "--json"],
                capture_output=True, text=True, timeout=8, env=_clean_env(),
            )
            data = json.loads((proc.stdout or "{}").strip() or "{}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
                json.JSONDecodeError):
            self._probe_cache = AuthProbe(ok=False, reason="auth-status-failed",
                                          method="", subscription="")
            return self._probe_cache
        logged_in = bool(data.get("loggedIn"))
        self._probe_cache = AuthProbe(
            ok=logged_in,
            reason="logged-in" if logged_in else "not-logged-in",
            method=data.get("authMethod", ""),
            subscription=data.get("subscriptionType", ""),
            email=data.get("email", ""),
        )
        return self._probe_cache

    def test_call(self) -> tuple[bool, str]:
        """Make a REAL tiny model call to prove distillation actually works.

        This is stronger than :meth:`probe`: ``probe`` only asks "are you logged
        in?", but a login can be valid while the model call still fails (e.g. an
        org/context restriction). The install gate needs the truth, so we send a
        one-token prompt and require a non-empty reply. Returns (ok, detail)."""
        if not self._healthy:
            return False, "claude CLI not found"
        out = self._run(system="Reply with exactly: OK", user="ping", max_tokens_hint=16)
        if out and "ok" in out.lower():
            return True, "model responded"
        if out:
            return True, "model responded"  # any text means the call worked
        return False, "claude -p returned nothing (login valid but model call blocked?)"

    def _run(self, *, system: str, user: str, max_tokens_hint: int = 2000) -> str:
        """One headless call. Returns model text, or "" on any failure."""
        if not self._healthy:
            return ""
        cmd = [
            self.claude_bin, "-p",
            "--output-format", "text",
            "--model", self.model,
            "--no-session-persistence",
            "--append-system-prompt", system,
        ]
        try:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True,
                timeout=self.timeout,
                # Keep the distiller from inheriting hook-specific env that could
                # change behavior; pass through auth-relevant vars only.
                env=_clean_env(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 and not out:
            return ""
        # Detect auth/access failures that the CLI prints as normal stdout.
        if any(m.lower() in out.lower() for m in _AUTH_FAIL_MARKERS) and len(out) < 300:
            return ""
        return out

    # LLMClient ----------------------------------------------------------------

    def complete(self, *, system: str, user: str) -> str:
        return self._run(system=system, user=user)

    # ScopeJudge ---------------------------------------------------------------

    def __call__(self, learning: Learning, *, context: dict) -> dict:
        usr = json.dumps({
            "title": learning.title, "body": learning.body,
            "trigger": learning.trigger, "tags": learning.tags,
            "category": learning.category, "cwd": context.get("cwd", ""),
        }, ensure_ascii=False)
        text = self._run(system=_JUDGE_SYSTEM, user=usr, max_tokens_hint=800)
        if not text:
            return {"scope": Scope.PROJECT.value, "category": learning.category,
                    "rationale": "judge-unavailable"}
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return {"scope": Scope.PROJECT.value, "category": learning.category,
                "rationale": "judge-unparseable"}


def _clean_env() -> dict:
    """Environment for the nested ``claude`` CLI subprocess.

    Strategy: copy the FULL environment, then DROP only the specific Claude-Code
    runtime vars that would make the nested CLI think it's running inside a hook
    (which changes its behavior). An earlier version used an allowlist and
    accidentally stripped Windows credential-locating vars (USERNAME, USERDOMAIN,
    ALLUSERSPROFILE, …), which broke OAuth credential resolution — ``auth status``
    reported not-logged-in purely because of the missing env. Keeping the full env
    minus the known-bad keys preserves auth while still de-nesting the CLI.
    """
    drop_exact = {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_TMPDIR", "CLAUDE_CODE_DISABLE_CRON",
        "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
        "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
        "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
        "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH", "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_EFFORT",
    }
    return {k: v for k, v in os.environ.items() if k not in drop_exact}


_JUDGE_SYSTEM = """You decide the SCOPE of a distilled learning for a shared knowledge system.

Given one learning (JSON), decide whether it is GENERALLY TRUE and useful to anyone
doing this class of work — independent of this specific user, project, or machine —
or whether it is specific to this project's conventions.

Return ONLY a JSON object:
{
  "scope": "global" | "project",
  "category": "<keep or refine the category>",
  "generalized_title": "<if global: rewrite title to be general, stripping any project/user/machine specifics>",
  "generalized_body": "<if global: rewrite body to be general; remove any names, paths, identifiers>",
  "rationale": "<one short clause>"
}

Rules:
- "global" ONLY if the lesson holds for many people and contains NO identifiers,
  names, paths, repo/org names, or anything user/machine-specific. When unsure → "project".
- Be conservative: a wrong "global" leaks specifics into a public pool. Default "project"."""


__all__ = ["ClaudeCLILLM", "AuthProbe"]
