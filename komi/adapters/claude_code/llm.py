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
- Be conservative: a wrong "global" leaks specifics into a public pool. Default "project"."""


def build_llm() -> "AnthropicLLM | NullLLM":
    a = AnthropicLLM()
    return a if a.available else NullLLM()


__all__ = ["AnthropicLLM", "NullLLM", "build_llm"]
