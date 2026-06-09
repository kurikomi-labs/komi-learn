"""The LoCoMo eval loop: for each (condition, conversation), ingest → for each QA,
build context → ask the LLM to answer → LLM-judge the answer vs gold (J-score).

Metrics reported per condition: J-score (overall + per category), avg context tokens
(the cost axis), and judge/answer call counts. The headline plot is J-score vs tokens —
the comparison that answers 'is komi-learn better than a flat pile of md files?'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import mean

from .dataset import CATEGORY_NAMES


_ANSWER_PROMPT = """You are answering a question about a long conversation between two people.
Use ONLY the conversation context below. If the context does not contain the answer, reply exactly: NO ANSWER.
Answer in as few words as possible — a name, a date, a short phrase. Do not explain.

CONTEXT:
{context}

QUESTION: {question}
ANSWER:"""

_JUDGE_PROMPT = """You are grading an answer to a question against the gold answer.
Reply with exactly one word: CORRECT or WRONG.
Grade CORRECT if the predicted answer conveys the same fact as the gold answer (allow
paraphrase, different date formats, extra words). Grade WRONG otherwise.

QUESTION: {question}
GOLD ANSWER: {gold}
PREDICTED ANSWER: {pred}

VERDICT:"""


@dataclass
class QAResult:
    question: str
    gold: str
    pred: str
    category: int
    correct: bool
    ctx_tokens: int


@dataclass
class ConditionResult:
    condition: str
    rows: list = field(default_factory=list)        # list[QAResult]

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def j_score(self) -> float:
        return (100.0 * sum(r.correct for r in self.rows) / self.n) if self.n else 0.0

    @property
    def avg_tokens(self) -> float:
        return mean([r.ctx_tokens for r in self.rows]) if self.rows else 0.0

    def by_category(self) -> dict:
        out = {}
        for code, name in CATEGORY_NAMES.items():
            rows = [r for r in self.rows if r.category == code]
            if rows:
                out[name] = round(100.0 * sum(x.correct for x in rows) / len(rows), 1)
        return out

    @property
    def efficiency(self) -> float:
        """J-score per 1000 context tokens — the accuracy-per-cost number that decides
        whether selective recall beats dump-everything."""
        return round(self.j_score / (self.avg_tokens / 1000.0), 2) if self.avg_tokens else 0.0


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def judge_answer(llm, question: str, gold: str, pred: str) -> bool:
    """LLM-as-judge (J-score). Cheap deterministic shortcuts first to save calls:
    empty/NO-ANSWER preds for a real gold are WRONG; exact normalized match is CORRECT."""
    p = _norm(pred)
    g = _norm(gold)
    if not p or p in ("no answer", "no answer."):
        return not g                       # only 'correct' if gold is also empty (rare)
    if g and (p == g or g in p):
        return True
    verdict = llm.complete(_JUDGE_PROMPT.format(question=question, gold=gold, pred=pred))
    return "correct" in _norm(verdict)


def answer_question(llm, context: str, question: str) -> str:
    return llm.complete(_ANSWER_PROMPT.format(context=context, question=question)).strip()


def run_condition(condition, conv, llm, *, judge_llm=None, limit_qa=None,
                  progress=None) -> ConditionResult:
    """Ingest the conversation into ``condition``, then answer + judge each QA."""
    judge_llm = judge_llm or llm
    condition.ingest(conv)
    res = ConditionResult(condition=condition.name)
    qas = conv.qa[:limit_qa] if limit_qa else conv.qa
    for i, qa in enumerate(qas):
        ctx, toks = condition.context_for(qa.question)
        pred = answer_question(llm, ctx, qa.question)
        correct = judge_answer(judge_llm, qa.question, qa.answer, pred)
        res.rows.append(QAResult(
            question=qa.question, gold=qa.answer, pred=pred,
            category=qa.category, correct=correct, ctx_tokens=toks,
        ))
        if progress:
            progress(condition.name, conv.sample_id, i + 1, len(qas), correct)
    return res
