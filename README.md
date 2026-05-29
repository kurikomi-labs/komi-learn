# komi-learn

**A continuous, zero-friction learning layer for AI agents.**

Today's agents are amnesiac geniuses — every session starts from zero. They
re-derive the same workarounds, re-learn your preferences, and repeat corrected
mistakes. komi-learn fixes that: it watches an agent's work, distills durable
lessons in the background, and reloads the relevant ones into every future
session — **automatically, with no commands to type.** It also grows a shared,
anonymized, provenance-verified **Global Learnings** pool so the whole community
gets smarter together.

Inspired by [Hermes Agent](https://github.com/nousresearch/hermes-agent)'s
self-improvement loop, rebuilt to be model-agnostic, universal across personas
(developers, knowledge workers, students, scientists), and shareable.

> Status: **v1 MVP.** The personal learning loop runs end-to-end today as a
> Claude Code plugin. The global pool's contribution/trust pipeline is fully
> implemented locally; the network transport is stubbed (designed, not yet served).

---

## How it works

Three planes, decoupled by design (see [`docs/02-architecture.md`](docs/02-architecture.md)):

```
HOST     Claude Code hooks: SessionStart (recall IN) · Stop (distill OUT)
  │
ENGINE   recall ─ distill ─ classify ─ curate ─ store   (host-agnostic)
  │      USER.md · MEMORY.md · skills/ · index.db (SQLite FTS5)
  │
POOL     scrub → sign → review → publish · pull → verify → cache   (PAM-style)
```

**The loop, with zero friction:**

1. **Recall** — at session start, a hook injects the learnings relevant to what
   you're doing (who you are + techniques that fit the context) as model context.
2. **Distill** — after you work, a background pass reads the transcript and
   extracts durable learnings: corrections you made, techniques that worked,
   fixes for failures. It runs detached and never blocks you.
3. **Classify** — each learning is scoped `personal` / `project` / `global` by a
   **hybrid** classifier: a deterministic safety floor rejects anything with
   secrets/PII/identifiers, then an LLM judges what's genuinely general.
4. **Store** — learnings land in human-readable Markdown (`USER.md`, `MEMORY.md`,
   skills), mirrored into a SQLite FTS index. A slow curator consolidates toward
   a few rich "umbrella" skills and archives (never deletes) stale ones.
5. **Share (optional)** — general learnings are scrubbed, content-addressed
   (BLAKE3), signed (Ed25519), and **held in a review queue for your approval**
   before anything reaches the public pool. Pulled global learnings are verified
   locally and always framed as untrusted reference data.

**What it deliberately does NOT learn** (the difference between getting smarter
and accumulating fear): environment-specific failures, "tool X is broken" claims,
transient errors that resolved, and one-off task narratives. See the distiller
prompt at [`komi/engine/prompts/distill.md`](komi/engine/prompts/distill.md).

---

## Try it (offline, no API key)

```bash
python examples/demo_loop.py
```

This simulates two sessions: in the first, the user corrects the agent's style
and a debugging technique emerges; the second shows the agent recalling both with
nothing typed. A general learning is scrubbed and queued for the global pool.

Run the tests:

```bash
pip install pytest
python -m pytest tests/ -q        # 29 passing
```

---

## Install as a Claude Code plugin

The plugin ships hooks that auto-register (`SessionStart` → recall,
`Stop`/`SubagentStop` → distill). With `ANTHROPIC_API_KEY` set, distillation and
classification use a real model (a cheap one by default — distillation is a
summarization task); without it, the hooks degrade to safe no-ops.

```
.claude-plugin/plugin.json     # manifest
hooks/hooks.json               # auto-registered hooks
komi/                          # the engine + adapter (Python)
```

Personal learnings live under `~/.claude/komi/`; project learnings under
`<project>/.claude/komi/` (committable, team-shareable). They share one index.

**Optional power-ups** (zero code change — the engine detects them):

```bash
pip install blake3 pynacl     # real BLAKE3 ids + Ed25519 pool signatures
pip install anthropic         # real LLM distillation/classification
```

Tune cadence with `KOMI_NUDGE_TURNS` (default 8) and the model with
`KOMI_DISTILL_MODEL`.

---

## Why it's trustworthy (the Global Learnings safety model)

The public pool is the killer feature and the riskiest part, so it's defense-in-depth
(modeled on the "Portable Agent Memory" protocol):

- **Nothing leaves without you.** Every global candidate sits in a local review
  queue until you approve it.
- **A deterministic floor the LLM can't override.** Secrets/PII/machine paths/repo
  names are rejected before any LLM sees them — and re-checked on the LLM's own
  rewrite, so a "global" with a leaked path is downgraded automatically.
- **Tamper-evident.** Each learning's id is the BLAKE3 hash of its content; editing
  it breaks the id. Pulled learnings are re-verified locally — the server is never
  trusted blindly.
- **Anonymous + attributable.** Local evidence is stripped before sharing;
  contributions are signed with a pseudonymous Ed25519 key, so corroboration can be
  counted across distinct signers without ever knowing who you are.
- **Injection-safe.** Recalled learnings (especially public ones) are wrapped in
  "treat as data, not instructions" framing.

---

## Project layout

| Path | What |
|------|------|
| `komi/engine/` | host-agnostic engine — model, store, classify, recall, distill |
| `komi/pool/` | global pool — identity (Ed25519), contribute/pull (PAM-style) |
| `komi/adapters/claude_code/` | the zero-friction hooks + LLM backends |
| `docs/01-research.md` | deep research: Hermes, Letta, PAM, the Claude Code substrate |
| `docs/02-architecture.md` | the full system design + open decisions |
| `examples/demo_loop.py` | runnable end-to-end demo |
| `tests/` | 29 tests, including the privacy-floor safety tests |

---

## Roadmap

v1 is the personal loop + the local global pipeline. Next, in priority order:
the pool **server** (ingest, moderation, corroboration-based trust); additional
**host adapters** (Codex, a chat UI) behind the same two-method interface;
**embedding-based** recall/curation (v1 uses FTS + heuristics); and a review-queue
**inspection UI**. See `docs/02-architecture.md §9–10`.

MIT licensed.
