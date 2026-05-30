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
- **every signature verifies** against its own signer key — a learning may carry a
  `signatures` array of independent endorsers, and *each* must verify (a claimed-but-
  invalid signature is a hard failure), with at least one valid signer;
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
produce the **same file**. That's not a conflict — it's corroboration.

When a second contributor independently distills a lesson that already exists,
komi-learn **appends their signature** to that file's `signatures` array (it opens
a "Corroborate learning: …" PR) rather than duplicating or overwriting it.

**Corroboration counts distinct GitHub accounts, not keys (Sybil-resistant).** A
signing key is free to mint, so counting keys could be gamed by one person making
many keys. Instead each signature binds the contributor's **GitHub username** (inside
the signed message, so it can't be swapped), and CI enforces that every signature a
PR adds belongs to the **PR author** (and that the account clears an age bar). So
"N signers" means **N distinct GitHub accounts that each opened a PR** — a real
measure of independent agreement. Set your username once: `komi-learn config set
pool.github_user <you>` (the install wizard also asks).

A learning endorsed by more distinct accounts is **weighted (a little) higher** when
agents pull it, and consumers can require a minimum — `komi-learn config set
pool.min_corroboration 2` pulls only lessons more than one account signed. (The
default is 1 while the pool is young.) The count is still **clamped to 3** and treated
as advisory — a high count never *admits* a learning a safety filter would exclude.

> Contributing without a username still works (legacy/anonymous mode), but those
> signatures aren't account-verified and don't earn corroboration credit. For the
> strong guarantee, the pool must require `github_user` via CI + branch protection.
