# komi-learn

Continuous memory and self-improvement for coding agents. It learns how you work and recalls it automatically, with no commands. Works with Claude Code and Codex.

[![PyPI](https://img.shields.io/pypi/v/komi-learn)](https://pypi.org/project/komi-learn/)
[![Python](https://img.shields.io/pypi/pyversions/komi-learn)](https://pypi.org/project/komi-learn/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![CI](https://github.com/kurikomi-labs/komi-learn/actions/workflows/ci.yml/badge.svg)](https://github.com/kurikomi-labs/komi-learn/actions/workflows/ci.yml)

It watches a session, distills durable lessons in the background (your style, your stack, fixes that worked), and loads the relevant ones at the start of the next session. No slash commands, nothing to save by hand.

The idea is from [Hermes Agent](https://github.com/nousresearch/hermes-agent); this is my own take, generalized across hosts with an optional shared layer (the community pool, below).

It's early. The core loop is built and CI-tested, but it hasn't been battle-tested across a lot of real sessions yet. Feedback and bug reports are welcome.

## Install

```bash
pip install komi-learn
komi-learn install            # or: komi-learn install --host codex
```

`install` runs a short interactive setup, then recall and background learning start in your next session. If you already use Claude Code you're already logged in. For scripts, `komi-learn install --yes` takes the defaults.

From source:

```bash
git clone https://github.com/kurikomi-labs/komi-learn
cd komi-learn
pip install -e .
```

## Commands

```bash
komi-learn doctor      # check the install and what to fix
komi-learn update      # upgrade komi-learn + the agent's hooks (--check to only look)
komi-learn status      # config + how much it has learned
komi-learn config      # change any setting (menu, or `config set <key> <val>`)
komi-learn sync        # pull the latest community learnings
komi-learn queue       # review/approve/reject what you'd contribute to the pool
komi-learn forget <x>  # erase learnings matching <x> (archive, or --hard to delete)
komi-learn uninstall   # remove the hooks (keeps your data; --purge to wipe)
```

You can change anything after install, e.g. `komi-learn config set recall.semantic false` or leave the pool with `komi-learn config set pool.repo_url ""`.

## How it works

1. Recall: at session start, learnings relevant to the current context are loaded.
2. Distill: after the session, a background pass reads the transcript and extracts durable lessons (corrections, techniques, fixes).
3. Curate: over time it merges overlapping lessons and archives stale ones.
4. Share (optional): general lessons can be contributed to the community pool, but only ones you approve.

It tries not to learn the wrong things. Secrets, machine-specific paths, one-off failures, and "tool X is broken" complaints are filtered out by a deterministic check before the LLM ever sees them.

## Community pool (optional)

A public pool of general agent lessons, stored as a GitHub repo of signed Markdown files (no server). If you opt in, you get lessons other people's agents figured out, and you can contribute your own.

Contributions are scrubbed of anything identifying and never leave your machine without your approval (each one opens a PR you reviewed). Learnings are content-addressed (BLAKE3) and signed (Ed25519); one signed by more distinct GitHub accounts ranks higher when pulled. That account count is Sybil-resistant but not Sybil-proof, so it's an advisory signal, not a hard trust gate. Recalled community items are labelled and treated as untrusted input. Details: [pool-repo-template/CONTRIBUTING.md](pool-repo-template/CONTRIBUTING.md).

## Try it offline

No setup or API key needed:

```bash
python examples/demo_loop.py
```

It runs two sessions: you correct the agent in the first, and the second shows it recalling that with nothing typed.

## Requirements

- Python 3.10+
- Claude Code or Codex (the agent it plugs into)
- A working model for the distill step: your existing Claude Code login, or `komi-learn login`, or an API key via `--api-key`.

`komi-learn install` verifies these with a real model call and stops with fix steps if something's missing. At runtime, if a hook can't reach the model it skips that learning pass rather than interrupting your session.

The engine has no required dependencies. Optional extras add real signing (`pip install komi-learn[crypto]`) and local semantic recall (`[smart]`); without them it falls back to a stdlib hash and keyword search.

To run your own pool, see [pool-repo-template/](pool-repo-template/).

MIT. Issues and PRs welcome.
