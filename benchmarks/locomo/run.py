"""Run the LoCoMo benchmark across conditions and conversations; write results + report.

  # validate the harness end-to-end with NO API spend:
  python -m benchmarks.locomo.run --fake --convos 1

  # pilot: 2 conversations, all conditions, via the claude CLI (your subscription):
  python -m benchmarks.locomo.run --convos 2

  # full run:
  python -m benchmarks.locomo.run --convos 10

Outputs benchmarks/locomo/results/<timestamp>.json and prints a comparison table.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# UTF-8 stdout so progress glyphs render on Windows cp1252 consoles too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .dataset import load, DEFAULT_PATH
from .conditions import ALL_CONDITIONS
from .harness import run_condition
from .llm import ClaudeCLI, FakeLLM


def _fake_llm():
    """A deterministic stand-in: answers by echoing any context line that shares the most
    words with the question, and judges by substring. Lets the harness run end-to-end with
    zero external calls so we can prove the plumbing before spending."""
    import re
    word = re.compile(r"[a-z0-9]+")

    def answer_fn(prompt: str) -> str:
        # crude: if it's a judge prompt, decide by gold/pred overlap; else echo best line
        low = prompt.lower()
        if "verdict:" in low:
            # CORRECT if predicted shares a token with gold
            gold = re.search(r"gold answer:\s*(.*)", prompt, re.I)
            pred = re.search(r"predicted answer:\s*(.*)", prompt, re.I)
            gw = set(word.findall((gold.group(1) if gold else "").lower()))
            pw = set(word.findall((pred.group(1) if pred else "").lower()))
            return "CORRECT" if (gw & pw) else "WRONG"
        q = re.search(r"question:\s*(.*)", prompt, re.I)
        qw = set(word.findall((q.group(1) if q else "").lower()))
        best, best_score = "NO ANSWER", 0
        for line in prompt.splitlines():
            lw = set(word.findall(line.lower()))
            s = len(qw & lw)
            if s > best_score:
                best, best_score = line, s
        return best[:80]
    return FakeLLM(answer_fn=answer_fn)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LoCoMo benchmark for komi-learn")
    ap.add_argument("--convos", type=int, default=2, help="how many conversations (1-10)")
    ap.add_argument("--qa", type=int, default=None, help="cap QA per conversation (smoke)")
    ap.add_argument("--conditions", default=",".join(ALL_CONDITIONS),
                    help="comma list: " + ",".join(ALL_CONDITIONS))
    ap.add_argument("--fake", action="store_true", help="use FakeLLM (no API/CLI spend)")
    ap.add_argument("--model", default="", help="claude model for answering")
    ap.add_argument("--judge-model", default="", help="claude model for judging")
    ap.add_argument("--data", default=str(DEFAULT_PATH))
    args = ap.parse_args(argv)

    try:
        convos = load(Path(args.data))[: args.convos]
    except FileNotFoundError as e:
        print(e); return 1
    conds = [c.strip() for c in args.conditions.split(",") if c.strip() in ALL_CONDITIONS]
    if not conds:
        print("no valid conditions"); return 1

    if args.fake:
        llm = _fake_llm(); judge = llm
    else:
        llm = ClaudeCLI(model=args.model)
        judge = ClaudeCLI(model=args.judge_model)

    def progress(cond, conv_id, i, total, correct):
        mark = "+" if correct else "."
        print(f"\r  [{cond:13}] {conv_id} {i}/{total} {mark}", end="", flush=True)

    n_qa = sum(len(c.qa[: args.qa] if args.qa else c.qa) for c in convos)
    print(f"LoCoMo: {len(convos)} conversation(s) x {len(conds)} condition(s) "
          f"~ {n_qa * len(conds)} answers" + (" [FAKE]" if args.fake else " [claude CLI]"))

    results = {}
    for cname in conds:
        cond_results = []
        for conv in convos:
            cond = ALL_CONDITIONS[cname](llm)
            r = run_condition(cond, conv, llm, judge_llm=judge,
                              limit_qa=args.qa, progress=progress)
            cond_results.append(r)
            print()
        # merge per-conversation results for this condition
        merged_rows = [row for r in cond_results for row in r.rows]
        from .harness import ConditionResult
        results[cname] = ConditionResult(condition=cname, rows=merged_rows)

    _report(results, args, llm, judge)
    return 0


def _report(results, args, llm, judge) -> None:
    print("\n" + "=" * 72)
    print(f"{'condition':14} {'J-score':>8} {'avg-tok':>9} {'J/1k-tok':>9}   by-category")
    print("-" * 72)
    for name, r in results.items():
        cats = " ".join(f"{k}:{v}" for k, v in r.by_category().items())
        print(f"{name:14} {r.j_score:7.1f}% {r.avg_tokens:9.0f} {r.efficiency:9.2f}   {cats}")
    print("=" * 72)

    out = {
        "n_conversations": args.convos,
        "fake": args.fake,
        "conditions": {
            name: {
                "j_score": round(r.j_score, 2),
                "avg_tokens": round(r.avg_tokens, 1),
                "j_per_1k_tokens": r.efficiency,
                "by_category": r.by_category(),
                "n_qa": r.n,
            } for name, r in results.items()
        },
    }
    if not args.fake:
        out["answer_calls"] = getattr(llm, "calls", None)
        out["judge_calls"] = getattr(judge, "calls", None)
    dest_dir = Path(__file__).parent / "results"
    dest_dir.mkdir(exist_ok=True)
    # no Date.now in scripts here — use a monotonic counter file or argv stamp instead
    stamp = str(int(time.time())) if not args.fake else "fake"
    dest = dest_dir / f"{stamp}.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {dest}")


if __name__ == "__main__":
    raise SystemExit(main())
