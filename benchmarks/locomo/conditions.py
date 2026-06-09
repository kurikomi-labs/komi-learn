"""The memory backends under test. Each takes a Conversation, ingests it, and exposes
``context_for(question) -> (context_str, n_tokens_in_context)``. The harness then asks
the LLM to answer using only that context, and judges the answer.

Conditions (all scored on J-score AND tokens — the comparison the feedback demands):

  full-context  : dump the ENTIRE transcript into context. Upper bound on accuracy,
                  worst on tokens. The "just put it all in the prompt" baseline.
  md-pile       : the conversation as a flat pile of markdown lines, retrieved by simple
                  keyword overlap (top-k). The "structured collection of md files" the
                  feedback names — what komi-learn must beat.
  komi-recall   : every turn stored as a komi-learn learning; komi's RANKED RECALL
                  (semantic + keyword, the real engine) selects the top-k. Tests the
                  MECHANISM independent of the coding-tuned distiller.
  komi-distill  : the REAL komi-learn pipeline — the coding-tuned distiller runs over the
                  conversation, then recall. Shows concretely how much the domain mismatch
                  costs (the distiller is built to drop social facts LoCoMo asks about).

A token is approximated as ~4 chars (good enough for a relative tokens-per-condition
comparison; the point is the RATIO between conditions, not absolute counts).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tempfile

from komi.engine.store import Store
from komi.engine.recall import _candidate_hits
from komi.engine.model import Learning, LearningType, Category, Scope


def approx_tokens(text: str) -> int:
    return max(0, (len(text) + 3) // 4)


# ── baseline 1: full context ────────────────────────────────────────────────

@dataclass
class FullContext:
    name = "full-context"

    def ingest(self, conv) -> None:
        self._transcript = conv.transcript()

    def context_for(self, question: str):
        return self._transcript, approx_tokens(self._transcript)


# ── baseline 2: md-pile (flat markdown, keyword top-k) ──────────────────────

_WORD = re.compile(r"[a-z0-9]+")


def _keywords(text: str) -> set:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


@dataclass
class MdPile:
    """The conversation as a flat list of markdown lines, retrieved by keyword overlap.
    This is the honest 'pile of .md files' baseline — no embeddings, no ranking model,
    just 'find the lines that share words with the question'."""
    k: int = 12
    name = "md-pile"

    def ingest(self, conv) -> None:
        self._lines = [t.as_line() for t in conv.turns]
        self._kw = [_keywords(l) for l in self._lines]

    def context_for(self, question: str):
        q = _keywords(question)
        scored = sorted(
            range(len(self._lines)),
            key=lambda i: len(q & self._kw[i]),
            reverse=True,
        )
        top = sorted(scored[: self.k])           # restore chronological order
        ctx = "\n".join(self._lines[i] for i in top)
        return ctx, approx_tokens(ctx)


# ── komi-recall: every turn stored, komi's ranked recall retrieves ──────────

def _turn_to_learning(t) -> Learning:
    """Store a raw turn as a semantic learning so komi's index can rank it. We bypass the
    distiller here ON PURPOSE — this isolates the RETRIEVAL mechanism from the (coding-
    tuned) extraction step."""
    body = t.text
    return Learning(
        type=LearningType.SEMANTIC.value,
        category=Category.DOMAIN_KNOWLEDGE.value,
        title=(t.text[:60] or t.dia_id),
        body=f"{t.speaker} ({t.date}): {t.text}" if t.date else f"{t.speaker}: {t.text}",
        trigger=t.text,
        tags=sorted(_keywords(t.text))[:8],
        scope=Scope.PROJECT.value,
        confidence=0.6,
    ).finalize()


@dataclass
class KomiRecall:
    k: int = 12
    name = "komi-recall"

    def ingest(self, conv) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="locomo_komi_"))
        self._store = Store(self._dir)
        stored = 0
        for t in conv.turns:
            self._store.upsert(_turn_to_learning(t))   # let errors surface — a silently
            stored += 1                                # empty store would void the result
        # Guard: a benchmark whose memory backend ingested nothing would score ~0 and look
        # like a komi-learn failure when it's really a harness bug. Refuse to proceed.
        if stored == 0 and conv.turns:
            raise RuntimeError("komi-recall ingested 0 turns — harness bug, not a result")

    def context_for(self, question: str):
        # use komi's real ranking engine (semantic-first, keyword fallback) with the
        # QUESTION as the query — this is the exact recall machinery the product ships.
        hits = _candidate_hits(self._store, question, limit=self.k, scopes=None)
        lines = []
        for row, _sim in hits[: self.k]:
            lines.append(f"- {row['body']}")
        ctx = "\n".join(lines)
        return ctx, approx_tokens(ctx)


# ── komi-distill: the REAL pipeline (coding distiller + recall) ─────────────

@dataclass
class KomiDistill:
    """The full shipping pipeline: run komi-learn's actual distiller over the
    conversation (session by session), then recall. Expected to underperform on LoCoMo
    BY DESIGN — the distiller extracts reusable *coding lessons*, not social facts — and
    quantifying that gap is the point of including it."""
    k: int = 12
    llm: object = None             # a benchmarks.locomo.llm.* with .complete(prompt)
    name = "komi-distill"

    def ingest(self, conv) -> None:
        from komi.engine.distill import distill
        self._dir = Path(tempfile.mkdtemp(prefix="locomo_distill_"))
        self._store = Store(self._dir)
        # feed the conversation to the real distiller in session-sized chunks, mirroring
        # how komi distils a live session. The distiller's LLM is the same claude CLI.
        from collections import defaultdict
        by_session = defaultdict(list)
        for t in conv.turns:
            by_session[t.session].append(t)
        for sk, turns in by_session.items():
            transcript = [{"role": "user", "text": t.text} for t in turns]
            try:
                distill(transcript, personal_store=self._store,
                        llm=_DistillLLMAdapter(self.llm), session_id=sk,
                        cwd=str(self._dir))
            except Exception:
                pass

    def context_for(self, question: str):
        hits = _candidate_hits(self._store, question, limit=self.k, scopes=None)
        lines = [f"- {row['body']}" for row, _ in hits[: self.k]]
        ctx = "\n".join(lines)
        return ctx, approx_tokens(ctx)


class _DistillLLMAdapter:
    """komi's distiller expects an object with ``complete(*, system, user) -> str``;
    our harness LLM has ``complete(prompt) -> str``. Bridge the two."""
    def __init__(self, harness_llm):
        self._llm = harness_llm

    def complete(self, *, system: str, user: str) -> str:
        if self._llm is None:
            return "[]"
        return self._llm.complete(f"{system}\n\n{user}")


ALL_CONDITIONS = {
    "full-context": lambda llm: FullContext(),
    "md-pile": lambda llm: MdPile(),
    "komi-recall": lambda llm: KomiRecall(),
    "komi-distill": lambda llm: KomiDistill(llm=llm),
}
