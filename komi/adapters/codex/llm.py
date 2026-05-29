"""Model backend for the Codex adapter's distiller/judge.

Codex authenticates via ``OPENAI_API_KEY`` (or a ChatGPT login the ``codex`` CLI
manages). For komi-learn's distillation we mirror the Claude Code adapter's
shape: try an OpenAI API client first (reliable from a detached worker), then fall
back to a safe no-op so distillation simply turns off rather than breaking a
session. Both backends implement the engine's LLMClient + ScopeJudge interface.

The distiller is model-agnostic by design (it just needs text in / JSON out), so
the engine doesn't care that this host talks to GPT instead of Claude — which is
exactly the portability the second-host work is meant to prove.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ...engine.model import Learning, Scope

_DISTILL_MODEL = os.environ.get("KOMI_CODEX_DISTILL_MODEL", "gpt-5-mini")


class NullLLM:
    def complete(self, *, system: str, user: str) -> str:
        return "[]"

    def __call__(self, learning: Learning, *, context: dict) -> dict:
        return {"scope": Scope.PROJECT.value, "category": learning.category,
                "rationale": "no-llm"}


class OpenAILLM:
    """OpenAI Chat Completions client implementing LLMClient + ScopeJudge."""

    def __init__(self, *, model: str = _DISTILL_MODEL, api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or _komi_openai_key() or os.environ.get("OPENAI_API_KEY", "")
        self._client = None
        if self.api_key:
            try:
                import openai
                self._client = openai.OpenAI(api_key=self.api_key)
            except Exception:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def test_call(self) -> tuple[bool, str]:
        if not self._client:
            return False, "openai SDK or API key missing"
        try:
            r = self._client.chat.completions.create(
                model=self.model, max_tokens=16,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            )
            text = (r.choices[0].message.content or "").strip()
            return (bool(text), "model responded" if text else "empty response")
        except Exception as e:
            return False, f"API call failed: {str(e)[:80]}"

    def complete(self, *, system: str, user: str) -> str:
        if not self._client:
            return "[]"
        try:
            r = self._client.chat.completions.create(
                model=self.model, max_tokens=2000,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return r.choices[0].message.content or ""
        except Exception:
            return "[]"

    def __call__(self, learning: Learning, *, context: dict) -> dict:
        if not self._client:
            return {"scope": Scope.PROJECT.value, "category": learning.category}
        usr = json.dumps({
            "title": learning.title, "body": learning.body,
            "trigger": learning.trigger, "tags": learning.tags,
            "category": learning.category, "cwd": context.get("cwd", ""),
        }, ensure_ascii=False)
        try:
            r = self._client.chat.completions.create(
                model=self.model, max_tokens=800,
                messages=[{"role": "system", "content": _JUDGE_SYSTEM},
                          {"role": "user", "content": usr}],
            )
            text = r.choices[0].message.content or ""
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e != -1 and e > s:
                return json.loads(text[s:e + 1])
        except Exception:
            pass
        return {"scope": Scope.PROJECT.value, "category": learning.category}


def _komi_openai_key() -> Optional[str]:
    """Read OPENAI_API_KEY from ~/.codex/komi/.env without polluting os.environ."""
    try:
        from . import paths
        env_path = paths.personal_root() / ".env"
        if not env_path.exists():
            return None
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY=") and len(line) > 16:
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


_JUDGE_SYSTEM = """You decide the SCOPE of a distilled learning for a shared knowledge system.
Given one learning (JSON), decide whether it is GENERALLY TRUE and useful to anyone
doing this class of work — independent of this user/project/machine — or specific to
this project. Return ONLY a JSON object:
{"scope":"global"|"project","category":"<keep or refine>","generalized_title":"<if global>","generalized_body":"<if global, no identifiers>","rationale":"<one clause>"}
Rules: "global" ONLY if it holds for many people and has NO identifiers/names/paths.
When unsure → "project". A wrong "global" leaks specifics into a public pool."""


def build_llm():
    a = OpenAILLM()
    return a if a.available else NullLLM()


__all__ = ["OpenAILLM", "NullLLM", "build_llm"]
