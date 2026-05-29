# komi-pool

**The Global Learnings pool for [komi-learn](https://github.com/kurikomi-labs/komi-learn).**

This repo is a community knowledge base of small, general, anonymized learnings
that make AI agents better — techniques, pitfalls, fixes, and ways of working that
hold across people and projects. Each learning is one Markdown file under
[`learnings/`](learnings/), grouped by category.

There is **no server**. The repo *is* the database. komi-learn syncs this repo to
a local cache, re-verifies every learning locally, and recalls the relevant ones
into sessions — always framed as unverified community reference, never as
instructions.

## What belongs here

✅ General, reusable knowledge with **no identifying information**:
- "Read Python tracebacks bottom-up — the root cause is usually the deepest frame."
- "Prefer `rg` over `grep -r`: it's faster and respects `.gitignore`."
- "When a CI test passes locally but fails remotely, check for time-zone–dependent assertions."

❌ Never:
- Secrets, credentials, tokens, private URLs.
- Personal data (names, emails, anything identifying a person).
- Machine/project specifics: home paths, repo/org names, internal hostnames.
- "Tool X is broken" claims, one-off task narratives, or environment-setup gripes.

## How a learning is trusted

Every file carries a verifiable record (a fenced ` ```komi ` block):
- **Content-addressed id** — the BLAKE3 hash of the learning's content. Edit the
  content and the id no longer matches → CI rejects it.
- **Signature** — signed with the contributor's pseudonymous Ed25519 key. You stay
  anonymous; corroboration is counted across distinct signers.
- **CI re-verification** — every PR is checked by [`.github/workflows/verify.yml`](.github/workflows/verify.yml):
  id match, signature, a fresh safety scrub, correct file path, schema.

## Contributing

You don't write these by hand — komi-learn prepares, scrubs, signs, and opens the
PR for you after **you approve** the learning locally. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Learnings are contributed under [CC0-1.0](LICENSE) (public domain) so anyone can
use them freely. By contributing you affirm the content is general, non-identifying,
and yours to share.
