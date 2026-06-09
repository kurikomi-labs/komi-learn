# Coding-memory benchmark — design notes

**Status: design only (not built). Pending the fixed LoCoMo smoke result.**

## Why a second benchmark

LoCoMo tests *conversational* recall ("what gift did X give Y three sessions ago?").
komi-learn is a *coding-agent* memory. The mechanism overlaps (persist across sessions,
retrieve later) but the **value claim** does not: komi-learn's claim is *"recalling a
lesson from a past coding session makes the agent do the right thing in a future one"* —
not "recall a social fact." A benchmark that matches the claim is the honest evidence.

This is NOT abandoning LoCoMo — LoCoMo stays as the externally-recognized number. This
ADDS the eval that actually tests komi-learn's purpose.

## The core difference from LoCoMo

| | LoCoMo | coding-memory eval |
|---|---|---|
| unit | a QA pair | a **two-session task pair** |
| session 1 | conversation turns | a coding session that surfaces a reusable LESSON (a user correction, a fix, a convention) |
| session 2 | (none) | a NEW task in the same project where that lesson SHOULD change the agent's action |
| score | did it recall the fact (J-score) | did the lesson change the OUTPUT correctly (behavioral) |

The test: run session-1, let komi distil. Start session-2 fresh; the only difference
between arms is whether session-1's lesson is in context. Measure whether session-2's
output obeys the lesson.

## Worked example (what one item looks like)

- **Session 1**: user says *"stop using `Color(0xFF...)` literals — all colors go through
  `LocalStatusColors`."* (a correction → komi distils a `formatting-style` learning).
- **Session 2 (fresh)**: *"add an error state to the badge component."* A naive agent writes
  `Color(0xFFD32F2F)`. An agent that recalled the lesson uses `LocalStatusColors.error`.
- **Score**: does session-2's diff contain a raw `Color(0xFF...)` literal? (0/1, objective —
  a regex/AST check, no LLM judge needed for the clear-cut cases.)

## Conditions (reuse the LoCoMo harness shape)

- `no-memory` : session-2 with zero context from session-1 (the floor).
- `md-pile` : session-1's raw transcript as flat md, keyword-retrieved (the reviewer's baseline).
- `komi` : the real komi-learn distil→recall (the product).
- (optional) `full-context`: all of session-1 in the prompt (ceiling, but unrealistic).

Metric: **lesson-adherence rate** (% of session-2 outputs that obey the session-1 lesson),
plus context tokens. komi's claim is it hits high adherence at low tokens.

## The hard part: the dataset

There is no off-the-shelf set of (lesson session → dependent task) pairs. Options, cheapest
to most credible:

1. **Hand-authored seed set (~15-25 pairs)** drawn from REAL komi learnings — the field
   data already gives genuine examples (the Compose token rule, the release-workflow race,
   the i18n plural rule, the ripgrep preference). Each becomes a session-1 lesson + a
   session-2 task with an objective check. Small, but real and honest. **Recommended start.**
2. **Synthetic generation**: prompt an LLM to generate lesson/task pairs across categories.
   Scales, but risks circularity (the same model judges adherence) and unrealistic lessons.
3. **Mine real multi-session transcripts** (e.g. the user's own Claude Code history) for
   "user corrected X in session A, did the agent repeat the mistake in session B?" — the
   most authentic, but laborious and privacy-sensitive (must stay local).

## Objective scoring (avoid the LLM-judge noise where possible)

Prefer **deterministic checks** per item: a regex/AST assertion on the session-2 output
("no raw color literal", "used `--workspace`", "synchronized the version bump"). Fall back
to LLM-judge only for items where adherence is genuinely fuzzy. This sidesteps the
judge-variance that muddies LoCoMo.

## Honesty guardrails (same discipline as LoCoMo)

- The `md-pile` baseline is the bar. If komi doesn't beat it on adherence-per-token, say so.
- Hand-authored items risk being written to favour komi — author the session-2 tasks BEFORE
  deciding the check, and have the check be objective so authoring bias can't tilt the score.
- Report n; ~20 items is directional, not definitive — label it as such.

## Reuse plan

`benchmarks/coding_memory/` mirrors `benchmarks/locomo/`: `dataset.py` (the seed pairs),
`conditions.py` (no-memory / md-pile / komi), `harness.py` (run session-1 distil → session-2
generate → objective check), `run.py`. The LLM wrapper (`claude` CLI, stdin) is shared.
