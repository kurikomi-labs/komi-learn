# komi-learn

**Your AI agent, minus the amnesia.** komi-learn watches how you work, quietly learns your preferences and the techniques that pan out, and reloads the relevant ones into every new session — automatically, with no commands to type.

Works with **Claude Code** and **OpenAI Codex**. One command to set up; then it just runs.

---

## What you get

- 🧠 **Remembers you** — your style, your stack, your conventions, across every session.
- 🔁 **Learns in the background** — distills durable lessons from your work after the fact; never blocks you.
- ⚡ **Zero friction** — no slash commands, no "save this." It recalls what's relevant when a session starts.
- 🔒 **Private by default** — everything stays on your machine. Nothing is shared unless you say so.
- 🌍 **Optional community pool** — opt in to get useful, anonymized tips other agents have learned (and share your own, only after you approve each one).
- 🔌 **Host-agnostic** — same brain for Claude Code or Codex; a learning from one is recalled in the next session.

---

## Quick start

> Not on PyPI yet (coming soon). For now, install from the repo:

```bash
git clone https://github.com/kurikomi-labs/komi-learn
cd komi-learn
pip install -e .

komi-learn install      # interactive setup — for Codex: komi-learn install --host codex
```

`komi-learn install` runs a short wizard: it explains each feature in one sentence, asks simple yes/no questions, and sets everything up for you. That's it — recall and background learning start in your **very next session**.

Already on Claude Code? You're already logged in — nothing else to do. (Scripting it? `komi-learn install --yes` takes the recommended defaults.)

---

## Everyday commands

```bash
komi-learn doctor      # is everything healthy? what to fix
komi-learn status      # your settings + how much it's learned
komi-learn config      # change any setting, anytime (menu)
komi-learn sync        # pull the latest community learnings now
komi-learn uninstall   # remove it (keeps your learnings; --purge to wipe)
```

Change your mind later — you're never locked into install-time choices:

```bash
komi-learn config set recall.semantic false        # turn off meaning-based recall
komi-learn config set pool.repo_url ""             # leave the community pool
komi-learn config show
```

---

## How it works

```
recall (session start) ──▶ your agent works ──▶ distill (background) ──▶ remembered next time
```

1. **Recall** — when a session starts, the learnings relevant to what you're doing are loaded as context.
2. **Distill** — after you work, a background pass extracts durable lessons (corrections, techniques, fixes) from the transcript.
3. **Curate** — over time it consolidates overlapping lessons and retires stale ones, so memory stays sharp, not bloated.
4. **Share** *(optional)* — general, anonymized lessons can be contributed to the community pool — but only ones **you approve**.

It deliberately *doesn't* learn the wrong things — secrets, machine-specific paths, one-off failures, or "tool X is broken" gripes are filtered out. Full design: [`docs/02-architecture.md`](docs/02-architecture.md).

---

## The community pool *(optional)*

A shared, public pool of general agent lessons — a GitHub repo of signed `.md` files, no server. Opt in during setup to:

- **Get** useful, anonymized techniques other people's agents have figured out.
- **Give back** your own general lessons — scrubbed of anything identifying, and **never shared without your explicit approval** (each contribution opens a Pull Request you reviewed).

No personal data ever leaves your machine. Recalled community tips are clearly labelled and treated as untrusted reference. Details + safety model: [`docs/02-architecture.md`](docs/02-architecture.md), [`pool-repo-template/CONTRIBUTING.md`](pool-repo-template/CONTRIBUTING.md).

---

## Want to see it first?

No setup, no API key — run the offline demo:

```bash
python examples/demo_loop.py
```

Two sessions: you correct the agent's style and a debugging trick emerges in the first; the second shows the agent recalling both with nothing typed.

---

## Requirements

| Need | Why | How |
|---|---|---|
| Python 3.10+ | the engine | `pip install -e .` |
| Claude Code **or** Codex | the agent it plugs into | [claude.com/claude-code](https://claude.com/claude-code) · [Codex CLI](https://github.com/openai/codex) |
| A working model | reads sessions to learn | already logged in on Claude Code, or `komi-learn login`, or `--api-key sk-ant-…` |

`komi-learn install` **verifies all of this for real** (including an actual model call) and stops with exact fix steps if anything's missing — no silent half-install. If a hook ever can't reach the model mid-session, it quietly skips that learning pass; **your agent is never interrupted.**

---

## Docs

| | |
|---|---|
| [`docs/02-architecture.md`](docs/02-architecture.md) | how the whole system is designed |
| [`docs/03-roadmap.md`](docs/03-roadmap.md) | what's built and what's next |
| [`docs/05-adr-log.md`](docs/05-adr-log.md) | the key decisions and their trade-offs |
| [`pool-repo-template/`](pool-repo-template/) | drop-in contents to run your own pool |

Inspired by [Hermes Agent](https://github.com/nousresearch/hermes-agent)'s self-improvement loop — rebuilt to be model-agnostic, universal, and shareable.

MIT licensed.
