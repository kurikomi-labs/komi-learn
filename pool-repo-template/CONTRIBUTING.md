# Contributing to komi-pool

Contributions are **automated and human-gated**. You never hand-author files here.

## The flow

1. As you work with an agent running komi-learn, the background distiller spots a
   general, reusable learning (a technique, a pitfall, a fix).
2. The hybrid classifier confirms it's genuinely general and **strips anything
   identifying** — a deterministic floor rejects secrets/PII/paths/names before an
   LLM ever judges it, and re-checks the LLM's generalized rewrite.
3. The learning lands in your **local review queue**. Nothing leaves your machine.
4. **You approve it.** Only then does komi-learn prepare a signed, scrubbed `.md`
   file and open a PR here.
5. CI re-verifies (id, signature, scrub, path, schema). A maintainer reviews the
   human-readable diff and merges.

## What CI checks (`.github/workflows/verify.yml`)

A PR must pass all of:
- the `komi` envelope parses and has required fields;
- the content-addressed **id matches** the content (no tampering);
- the **signature verifies** against the embedded signer key;
- the **safety scrub** finds no secrets/PII/identifiers;
- the file is at the correct content-addressed **path** (`learnings/<category>/<id>.md`).

PRs that fail are not merged. Reviewers additionally reject anything that reads as
project-specific, low-quality, unsafe, or not actually general — even if CI passes.

## Manual review checklist (for maintainers)

- [ ] Is the learning **general** (useful to many, not one project)?
- [ ] Truly **no identifiers** (names, paths, repos, hosts, secrets)?
- [ ] Is it **correct** and not actively harmful advice?
- [ ] Is the category right? Is it a duplicate of an existing learning?

## Categories

`tooling` · `workflow` · `preference` · `domain-knowledge` · `pitfall` ·
`debugging` · `language-behavior` · `formatting-style` · `meta-agent`

(`environment` is intentionally **not** a pool category — setup state is always
kept personal.)

## Corroboration, not conflict

Because file paths are content-addressed, two people who learn the same thing
produce the **same file**. That's not a conflict — it's corroboration. A learning
trusted by many independent signers is weighted higher when agents pull it.
