# komi-learn — Roadmap

*Living document. Reflects what's actually built (verified against the code), not aspiration.*

## ✅ Shipped

**Phase 0 — Research & architecture.** Hermes/Letta/PAM synthesis; full spec. (`docs/01`, `docs/02`)

**Phase 1 — Personal learning loop.** Content-addressed `Learning` model (BLAKE3, tamper-evident); Store (Markdown + skills/ + SQLite FTS); distiller with anti-capture rules; hybrid classifier (deterministic safety floor → LLM scope); relevance-ranked recall with data-not-instructions framing.

**Phase 2 — Global Learnings pool (GitHub-backed).** A repo of signed `.md` files — no server. PR-based contribution, periodic git sync, local re-verification, CI safety gate. **Live** on `kurikomi-labs/komi-pool` (real PR → CI green → merge → pull, proven).

**Phase 3 — One-command install + OAuth.** `komi-learn install/doctor/status/sync/login/curate`. OAuth-first distillation (free, no key) with API-key fallback; **strict requirements gate** (real model-call verification, fails loudly, no silent degradation); runtime stays safe (never crashes a session). Live and verified on a real machine.

**Phase 4 — The Curator.** Slow (~weekly) consolidation pass: deterministic pruning (archive, never delete, stale+unused+low-confidence; pinned/pool exempt) + LLM "umbrella" consolidation of overlapping skills. Cadence-guarded at SessionStart; writes `CURATION_REPORT.md`; `komi-learn curate`. *(Also closed a gap here: procedural learnings now persist as `skills/<slug>/SKILL.md`.)*

**Reviews (4 lenses).** Adversarial correctness/security bug-hunt, then AI-Engineer, Security-Engineer, and Software-Architect persona reviews — all real findings fixed + regression-tested (incl. a CRITICAL recall prompt-injection fence-escape). See `docs/04-ai-engineering-review.md`, `docs/05-adr-log.md`.

**Phase 6 — Second host adapter (OpenAI Codex CLI).** Proves the engine is genuinely host-agnostic. `komi/adapters/base.py` Adapter ABC made real; `komi/adapters/hooklib.py` holds the host-neutral hook logic; `komi/adapters/codex/` is a THIN shim (CodexAdapter + ~/.codex paths + OpenAI/codex LLM + hooks). `komi-learn install --host codex`. **Demonstrated** end-to-end: a learning distilled in a Codex session is recalled in the next, same engine, files under `$CODEX_HOME`, zero Claude Code (`examples/demo_codex_host.py`, `tests/test_codex_adapter.py`). *(Live Codex auth not exercised from the build sandbox — same caveat as Claude Code distill; verify in a real Codex session.)*

## 🔜 Next

**Phase 5 — Trust & quality at scale** *(best once the pool has volume)*
- Corroboration-based trust: a learning signed by N independent contributors ranks higher; low-trust isn't pulled by default.
- Confidence/reuse-driven ranking; pool-side near-duplicate linking.
- Embedding-based recall & clustering (upgrade from keyword FTS → semantic similarity). Sharper recall, better umbrella detection.

**Phase 6 — Second host adapter** *(proves "works for every agent")*
- A non–Claude-Code adapter (Codex, or a chat UI) behind the same two-method interface (`recall()` + `on_session_end()`). The real test that the substrate isn't Claude-specific.
- Persona validation (developer / finance / student / scientist on one substrate).

**Phase 7 — Polish & open up**
- Review-queue inspection UI (approve/reject pending global contributions).
- Erasure / redaction pipeline (PAM "right to be forgotten" — designed in §7.4, not built).
- PyPI + plugin-marketplace distribution; docs site.
- Flip both repos public.

## Known gaps / honest notes
- Recall ranking is FTS + heuristics (no embeddings yet).
- Trust is binary (verified / not) — no corroboration weighting yet.
- Single host (Claude Code); "universal" is architected but unproven on a 2nd host.
- The pool repo's vendored `verify.py` must stay in sync with the engine's verification logic (regression-tested).
- Both repos private during testing.
