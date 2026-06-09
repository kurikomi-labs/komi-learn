"""Thin LLM wrapper for the LoCoMo harness — backed by the local ``claude`` CLI.

The harness needs an LLM for two jobs: GENERATING answers from retrieved context,
and JUDGING answers vs the gold label (the J-score). Both run through the user's
existing Claude.ai OAuth session via ``claude -p`` (headless), so the benchmark
costs no separate API tokens — just normal subscription usage.

Deliberately minimal: one ``complete(prompt) -> str``. A FakeLLM with the same shape
lets the harness unit-test end-to-end with ZERO API/CLI spend.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass
class ClaudeCLI:
    """Headless ``claude -p`` wrapper. Conservative: a failure returns "" rather than
    raising, so one bad call doesn't abort a multi-hour run (the row is scored wrong,
    not lost). ``model`` lets the judge pin a cheaper/faster model than the answerer."""
    model: str = ""                       # "" = CLI default; e.g. "claude-3-5-haiku-latest"
    timeout: int = 120
    calls: int = 0                        # observability: how many CLI calls this run made

    def complete(self, prompt: str) -> str:
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("`claude` CLI not found on PATH — install it or use --fake")
        cmd = [exe, "-p", prompt]
        if self.model:
            cmd += ["--model", self.model]
        self.calls += 1
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
                encoding="utf-8", errors="replace",
            )
            return (out.stdout or "").strip()
        except (subprocess.TimeoutExpired, Exception):
            return ""


@dataclass
class FakeLLM:
    """Deterministic stand-in for tests. ``answer_fn(prompt)`` decides the reply, so a
    test can simulate 'the model answered correctly/incorrectly' without any real call."""
    answer_fn: object = field(default=lambda p: "FAKE")
    calls: int = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self.answer_fn(prompt)
