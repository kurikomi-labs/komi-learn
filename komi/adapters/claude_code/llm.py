"""LLM backends for the distiller/judge in the Claude Code adapter.

Two concrete clients implement the engine's ``LLMClient`` / ``ScopeJudge``:
  • AnthropicLLM — calls the Anthropic Messages API directly (cheap model by
    default; distillation is a summarization task). Used when ANTHROPIC_API_KEY
    is set. Includes prompt caching of the long system prompt.
  • NullLLM — returns "[]" / project verdicts; used when no key is available so
    hooks degrade to no-ops instead of erroring.

The real Claude Agent SDK path (forking a background subagent with a restricted
tool set, the closest analogue to Hermes' review fork) plugs in here later behind
the same interface; the direct-API client is the dependency-light default.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ...engine.model import Learning, Scope


_DISTILL_MODEL = os.environ.get("KOMI_DISTILL_MODEL", "claude-haiku-4-5-20251001")


class NullLLM:
    """No-op backend: extract nothing, judge everything project. Keeps hooks safe
    when no API key is configured."""
    def complete(self, *, system: str, user: str) -> str:
        return "[]"

    def __call__(self, learning: Learning, *, context: dict) -> dict:
        return {"scope": Scope.PROJECT.value, "category": learning.category,
                "rationale": "no-llm"}


class AnthropicLLM:
    """Anthropic Messages API client implementing both LLMClient and ScopeJudge."""

    def __init__(self, *, model: str = _DISTILL_MODEL, api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None
        if self.api_key:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except Exception:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def test_call(self) -> tuple[bool, str]:
        """Real one-token call to prove the API key actually works (not just present)."""
        if not self._client:
            return False, "anthropic SDK or API key missing"
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=16,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return (bool(text), "model responded" if text else "empty response")
        except Exception as e:
            return False, f"API call failed: {str(e)[:80]}"

    def complete(self, *, system: str, user: str) -> str:
        if not self._client:
            return "[]"
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=2000,
                # Cache the long, stable distill prompt so repeated passes are cheap
                # (the engine's recurring cost lever, mirroring Hermes' cache reuse).
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        except Exception:
            return "[]"

    def __call__(self, learning: Learning, *, context: dict) -> dict:
        """ScopeJudge: decide project vs global + generalization rewrite."""
        if not self._client:
            return {"scope": Scope.PROJECT.value, "category": learning.category}
        sys = _JUDGE_SYSTEM
        usr = json.dumps({
            "title": learning.title, "body": learning.body,
            "trigger": learning.trigger, "tags": learning.tags,
            "category": learning.category, "cwd": context.get("cwd", ""),
        }, ensure_ascii=False)
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=800,
                system=[{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": usr}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start:end + 1])
        except Exception:
            pass
        return {"scope": Scope.PROJECT.value, "category": learning.category}


_JUDGE_SYSTEM = """You decide the SCOPE of a distilled learning for a shared knowledge system.

Given one learning (JSON), decide whether it is GENERALLY TRUE and useful to anyone
doing this class of work — independent of this specific user, project, or machine —
or whether it is specific to this project's conventions.

Return ONLY a JSON object:
{
  "scope": "global" | "project",
  "visibility": "shareable" | "private",
  "category": "<keep or refine the category>",
  "generalized_title": "<if global: rewrite title to be general, stripping any
      project/user/machine specifics>",
  "generalized_body": "<if global: rewrite body to be general — replace 'in this
      repo' with the generic condition (e.g. 'in a uv-managed monorepo'); remove any
      names, paths, identifiers>",
  "rationale": "<one short clause>"
}

Rules:
- "global" ONLY if the lesson holds for many people and contains NO identifiers,
  names, paths, repo/org names, or anything user/machine-specific. When unsure → "project".
- A learning that depends on this project's structure, naming, or private choices → "project".
- Be conservative: a wrong "global" leaks specifics into a public pool. Default "project".
- "visibility" is SEPARATE from scope and protects against committing/sharing
  CONFIDENTIAL business data. Set "private" if the lesson contains or is about:
  equity / cap tables / shares / valuation, fundraising / investors / term sheets,
  revenue / ARR / MRR / salaries, internal strategy / competitive positioning /
  unreleased roadmap, acquisitions, or anything plainly confidential. Otherwise
  "shareable" (the default — normal engineering/craft knowledge is shareable).
- A "private" learning is NEVER "global" (confidential data must never reach the
  pool). When unsure whether something is sensitive business info → "private"."""


def build_llm(*, prefer: str = "oauth"):
    """Pick a backend for the distiller/judge.

    Default ordering (``prefer="oauth"``): **OAuth via the claude CLI first** —
    it's free (rides the user's Claude.ai subscription) and needs no key — but
    *only when a cheap auth probe confirms it's logged in*. We gate on the probe
    (``claude auth status``, no model call) rather than just the binary's presence,
    so we never select a CLI that will fail at distill time. If OAuth isn't
    available we fall back to an **Anthropic API key** (from env or komi-learn's
    ``~/.claude/komi/.env``), then a safe **no-op** so distillation simply turns
    off — never breaking the session.

    ``prefer="api"`` reverses the first two (API key first) for users who'd rather
    not use subscription OAuth for automated distillation.

    The chosen client implements both ``LLMClient`` and ``ScopeJudge``.
    """
    _load_komi_env()

    def _oauth():
        try:
            from .llm_cli import ClaudeCLILLM
            cli = ClaudeCLILLM()
            if cli.available and cli.probe().ok:
                return cli
        except Exception:
            pass
        return None

    def _api():
        a = AnthropicLLM()
        return a if a.available else None

    order = (_oauth, _api) if prefer == "oauth" else (_api, _oauth)
    for pick in order:
        client = pick()
        if client is not None:
            return client
    return NullLLM()


def _load_komi_env() -> None:
    """Load ANTHROPIC_API_KEY (and any other vars) from ~/.claude/komi/.env into
    the process env if not already set. This is how a hook-spawned distiller gets
    the credential the installer stored, without depending on shell env inheritance."""
    try:
        from . import paths
        env_path = paths.personal_root() / ".env"
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


# ── curator consolidator ────────────────────────────────────────────────────

_CONSOLIDATE_SYSTEM = """You are komi-learn's skill CURATOR. You are given a CLUSTER of
related procedural learnings (skills) that overlap. Merge them into ONE rich,
CLASS-LEVEL "umbrella" skill that covers the whole cluster.

Return ONLY a JSON object:
{
  "title": "<class-level name — covers the whole class, NOT one specific task>",
  "body": "<the merged guidance: combine the techniques, keep every distinct
            pitfall/step, deduplicate. Write it as durable reference data.>",
  "trigger": "<'use when…' — the situation this umbrella applies to>",
  "tags": ["<lowercase>", "<keywords>"],
  "category": "<keep the shared category>",
  "rationale": "<one clause>"
}

Rules:
- The title MUST be at the class level (e.g. "Working with pytest"), never a single
  task/PR/error.
- Preserve substance: do not drop a member's specific fix or pitfall when merging.
- DECLINE generously. Merging is DESTRUCTIVE — the originals are archived — so only
  merge when the members are genuinely facets of ONE skill that a single umbrella
  serves better than separate entries. Return {} (leave them alone) when:
    • the members are UNRELATED, OR
    • they are related/same-domain but are DISTINCT skills a user would want to keep
      separately (e.g. "filter tests by name with pytest -k" vs "stop at first failure
      with pytest -x" — same tool, different techniques: do NOT merge), OR
    • merging would force you to drop or blur a member's specific guidance.
  When in doubt, return {}. A missed merge is cheap; a wrong merge destroys a skill."""


def build_consolidator(llm=None):
    """Return a ConsolidateLLM callable for the curator, backed by ``llm`` (or a
    freshly chosen backend). Returns None when no model is available — the curator
    then reports clusters without merging (safe degradation)."""
    client = llm if llm is not None else build_llm()
    if isinstance(client, NullLLM):
        return None

    def _consolidate(members):
        payload = json.dumps([
            {"title": m.title, "body": m.body, "trigger": m.trigger,
             "tags": m.tags, "category": m.category}
            for m in members
        ], ensure_ascii=False)
        text = client.complete(system=_CONSOLIDATE_SYSTEM, user=payload)
        if not text:
            return None
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e == -1 or e <= s:
            return None
        try:
            obj = json.loads(text[s:e + 1])
            return obj or None
        except json.JSONDecodeError:
            return None

    return _consolidate


__all__ = ["AnthropicLLM", "NullLLM", "build_llm", "build_consolidator"]
