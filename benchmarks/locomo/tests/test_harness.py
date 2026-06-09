"""Harness unit tests — run end-to-end on a tiny in-memory fixture with FakeLLM, so the
plumbing is proven (ingest → retrieve → answer → judge → score) with ZERO API/CLI spend."""

import re

import pytest

from benchmarks.locomo.dataset import Conversation, Turn, QA
from benchmarks.locomo.conditions import (
    FullContext, MdPile, KomiRecall, approx_tokens,
)
from benchmarks.locomo.harness import run_condition, judge_answer, ConditionResult
from benchmarks.locomo.llm import FakeLLM


_word = re.compile(r"[a-z0-9]+")


def _fixture():
    turns = [
        Turn("session_1", "D1:1", "Caroline", "I went to the LGBTQ support group on 7 May 2023.", date="2023"),
        Turn("session_1", "D1:2", "Melanie", "Nice! I painted a sunrise last year.", date="2023"),
        Turn("session_2", "D2:1", "Caroline", "I'm thinking of studying psychology.", date="2023"),
        Turn("session_2", "D2:2", "Melanie", "My dog is named Biscuit.", date="2023"),
    ]
    qa = [
        QA("When did Caroline go to the LGBTQ support group?", "7 May 2023", 2, ["D1:1"]),
        QA("What is Melanie's dog named?", "Biscuit", 4, ["D2:2"]),
    ]
    return Conversation(sample_id="fix", turns=turns, qa=qa)


def _oracle_llm():
    """Answers by echoing the best-overlapping line; judges by gold/pred token overlap.
    Good enough to make the fixture's answerable QAs come out CORRECT deterministically."""
    def fn(prompt: str) -> str:
        low = prompt.lower()
        if "verdict:" in low:
            gold = re.search(r"gold answer:\s*(.*)", prompt, re.I)
            pred = re.search(r"predicted answer:\s*(.*)", prompt, re.I)
            gw = set(_word.findall((gold.group(1) if gold else "").lower()))
            pw = set(_word.findall((pred.group(1) if pred else "").lower()))
            return "CORRECT" if (gw & pw) else "WRONG"
        q = re.search(r"question:\s*(.*)", prompt, re.I)
        qw = set(_word.findall((q.group(1) if q else "").lower()))
        best, score = "NO ANSWER", 0
        for line in prompt.splitlines():
            lw = set(_word.findall(line.lower()))
            if len(qw & lw) > score:
                best, score = line, len(qw & lw)
        return best
    return FakeLLM(answer_fn=fn)


def test_approx_tokens_monotonic():
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1
    assert approx_tokens("a" * 400) == 100


@pytest.mark.parametrize("cond_cls", [FullContext, MdPile, KomiRecall])
def test_condition_runs_end_to_end(cond_cls):
    llm = _oracle_llm()
    conv = _fixture()
    res = run_condition(cond_cls(), conv, llm)
    assert isinstance(res, ConditionResult)
    assert res.n == 2                                   # both QAs scored
    assert 0.0 <= res.j_score <= 100.0
    assert res.avg_tokens > 0


def test_full_context_includes_everything():
    fc = FullContext()
    fc.ingest(_fixture())
    ctx, toks = fc.context_for("anything")
    assert "Biscuit" in ctx and "psychology" in ctx     # nothing dropped
    assert toks == approx_tokens(ctx)


def test_mdpile_retrieves_by_keyword():
    md = MdPile(k=1)
    md.ingest(_fixture())
    ctx, _ = md.context_for("What is Melanie's dog named?")
    assert "Biscuit" in ctx                             # keyword overlap found the line


def test_komi_recall_retrieves_relevant_turn():
    kr = KomiRecall(k=2)
    kr.ingest(_fixture())
    ctx, _ = kr.context_for("When did Caroline go to the support group?")
    assert "support group" in ctx.lower()               # komi's ranking surfaced it


def test_judge_shortcuts_save_calls():
    llm = FakeLLM(answer_fn=lambda p: "CORRECT")
    # exact match → no LLM call needed
    assert judge_answer(llm, "q", "Biscuit", "biscuit") is True
    assert llm.calls == 0
    # empty pred for a real gold → WRONG, no call
    assert judge_answer(llm, "q", "Biscuit", "") is False
    assert llm.calls == 0


def test_claude_cli_passes_prompt_on_stdin_not_argv(monkeypatch):
    """Regression: a full-context prompt is ~78k chars; passing it as an argv argument
    blows the Windows command-line limit (WinError 206) and silently returns "" — which
    scored full-context 0% and nearly produced a false 'md-pile beats komi' result. The
    prompt MUST go on stdin. Pin it by asserting the prompt is fed via stdin, not argv."""
    import benchmarks.locomo.llm as llm_mod
    captured = {}

    class _FakeProc:
        stdout = "OK"

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        return _FakeProc()

    monkeypatch.setattr(llm_mod.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(llm_mod.subprocess, "run", fake_run)
    big = "x" * 78000
    out = llm_mod.ClaudeCLI().complete(big)
    assert out == "OK"
    # the giant prompt must be on stdin, and must NOT appear as an argv element
    assert captured["input"] == big
    assert big not in captured["cmd"]


def test_full_context_costs_more_tokens_than_recall():
    """The whole thesis in miniature: selective recall uses fewer context tokens than
    dumping everything. (Accuracy is compared on the real benchmark; here we pin the
    token-cost direction the efficiency metric depends on.)"""
    conv = _fixture()
    fc = FullContext(); fc.ingest(conv)
    kr = KomiRecall(k=1); kr.ingest(conv)
    _, fc_tok = fc.context_for("dog")
    _, kr_tok = kr.context_for("dog")
    assert kr_tok < fc_tok
