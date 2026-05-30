# komi-learn — Architecture Decision Log

Lightweight ADRs: the significant decisions and **why** (not just what), plus the
trade-off each one accepts. Written after the Software Architect review flagged
these as load-bearing-but-undocumented. Newest decisions can be appended.

Format per entry: **Decision · Context · Trade-off accepted · Reversibility.**

---

## ADR-1 — Engine injects the LLM via a Protocol, not a concrete client
**Decision.** `LLMClient` / `ScopeJudge` are `Protocol` types; the engine never
imports the Anthropic SDK. Adapters/tests pass a concrete client.
**Context.** The engine must run host-agnostic and with **zero required deps**
(stdlib-only fallback), and be testable with a deterministic mock.
**Trade-off.** Gain: testability, no vendor coupling, offline core. Give up:
a tiny bit of indirection; no compile-time guarantee the client is "complete."
**Reversibility.** Easy — concrete clients already satisfy the Protocol.

## ADR-2 — Markdown is the source of truth; SQLite FTS is a derived cache
**Decision.** Learnings persist as human-readable Markdown (`USER.md`,
`MEMORY.md`, `skills/<id>/SKILL.md`); `index.db` is rebuilt from them.
**Context.** Two consumers with opposite needs — humans (readable, hand-editable,
survives the plugin being disabled) and the engine (fast recall, clustering).
**Trade-off.** Gain: both audiences served; the index is disposable/rebuildable.
Give up: must keep two stores in sync (mitigated — every `upsert` writes both,
`reindex` rebuilds from Markdown and *preserves* DB-only telemetry); Markdown
upsert is O(n) per write (fine <~1000 learnings; revisit at scale).
**Reversibility.** Medium — the `.md` format is now load-bearing (see ADR-5).

## ADR-3 — Distillation runs detached, never blocking the session
**Decision.** The `Stop` hook spawns the distiller as a detached process and
returns immediately.
**Context.** A session must never wait on (or be broken by) a background learning
pass; and the live prompt cache must not be disturbed.
**Trade-off.** Gain: zero added latency, failure isolation, prefix-cache intact.
Give up: a distiller crash is *silent* to the user (mitigated by graceful no-op
on missing model; a failure log is a known follow-up). 
**Reversibility.** Easy — cadence/threshold are config.

## ADR-4 — Mandatory human gate before any pool contribution
**Decision.** Global-candidate learnings sit in a local review queue; nothing is
pushed to the public pool without explicit approval.
**Context.** The pool is shared, public, and signed — irreversibly. One bad/leaky
learning harms everyone and is costly to retract.
**Trade-off.** Gain: trust, user agency, no accidental leaks. Give up: friction /
slower pool growth. (A future opt-in auto-publish for high-confidence,
fully-scrubbed `meta-agent` learnings is possible but deliberately not built.)
**Reversibility.** Easy to relax later; hard to claw back over-shared data — so we
start strict.

## ADR-5 — Content-addressing (BLAKE3) + the `.md` envelope format
**Decision.** A learning's id is `BLAKE3(canonical_json(content))`; the pool
stores one `.md` per learning at `learnings/<category>/<id>.md` with a verifiable
` ```komi ` JSON block.
**Context.** Need dedup (same lesson → same id → corroboration, not duplication),
tamper-evidence, and a portable, reviewable, server-less pool.
**Trade-off.** Gain: free dedup, tamper-evidence, git-as-database, PR-reviewable
diffs. Give up: the format is now **load-bearing** for the live pool — changing it
needs a migration + dual-read. blake2b fallback exists for no-blake3 hosts but a
blake3 id can't be verified without blake3 (consumers need the `crypto` extra).
**Reversibility.** **Hard.** Treat the format + signing scheme as a stable API.
(We already paid one migration cost when the signing message changed — see git log.)

## ADR-6 — The pool CI verifier is a vendored copy, not an import
**Decision.** `pool-repo-template/.github/scripts/verify.py` re-implements
canonicalization / id / signature / scrub instead of importing `komi`.
**Context.** The pool repo must verify itself with no dependency on the code
package (decoupled repos; the pool's CI installs only `blake3`+`pynacl`, never
`komi`). Keeps the two repos independently releasable.
**Trade-off.** Gain: pool independence, CI works standalone. Give up: duplication
that can drift. **Mitigation:** a parity test (`tests/test_review_fixes.py`)
asserts the vendored detectors/canonicalization/id/signing match the engine —
it has already caught one real drift.
**Reversibility.** Easy — could publish a tiny verifier package later and import it.

## ADR-7 — Strict install gate; runtime degrades safely
**Decision.** `komi-learn install` verifies every requirement *for real* (incl. a
live model call) and **fails loudly** if unmet; but at *runtime* a hook never
crashes the session — it no-ops.
**Context.** "No hacks": if install says OK, it works. But a background hook must
never break the user's live agent.
**Trade-off.** Gain: honest setup + un-killable sessions. Give up: install can
refuse on a flaky/restricted environment (escape hatch: `--allow-incomplete`).
**Reversibility.** Easy — gate strictness is policy, not structure.

## ADR-8 — One Adapter contract; host plumbing stays in the adapter
**Decision.** `komi.adapters.base.Adapter` (ABC) defines `recall()` +
`on_session_end()`; `ClaudeCodeAdapter` implements it. The engine never imports an
adapter (dependency points adapter → engine only).
**Context.** "Works everywhere" needs the engine to be genuinely host-agnostic and
a second host to be a known surface, not copy-paste.
**Trade-off.** Gain: provable universality, clean dependency direction, Phase-6
ready. Give up: a little indirection now for a host (Claude Code) that's the only
one today.
**Reversibility.** Easy — additive.

## ADR-9 — Corroboration is a transient count of distinct signers, never part of the id
**Decision.** A pool learning carries a `signatures` array (one entry per distinct
contributor who signed the *same* content). Its corroboration level = the number of
*distinct, valid* signers, computed at pull time and attached to the in-memory
`Learning` (`corroboration`) + the index column. It is **excluded** from
`content_view()` / the content-addressed id. The legacy single-`signer` shape is
treated as signature #1 (back-compatible); the array is authoritative when present.
**Context.** "Verified" (valid signature) ≠ "good." Independent agreement is a real
trust signal, and the content-addressed id already makes it *mechanically*
detectable: two people who distill the same lesson produce the same file, and each
signs a message binding their own pubkey (so signatures can't be replayed under
another identity — ADR re: signing scheme). But the same lesson must hash
*identically* regardless of how many have signed it — otherwise corroboration would
fork the very files it's meant to merge.
**Trade-off.** Gain: a trust *hint* (`pool.min_corroboration`) + a recall ranking
nudge, with **zero new dependencies** and no id churn; old files and the live pool
stay valid. Give up: the count is recomputed on every pull (cheap) rather than
stored in the content; a signing-scheme change still invalidates signatures and
needs a re-sign pass (`resign_seeds.py`) — corroboration doesn't change that.
**Reversibility.** Easy — the array is additive; drop the bonus/gate and the system
reverts to binary verified/not. The authoritative-array rule is what keeps the
identity-swap defense intact (legacy-field tampering is ignored; parity-tested).

**Sybil resistance — distinct key ≠ distinct person, so count distinct ACCOUNTS.**
A contributor key is an Ed25519 keypair generated locally for free, so "N distinct
keys" is forgeable: one attacker mints N keys and signs the same content under each
to fabricate a high count. Flagged Critical in the 3-persona security review.
**Fix (shipped, Phase 7):** each signature binds the contributor's GitHub username
(`github_user`) *inside the signed message* (so it can't be swapped post-signature),
and corroboration counts **distinct accounts, not keys** (`_identity` in
`corroboration.py`) — one person's many keys under one account count once. The pool's
CI enforces it: a `--identity` step requires every signature a PR *adds* to be bound
to the **PR author's** account (hard fail otherwise) and clears an account-age bar
(`--identity`/`check_author_binding` in the vendored `verify.py`; mirrored in the
engine). Sybil now costs N established GitHub accounts that each open a PR, not N free
keys. **Defense in depth retained:** the count is still clamped to
`MAX_COUNTED_SIGNERS` (3) and recall only *filters/down-weights* on corroboration,
never *admits* otherwise-excluded content; the recall bonus is bounded (≈0.11 max).
**Back-compat:** `github_user` is added to the signed bytes only when non-empty, so
every pre-Phase-7 signature (and the seeds) still verifies byte-identically; a legacy
unbound signature still counts (by key) but earns no *account-verified* corroboration.
A pool wanting the strong guarantee requires `github_user` via CI + branch protection.
