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
> Claude Code plugin. The **Global Learnings pool is a GitHub repo of `.md` files**
> (no server): contributions go in via human-approved Pull Requests, and the engine
> syncs the repo to a local cache and re-verifies every learning locally. The full
> flow is tested against real git repos; pointing it at a live GitHub repo is one
> config value.

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
   (BLAKE3), signed (Ed25519), and **held in a review queue for your approval**.
   On approval, komi-learn writes the learning as a `.md` file and **opens a Pull
   Request** to the pool repo (`kurikomi-labs/komi-pool`); CI re-verifies it and a
   maintainer merges. The engine periodically **syncs the pool repo** to a local
   cache, re-verifies every learning, and surfaces relevant ones in recall — always
   framed as untrusted community reference.

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
python -m pytest tests/ -q        # 50 passing
```

---

## Requirements

komi-learn refuses to half-install. `komi-learn install` **verifies every
requirement for real** — including an actual model call — and if something's
missing it stops, leaves your settings untouched, and tells you exactly what to
fix. If it says installed, the full loop genuinely works.

| Requirement | Why | How |
|---|---|---|
| Python 3.10+ with komi-learn | the engine | `pip install komi-learn` |
| Claude Code (`claude` CLI) | the host komi-learn plugs into | [claude.com/claude-code](https://claude.com/claude-code) |
| A **working model** (OAuth *or* API key) | distillation reads sessions | `claude auth login` *or* `--api-key sk-ant-...` |
| git *(optional)* | global-pool transport | only if you join the pool |
| pynacl *(optional)* | sign pool contributions | only to contribute to the pool |

The model requirement is checked with a real call, not just "is a key set" — a
login that can't actually reach the model is reported as a failure, not a false OK.

## Install for Claude Code (one command)

If you already use Claude Code, you're **already logged in** — just install:

```bash
pip install komi-learn        # (today: pip install -e . from this repo)
komi-learn install            # Claude Code (default)
komi-learn install --host codex   # OpenAI Codex CLI (second host)
```

> **Works across agents.** The learning engine is host-agnostic — the same model,
> store, distiller, and recall power every host via a thin adapter
> (`komi/adapters/base.py`). Claude Code and OpenAI Codex CLI are supported today
> (`examples/demo_codex_host.py` proves a learning distilled in Codex is recalled
> next session, no Claude Code involved). New hosts implement two methods.

Only if `komi-learn install` reports the model check failing because you're *not*
logged in do you need `claude auth login` (or pass `--api-key sk-ant-...`). Already
on Claude Code? Skip it — your existing subscription login is used automatically.

That single `komi-learn install`:
- **verifies all requirements first** (and stops with exact fixes if any is unmet),
- detects your Python and registers the hooks in `~/.claude/settings.json`
  (backed up first, merged not clobbered, using an absolute interpreter path so
  it can't break on a PATH mismatch),
- writes config and generates your pseudonymous contributor key, and
- **recall + distillation start working in your very next session — no commands.**

**Distillation auth — zero-config when possible.** komi-learn prefers **free OAuth
via your existing Claude.ai login** (the `claude` CLI) — no API key, no per-call
cost. It confirms you're logged in with a cheap, cost-free `claude auth status`
check before using it. If you're not logged in, it falls back to an API key, and
if neither is available, distillation simply stays off (recall still works).

```bash
komi-learn login                                                       # free OAuth distillation (claude auth login)
komi-learn install --pool https://github.com/kurikomi-labs/komi-pool   # join the global pool
komi-learn install --api-key sk-ant-...                                # use an API key instead of OAuth
```

Manage it:

```bash
komi-learn doctor      # diagnose: what's healthy, what's an optional warning, how to fix
komi-learn status      # config + how many learnings you've accrued
komi-learn sync        # pull the latest global learnings now
komi-learn curate      # consolidate the library now (otherwise automatic, ~weekly)
komi-learn uninstall   # remove hooks (keeps your learnings; --purge to wipe)
```

**Reliability model — strict at install, safe at runtime:**
- **Install is a strict gate.** It verifies every requirement (incl. a real model
  call) and fails loudly with exact fixes rather than half-installing. No false
  "it's working." (`--allow-incomplete` overrides if you really want to.)
- **Runtime never breaks your agent.** Once installed, if a hook ever can't reach
  the model mid-session (network blip, etc.) it no-ops silently — a learning pass
  is skipped, your session is never interrupted. Recall, which needs no model,
  keeps working regardless.

Run `komi-learn doctor` anytime — it re-verifies everything (with a real model
call) and tells you precisely what's healthy and what to fix.

Personal learnings live under `~/.claude/komi/`; project learnings under
`<project>/.claude/komi/` (committable, team-shareable). They share one index.

> A Claude Code **plugin** form also ships (`.claude-plugin/plugin.json` +
> `hooks/hooks.json`) for marketplace-style distribution; the `komi-learn install`
> command above is the recommended path today.

**Optional power-ups** (zero code change — the engine detects them):

```bash
pip install komi-learn[smart] # semantic (meaning-based) recall via a local model
pip install blake3 pynacl     # real BLAKE3 ids + Ed25519 pool signatures
pip install anthropic         # real LLM distillation/classification
```

**Semantic recall** (`[smart]`): by default komi-learn finds past learnings by
*meaning*, not just keywords — a lesson about "test suites" surfaces when you're
working on "unit tests". It uses a local embedding model (one `pip install`,
~hundreds of MB, then fully offline, no API key). **Without it, recall falls back
to keyword search automatically** — nothing breaks, it's just less semantic.
`komi-learn doctor` shows which mode is active.

Tune cadence with `KOMI_NUDGE_TURNS` (default 8) and the model with
`KOMI_DISTILL_MODEL`.

### Connect the Global Learnings pool

The pool is a GitHub repo of `.md` files. To enable it:

1. Create a repo (e.g. `kurikomi-labs/komi-pool`) and copy in everything from
   [`pool-repo-template/`](pool-repo-template/) (README, CONTRIBUTING, the CI
   workflow that re-verifies every PR, CODEOWNERS, and the signed seed learnings).
2. Point komi-learn at it, in `~/.claude/komi/config.json`:
   ```json
   { "pool": { "repo_url": "https://github.com/kurikomi-labs/komi-pool", "sync_hours": 12 } }
   ```
   or set `KOMI_POOL_REPO_URL`. Until you do, the engine runs **personal-only** —
   no sync, nothing leaves your device.

The engine then syncs the repo to `~/.claude/komi/pool/repo` on a cadence,
re-verifies every learning locally, and surfaces relevant ones in recall. Approved
contributions open PRs via the GitHub CLI (`gh`); install it for the publish path.

---

## Why it's trustworthy (the Global Learnings safety model)

The public pool is the killer feature and the riskiest part, so it's defense-in-depth
(modeled on the "Portable Agent Memory" protocol):

- **Nothing leaves without you.** Every global candidate sits in a local review
  queue until you approve it.
- **A deterministic floor the LLM can't override.** Secrets/PII/machine paths/repo
  names are rejected before any LLM sees them — and re-checked on the LLM's own
  rewrite, so a "global" with a leaked path is downgraded automatically.
- **Tamper-evident, end to end.** Each learning's id is the BLAKE3 hash of its
  content; editing it breaks the id. The pool repo's CI re-verifies every PR (id +
  signature + a fresh scrub), and the engine re-verifies again on pull — the repo,
  like any remote, is never trusted blindly.
- **Transparent + reviewable.** Because the pool is a git repo of human-readable
  `.md` files, every contribution is a PR diff a maintainer can actually read, and
  the full history is public and auditable.
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
| `komi/pool/` | global pool — identity (Ed25519), contribute, GitHub backend, `.md` format, review queue, CI verifier |
| `komi/adapters/claude_code/` | the zero-friction hooks + LLM backends + config |
| `pool-repo-template/` | drop-in contents for the `komi-pool` GitHub repo (CI, docs, signed seeds) |
| `docs/01-research.md` | deep research: Hermes, Letta, PAM, the Claude Code substrate |
| `docs/02-architecture.md` | the full system design + open decisions |
| `examples/demo_loop.py` | runnable end-to-end demo (incl. pool publish/pull) |
| `tests/` | 38 tests, including the privacy-floor + pool-integrity safety tests |

---

## Roadmap

v1 is the personal loop + the GitHub-backed global pool (tested against local git
repos). Next, in priority order: stand up the real `komi-pool` repo + a
**corroboration-based trust** weighting on pull; additional **host adapters**
(Codex, a chat UI) behind the same two-method interface; the slow **curator**
(umbrella consolidation, archiving); **embedding-based** recall/curation (v1 uses
FTS + heuristics); and a review-queue
**inspection UI**. See `docs/02-architecture.md §9–10`.

MIT licensed.
