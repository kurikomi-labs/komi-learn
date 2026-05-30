# komi-learn — Roadmap

*Living document. Reflects what's actually built (verified against the code), not aspiration.*

## ✅ Shipped

**Phase 0 — Research & architecture.** Hermes/Letta/PAM synthesis; full spec. (`docs/01`, `docs/02`)

**Phase 1 — Personal learning loop.** Content-addressed `Learning` model (BLAKE3, tamper-evident); Store (Markdown + skills/ + SQLite FTS); distiller with anti-capture rules; hybrid classifier (deterministic safety floor → LLM scope); relevance-ranked recall with data-not-instructions framing.

**Phase 2 — Global Learnings pool (GitHub-backed).** A repo of signed `.md` files — no server. PR-based contribution, periodic git sync, local re-verification, CI safety gate. **Live** on `kurikomi-labs/komi-pool` (real PR → CI green → merge → pull, proven).

**Phase 3 — One-command install + OAuth.** `komi-learn install/doctor/status/sync/login/curate`. OAuth-first distillation (free, no key) with API-key fallback; **strict requirements gate** (real model-call verification, fails loudly, no silent degradation); runtime stays safe (never crashes a session). Live and verified on a real machine.

**Phase 4 — The Curator.** Slow (~weekly) consolidation pass: deterministic pruning (archive, never delete, stale+unused+low-confidence; pinned/pool exempt) + LLM "umbrella" consolidation of overlapping skills. Cadence-guarded at SessionStart; writes `CURATION_REPORT.md`; `komi-learn curate`. *(Also closed a gap here: procedural learnings now persist as `skills/<slug>/SKILL.md`.)*

**Reviews (4 lenses).** Adversarial correctness/security bug-hunt, then AI-Engineer, Security-Engineer, and Software-Architect persona reviews — all real findings fixed + regression-tested (incl. a CRITICAL recall prompt-injection fence-escape). See `docs/04-ai-engineering-review.md`, `docs/05-adr-log.md`.

**Phase 5 review (3 lenses, on corroboration + semantic clustering).** Security/AI/Architect personas re-reviewed the Phase 5b + clustering work; all findings fixed + regression-tested. Highlights: **Sybil interim hardening** (corroboration clamped to 3, advisory-only, never a hard gate — keys are free to mint, so distinct-key ≠ distinct-person; GitHub-account binding deferred to Phase 7 — see ADR-9); pull made crash-proof against a malformed pool file (one bad file no longer disables all community recall); signature-array + parsed-block **DoS caps**; **`(id, origin_root)` composite index identity** (a pool copy no longer overwrites/evicts the user's local learning of the same id); `corroboration` made structurally transient (never deserialized from content, never written to Markdown); clustering threshold **re-calibrated 0.45→0.58** against the real model with a labeled-set regression test, mutual-similarity clustering (no star-FPs), and a stronger consolidator contract; curator clustering vectorized (numpy) + reuses persisted vectors; CI **append-only signature** check + branch-protection guidance; shared adapter **config schema** (Codex had silently dropped 6 env vars).

**Phase 6 — Second host adapter (OpenAI Codex CLI).** Proves the engine is genuinely host-agnostic. `komi/adapters/base.py` Adapter ABC made real; `komi/adapters/hooklib.py` holds the host-neutral hook logic; `komi/adapters/codex/` is a THIN shim (CodexAdapter + ~/.codex paths + OpenAI/codex LLM + hooks). `komi-learn install --host codex`. **Demonstrated** end-to-end: a learning distilled in a Codex session is recalled in the next, same engine, files under `$CODEX_HOME`, zero Claude Code (`examples/demo_codex_host.py`, `tests/test_codex_adapter.py`). *(Live Codex auth not exercised from the build sandbox — same caveat as Claude Code distill; verify in a real Codex session.)*

## 🔜 Next

**Phase 5 — Trust & quality at scale**
- ✅ **Semantic recall (done).** Meaning-based recall via a local embedding model
  (`komi-learn[smart]`), keyword fallback when absent. `engine/embed.py`,
  `vector_search`, semantic-first `_candidate_hits`. Verified with the real model.
- ✅ **Corroboration-based trust (done).** A pool learning carries a `signatures`
  array — one per distinct contributor who independently signed the same
  content-addressed lesson. `pull` counts *distinct valid* signers and gates on
  `pool.min_corroboration`; recall adds a small log-dampened bonus so
  well-corroborated community knowledge ranks higher (never overriding relevance).
  Publishing an already-present learning by a new signer *appends* their signature
  (corroboration ↑) instead of being a no-op. Legacy single-signer files stay valid
  (no re-signing); the vendored CI verifier counts corroboration in lockstep
  (parity-tested). No new dependencies. `pool/corroboration.py`, `engine/recall.py`,
  `engine/store.py` (corroboration column), `tests/test_corroboration.py`.
- ✅ **Embedding-based clustering (done).** When the embedding model is present the
  curator clusters procedural learnings by *meaning* (cosine ≥ threshold, calibrated
  ~0.45 against the real model) instead of shared title-word/tag — so conceptually
  related lessons that share no surface form (e.g. "ripgrep" vs "ag" for code search)
  get proposed for the same umbrella. Deterministic greedy seed-based grouping;
  lexical clustering stays as the zero-dep fallback; the LLM consolidator remains the
  real merge gate. `engine/curator.py` (`_cluster_semantic`), `tests/test_semantic_clustering.py`.

**Phase 5 is complete.** ✅ Semantic recall · ✅ Corroboration trust · ✅ Semantic clustering.

**Phase 6 — Second host adapter** *(proves "works for every agent")*
- A non–Claude-Code adapter (Codex, or a chat UI) behind the same two-method interface (`recall()` + `on_session_end()`). The real test that the substrate isn't Claude-specific.
- Persona validation (developer / finance / student / scientist on one substrate).

**Phase 7 — Polish & open up** *(in progress)*
- ✅ Lean, install-first README; root MIT LICENSE; public repo metadata.
- 🔜 PyPI distribution (`pip install komi-learn`) — package built + verified; publish pending.
- 🔜 Signer↔GitHub-account binding for corroboration (the Sybil fix deferred from the Phase 5 review — see ADR-9).
- 🔜 Review-queue inspection UI (approve/reject pending global contributions).
- 🔜 Erasure / redaction pipeline (PAM "right to be forgotten" — designed in §7.4, not built).
- 🔜 Plugin-marketplace distribution; docs site.

## Known gaps / honest notes
- Recall ranking is semantic (embeddings) when the model is installed, keyword FTS otherwise; both feed the same blend + a corroboration bonus.
- Trust now has corroboration weighting (distinct-signer count), but the pool is young — `min_corroboration` defaults to 1 until enough lessons have independent signers to make a higher gate meaningful.
- Two hosts proven (Claude Code + Codex) via the shared engine; broader persona validation (finance/student/scientist) still unproven end-to-end.
- The pool repo's vendored `verify.py` must stay in sync with the engine's verification + corroboration logic (parity-tested in `tests/test_review_fixes.py` and `tests/test_corroboration.py`). After any signing-scheme change, re-run `pool-repo-template/.github/scripts/resign_seeds.py`.
- Repos are public (Phase 7). Corroboration's distinct-signer count is Sybil-forgeable until signer↔account binding lands — it's clamped + advisory-only meanwhile (ADR-9).
