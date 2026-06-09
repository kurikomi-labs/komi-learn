# LoCoMo benchmark for komi-learn

Evidence that komi-learn's **selective recall** beats a **flat pile of markdown** on
long, multi-session memory — measured on [LoCoMo](https://github.com/snap-research/locomo)
(Maharana et al., 2024), the benchmark the memory-systems field cites (Mem0, Zep, etc.).

This is a **research/eval tool**. It is *not* part of the shipped `komi-learn` package
(`benchmarks/` is excluded from the wheel).

## Why this exists

A reviewer's challenge: *"it does not provide evidence that it actually works and that it
is better than a structured collection of md files."* This harness answers that head-on.

## What it measures

For each **condition** (memory backend), we ingest a LoCoMo conversation, then for every
QA pair we build a context, ask an LLM to answer, and **LLM-judge** the answer vs the gold
(the standard "J-score"). We report J-score **and** average context tokens — the
accuracy-per-token axis is where selective memory is supposed to win.

| condition | what it is |
|---|---|
| `full-context` | dump the **entire** conversation into the prompt. Accuracy upper bound, worst tokens. |
| `md-pile` | the conversation as flat markdown, retrieved by keyword overlap (top-k). **The "structured collection of md files" baseline komi-learn must beat.** |
| `komi-recall` | every turn stored; komi-learn's **ranked recall** (semantic + keyword — the real engine) picks the top-k. Tests the *mechanism*. |
| `komi-distill` | the **real** komi-learn pipeline — the coding-tuned distiller runs, then recall. Included to quantify the coding↔conversational domain mismatch. |

The headline metric is **J-score per 1000 context tokens** (`j_per_1k_tokens`).

## How to run

```bash
# 1. fetch the dataset (10 conversations, ~5882 turns, ~1986 QA pairs)
python -m benchmarks.locomo.dataset --fetch

# 2. validate the harness end-to-end with ZERO API/CLI spend
python -m benchmarks.locomo.run --fake --convos 1

# 3. smoke test on real LLM (claude CLI / your subscription) — tiny
python -m benchmarks.locomo.run --convos 1 --qa 30

# 4. pilot: 2 conversations, all conditions
python -m benchmarks.locomo.run --convos 2

# 5. full run
python -m benchmarks.locomo.run --convos 10
```

The LLM is the local `claude` CLI (your Claude.ai OAuth session) — no separate API key,
no per-token billing, just normal subscription usage. Override the answer/judge model with
`--model` / `--judge-model`. Results are written to `benchmarks/locomo/results/<ts>.json`.

## Honesty notes (read before citing a number)

- **Domain mismatch is real.** LoCoMo tests *conversational/social* recall ("what gift
  did X give Y?"). komi-learn is built for *coding* memory. `komi-recall` isolates the
  retrieval mechanism (fair); `komi-distill` will score lower **by design** because the
  distiller is tuned to discard social facts — that gap is a finding, not a bug.
- **J-score is LLM-judged**, so it carries the judge's noise. We shortcut exact/empty
  matches without a call to cut cost and variance.
- **Tokens are approximate** (~4 chars/token). The point is the *ratio* between
  conditions, not absolute counts.
- The `md-pile` baseline is the one that matters most for the reviewer's question. If
  komi-recall doesn't beat it on accuracy-per-token, that is the honest answer.
