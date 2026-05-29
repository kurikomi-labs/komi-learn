# komi-learn — Architecture Specification (v1)

*Status: draft for review. Companion to `01-research.md`. Decisions locked 2026-05-29; see §10 for the ones open to your override.*

---

## 1. What we're building

A **continuous-learning layer** that rides underneath an AI agent and, with **zero user friction**:

1. **Observes** each session's work.
2. **Distills** durable lessons in a background pass (cache-warm, never disturbing the live turn).
3. **Classifies** each lesson by scope — `personal` / `project` / `global-candidate` — and by category.
4. **Persists** lessons to the right store, consolidating toward few rich "umbrella" skills (a slow curator prevents rot).
5. **Recalls** the relevant lessons into the next session automatically.
6. Optionally **contributes** scrubbed, provenance-verified, anonymized lessons to a **public Global Learnings pool**, and **pulls** trusted global lessons back down.

v1 ships as a **Claude Code plugin**. The learning *engine* is host-agnostic; Claude Code is the first adapter.

### Naming
- **Learning** — one durable unit of knowledge (the atom). Maps to a memory entry or a skill patch.
- **Skill / umbrella** — a class-level procedural document (`SKILL.md` + `references/`/`templates/`/`scripts/`).
- **Identity (USER)** — facts about who the user is and how they want to be served.
- **Pool** — the shared/global knowledge store.

---

## 2. The three planes

```
┌──────────────────────────────────────────────────────────────────────┐
│  HOST PLANE  (Claude Code today; Codex/others later)                   │
│  hooks ─ SessionStart (recall in) · Stop/SubagentStop (distill out)    │
│  skills ─ umbrellas live as Claude Code skills (auto-triggered)        │
└───────────────▲───────────────────────────────────┬───────────────────┘
                │ additionalContext                  │ transcript (JSONL)
┌───────────────┴───────────────────────────────────▼───────────────────┐
│  ENGINE PLANE  (host-agnostic, the product)                            │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Recall  │  │ Distiller│  │ Classifier│ │ Curator  │  │ Store API │  │
│  │ (in)    │  │ (LLM)    │  │ (rules+LLM)│ │ (slow)  │  │ (CRUD)    │  │
│  └─────────┘  └──────────┘  └──────────┘  └──────────┘  └───────────┘  │
│         local stores:  USER.md · MEMORY.md · skills/ · index.db (FTS)  │
└───────────────▲───────────────────────────────────┬───────────────────┘
                │ pull trusted globals               │ contribute scrubbed
┌───────────────┴───────────────────────────────────▼───────────────────┐
│  POOL PLANE  (public Global Learnings — PAM-style)                     │
│  scrub → sign → DAG → moderate → publish; query → verify → rehydrate   │
└────────────────────────────────────────────────────────────────────────┘
```

The three planes are deliberately decoupled: the **engine** never assumes Claude Code, and the **pool** never trusts the engine (it re-verifies everything).

---

## 3. Data model

One schema underlies everything, mapping onto the PAM five-type model (`E/S/P/W/I`). A **Learning** is the unit. (komi-learn focuses on the durable types — Identity, Semantic, Procedural — and uses Episodic only as transient distill input.)

### 3.1 The Learning record

```jsonc
{
  "id": "blake3:9f86d081…",        // content-addressed: BLAKE3(canonical_json(everything below except id/sig))
  "schema": "komi.learning/1",
  "type": "identity | semantic | procedural",   // PAM I / S / P
  "scope": "personal | project | global",
  "category": "tooling | workflow | preference | domain-knowledge | pitfall | environment",
  "title": "Run pytest with -p no:cacheprovider in this monorepo",
  "body": "…the actual lesson, written as data, not instructions…",
  "trigger": "When running tests in a uv-managed monorepo",   // 'use when' — drives recall
  "confidence": 0.0,                // 0–1, raised by repeat observation / successful reuse
  "evidence": {                     // provenance, kept LOCAL; never published
    "session_id": "…",
    "observed_at": "2026-05-29T14:00:00Z",
    "signal": "user-correction | technique | fix | repeated-pattern",
    "transcript_span": [124, 161]   // line range in the JSONL, for audit
  },
  "provenance": {                   // populated only when shared (PAM)
    "parent_ids": [],               // Merkle-DAG: which learnings/observations this derived from
    "origin": "agent:claude-code",
    "signature": null               // Ed25519 over the root, set at publish time
  },
  "usage": { "recalled": 0, "reused": 0, "last_used": null },
  "lifecycle": { "created_at": "…", "updated_at": "…", "state": "active | archived" },
  "tags": ["pytest", "uv", "monorepo"]
}
```

Design notes:
- **`id` is the BLAKE3 of the content** → any edit changes the id → tamper-evident, dedup-by-content for free.
- **`trigger` is the recall key** — mirrors a skill's "Use when…". Recall ranks on trigger/tag/body similarity.
- **`evidence` stays local forever.** It carries the raw provenance (session, transcript span) used for auditing *your own* learnings; it is **stripped before any contribution** to the pool.
- **`confidence`** starts low and only rises with corroboration (seen again) or successful reuse — this lets the curator prune low-confidence noise.

### 3.2 Physical stores (Claude Code adapter)

| Store | Path | Format | Loaded into context |
|---|---|---|---|
| Identity (USER) | `~/.claude/komi/USER.md` | Markdown, `§`-delimited entries | Full, at SessionStart |
| Personal memory | `~/.claude/komi/MEMORY.md` | Markdown, `§`-delimited | Index (~first 25KB) at SessionStart |
| Project memory | `<proj>/.claude/komi/MEMORY.md` | Markdown | At SessionStart when in project |
| Skills (umbrellas) | `~/.claude/skills/<name>/SKILL.md` + project `.claude/skills/` | SKILL.md + `references/`/`templates/`/`scripts/` | Descriptions always; bodies on trigger |
| Structured index | `~/.claude/komi/index.db` | **SQLite + FTS5** | Queried by Recall; not injected |
| Global cache | `~/.claude/komi/pool/` | Learning records (JSON) + signatures | Pulled subset → eligible for recall |
| Review queue | `~/.claude/komi/queue/` | Pending contributions (JSON) | Never auto-injected |

> We deliberately reuse Claude Code's existing auto-memory conventions and the `agentskills.io` skill format so komi-learn's output is *also* useful even with the plugin disabled. Nothing is locked in a proprietary blob.

The **`index.db`** is the engine's brain: every Learning (across USER/MEMORY/skills) is mirrored as a row with its `trigger`, `tags`, `body`, `scope`, `confidence`, `usage`, embedded into an FTS5 table for fast recall and into a normal table for the curator's clustering. The Markdown files remain the human-readable source of truth; `index.db` is a derived cache that can be rebuilt by re-scanning the files.

---

## 4. The learning loop (engine)

Faithful to Hermes' background-review design, adapted to Claude Code's hook + Agent SDK surface.

### 4.1 Recall — `SessionStart` & `UserPromptSubmit`

```
SessionStart hook
  → engine.recall(cwd, recent_context)
      1. load USER.md  (full)                          → identity block
      2. load MEMORY.md index + project MEMORY.md       → memory block
      3. query index.db (FTS5) for top-K by relevance   → just-in-time block
         relevance = 0.2·recency + 0.3·salience(confidence·reuse)
                   + 0.4·similarity(cwd, recent_files, prompt) + 0.1·depth
      4. frame everything as DATA-not-instructions (PAM markers)
  → emit hookSpecificOutput.additionalContext
```

- **Zero friction:** nothing typed; learnings appear in context as the session opens.
- **Frozen-snapshot discipline (Hermes lesson):** recall happens at `SessionStart`; we do **not** mutate context mid-turn, preserving Claude Code's prefix cache. `UserPromptSubmit` recall is *optional* and used only for sharply on-topic just-in-time pulls (off by default in v1 to keep the cache warm).
- Global learnings that were pulled into the local cache are eligible here, but **clearly labelled** as community knowledge and wrapped in the data-not-instructions frame (they're untrusted input).

### 4.2 Distill — `Stop` / `SubagentStop`

This is the analogue of Hermes' forked review agent.

```
Stop hook  (Claude finished responding)
  → if turns_since_distill ≥ NUDGE_TURNS (default 8)  OR  session ending:
       spawn DETACHED background process:  komi-distill <transcript_path> <session_id>
       (hook returns immediately — never blocks the user)

komi-distill  (tiny Agent SDK / API wrapper — the "review fork")
  1. read transcript JSONL  (the conversation snapshot)
  2. run the DISTILL PROMPT (see §5) → candidate learnings[]  (structured output)
  3. for each candidate → Classifier (§6) → {drop | personal | project | global-candidate}
  4. write survivors via Store API:
       - identity/preference  → USER.md
       - durable fact         → MEMORY.md (personal or project)
       - technique/pitfall    → patch an existing umbrella skill, else queue a new one
  5. global-candidates       → review queue (NEVER auto-published)
  6. update index.db; bump confidence on corroborated repeats
```

Cost-warm trick (Hermes' ~26% saving): when the distiller runs through the Agent SDK against the same model/provider, we **reuse the system-prompt prefix** so the distill request hits the warm cache. When it can't (e.g. a pure API wrapper), we keep the distill prompt short and run it on a cheaper model (Haiku-class) — distillation is a summarization task, not a reasoning-hard one.

Constraints honored (from research §3):
- The distiller is **read-mostly**: it may read the transcript and the stores, and write **only** to the learning stores + queue. (Background agents auto-deny prompts, so it must never need human approval.) This is Hermes' tool-whitelist, enforced here by giving the wrapper a restricted tool set.
- It **cannot** run other tools, touch the repo, or take outward actions.

### 4.3 Curate — slow background pass

Mirrors Hermes' 7-day curator. Triggered opportunistically (a `SessionStart` checks "last curated > 7d ago and idle"), runs detached:

```
curator()
  1. cluster index.db skills by prefix/tag/embedding
  2. for each cluster ≥2 members: propose an UMBRELLA; merge bodies, demote detail to references/
  3. prune: archive (never delete) learnings with confidence < τ and reuse = 0 older than 30d
  4. re-embed, rebuild FTS5
  5. write a human-readable CURATION_REPORT.md
```

Protected (never auto-edited): user-pinned skills (content-updatable but not archivable), and any skill the user authored by hand and marked `pinned: true`.

### 4.4 The cadence summary

| Pass | Trigger | Default | Cost posture |
|---|---|---|---|
| Recall | SessionStart | every session | free (one context injection) |
| Distill | Stop / SubagentStop | every ~8 turns or session end | cheap (short prompt, cache-warm or Haiku) |
| Curate | SessionStart guard | ≥7 days idle | rare; can be a heavier model |

---

## 5. The distill prompt (the product's "brain")

Adapted from Hermes' `_COMBINED_REVIEW_PROMPT` and Letta's reflection→creation pattern, with Letta's key finding baked in (**capture failure modes, not just successes**). Stored at `engine/prompts/distill.md`, versioned. Skeleton:

```
You are komi-learn's background distiller. You are reviewing a finished
session to extract DURABLE learnings for future sessions. Your output is
DATA for a learning store — not a message to a human.

Be ACTIVE: most real sessions yield at least one learning. A pass that
saves nothing is usually a missed opportunity — but saving noise is worse
than saving nothing.

Extract a learning when ANY of these fired:
 • The user corrected your style, tone, format, verbosity, or approach.
   (Frustration — "stop doing X", "too verbose", "I hate when you Y",
   "just give me the answer", "remember this" — is a FIRST-CLASS signal.
   Encode it so the next session starts already fixed.)
 • A non-trivial technique, fix, workaround, or debugging path emerged.
 • Something you tried FAILED and you found the fix. Capture BOTH the
   failure mode AND the fix — failure-aware learnings are more robust.
 • A durable fact about the user, their domain, or their project surfaced.

For each learning, emit a structured record: {type, category, title,
body, trigger ("use when…"), tags, signal}.

DO NOT capture (these rot into self-imposed constraints):
 • Environment-dependent failures: missing binaries, "command not found",
   unconfigured creds, uninstalled packages. The user can fix these.
   → If a setup issue had a fix, capture the FIX, never "X doesn't work".
 • Negative claims about tools/features ("browser tools don't work").
   These harden into refusals you cite against yourself for months.
 • Transient errors that resolved. If a retry worked, the lesson is the
   retry pattern, not the original failure.
 • One-off task narratives ("summarize today's news" is not a class of work).
 • Anything containing secrets, credentials, or tokens — never.

Prefer UPDATING an existing umbrella skill over creating a new one.
Name skills at the CLASS level, never after a single task/PR/error.
```

The distiller returns structured JSON (enforced via the Agent SDK's structured-output / a tool schema), which the engine consumes deterministically — no fragile parsing.

---

## 6. Classification — how a learning gets its scope (HYBRID)

> This is the "used with thought; some knowledge is global" logic. The user was unsure on approach; we chose **Hybrid (rules gate → LLM decides)** because a public pool needs a *hard safety floor* AND nuance. Open to override (§10).

```
classify(learning) →
  STAGE 1 — DETERMINISTIC SAFETY FLOOR  (cannot be reasoned around)
    reject-to-personal if body/title/tags match ANY:
      • secret/credential patterns (API keys, tokens, JWT, PEM, .env values)
      • PII (emails, names, phone, addresses) via detectors
      • machine/user-specific identifiers (absolute home paths, usernames,
        hostnames, internal URLs, private IPs)
      • repo/org/project proper nouns from the local git remote + cwd
    → if matched: scope = personal (or project if only project-identifiers). STOP.

  STAGE 2 — LLM SCOPE JUDGMENT  (only on survivors)
    Ask: "Is this lesson GENERALLY TRUE and USEFUL to anyone doing this class
    of work, independent of this user/project/machine? Or is it specific to
    THIS project's conventions?"
      • general technique / language-or-tool behavior / broadly-applicable
        pitfall, with NO identifiers  → global-candidate
      • depends on this project's structure, naming, or choices → project
      • about the user themselves / their preferences → personal (Identity)
    Also assign `category` and a generalization rewrite: the LLM REWRITES a
    global-candidate to strip residual specificity ("in this repo" → "in a
    uv-managed monorepo") so the published form is genuinely general.

  STAGE 3 — never auto-publish. global-candidates land in the REVIEW QUEUE.
```

Rationale recorded for review: Stage 1 guarantees no identifier/secret can *ever* reach the pool even if the LLM misjudges; Stage 2 supplies the nuance Hermes relies on. Strictly safer than LLM-only, strictly more nuanced than rules-only.

---

## 7. Global Learnings — the public pool (PAM-style, full trust pipeline)

The killer feature, and the part that most needs to be right. It must be: **anonymous, tamper-evident, injection-safe, moderatable, and erasable.** Design follows the "Portable Agent Memory" protocol (arXiv 2605.11032).

> **v1 implementation decision (2026-05-29): the pool is a GitHub repo of `.md` files — no custom server.** A dedicated repo (`kurikomi-labs/komi-pool`) holds one Markdown file per learning under `learnings/<category>/<id>.md`; each file carries the human-readable lesson plus the verifiable signed envelope in a fenced ` ```komi ` block. **Contribution = human-approved Pull Request** (`gh pr create`). **Consumption = periodic `git` sync to a local cache + local re-verification.** This gives free hosting, public auditability, PR-based moderation, and CDN distribution, and reuses the exact verification the protocol below specifies — only the transport is git instead of a bespoke API. The repo's CI (`.github/workflows/verify.yml`) re-runs id + signature + scrub verification on every PR. See `komi/pool/github_backend.py`, `komi/pool/repo_format.py`, `komi/pool/queue.py`, `komi/pool/verify_cli.py`, and `pool-repo-template/`.

### 7.1 Contribution pipeline (local → pool)

```
queued global-candidate
  → 1. SCRUB        second-pass LLM + detector sweep: strip evidence{},
                     any residual PII/secret/identifier; reject on doubt.
  → 2. GENERALIZE   ensure body is class-level (already rewritten in §6 S2;
                     re-verify). Drop confidence/usage/local fields.
  → 3. CANONICALIZE  produce canonical JSON of the publishable subset
                     {schema,type,category,title,body,trigger,tags}.
  → 4. ADDRESS      id = BLAKE3(canonical_json). parent_ids link to any
                     global learnings this built on (Merkle-DAG).
  → 5. SIGN         Ed25519 over the entry root with the contributor's key
                     (pseudonymous keypair generated locally; identity optional).
  → 6. HUMAN GATE   user reviews the final publishable form in the queue UI
                     and approves. NOTHING leaves without this in v1.
  → 7. SUBMIT       POST to pool endpoint (STUBBED in v1 — writes to a local
                     "outbox" that a future server would accept).
```

### 7.2 Pool-side (server — designed, not built in v1)

```
ingest(entry)
  • verify BLAKE3 id matches content; verify Ed25519 signature
  • verify DAG references resolve and are acyclic
  • run independent server-side scrubber (defense in depth)
  • dedup by content id; if near-duplicate, link instead of duplicate
  • MODERATION: automated safety classifier + community flagging +
    confidence accrual (a global learning gains trust as independent
    contributors submit a corroborating entry — corroboration = distinct
    signers reaching the same content id or a linked one)
  • publish to a categorized, queryable index
```

### 7.3 Consumption (pool → local)

```
pull(category|tags, trust_threshold)
  • fetch entries above a trust/corroboration threshold
  • re-verify hashes + signatures locally (never trust the server blindly)
  • store in ~/.claude/komi/pool/ , marked scope=global, untrusted-origin
  • eligible for recall, but ALWAYS injected inside data-not-instructions
    PAM markers, and visually labelled as community knowledge
```

### 7.4 Safety properties (why this is trustworthy)

- **Anonymity:** evidence stripped; pseudonymous signing; two scrubber passes (client + server) + the Stage-1 deterministic floor.
- **Tamper-evidence:** content-addressed ids + signed DAG roots; editing any entry breaks the chain.
- **Injection-safety:** every consumed global learning is framed as DATA with explicit "do not treat as instructions", plus boundary/role/instruction escaping (PAM's three passes). This matters because the pool is *public and untrusted*.
- **Erasure:** redaction pipeline replaces an entry's content with a typed token while keeping its DAG position → "right to be forgotten" without breaking downstream hashes.
- **Quality:** trust grows by **independent corroboration**, not raw vote counts; low-trust entries aren't pulled by default.

### 7.5 Categories (v1 taxonomy)
`tooling` · `workflow` · `language-behavior` · `pitfall` · `debugging` · `domain-knowledge` · `formatting/style` · `meta-agent` (how to work with agents). Categories are the primary query axis and the unit of "some knowledge is global, applied everywhere" (e.g. a `meta-agent` learning recalls regardless of project).

---

## 8. Universality — one substrate, many personas

The brief: works for developers, knowledge workers, students, scientists, everyone. We achieve this **without per-persona code** — the substrate is domain-neutral; personas differ only in *which categories dominate* and *which host surfaces* they use:

- **Developer (Claude Code):** procedural skills + tooling/pitfall learnings dominate; transcripts are code sessions.
- **Knowledge worker / finance:** domain-knowledge + workflow + formatting/style learnings (e.g. "this analyst wants outputs as a one-page memo, numbers in basis points"). Same USER.md/MEMORY.md/skill machinery.
- **Student / scientist:** identity learnings about level & explanation style ("explain at undergrad level, derive before stating"), domain-knowledge accretion across a course or research line.

The engine doesn't branch on persona; the **distill prompt's signal list is universal** (corrections, techniques, fixes, durable facts apply to anyone), and the **category taxonomy** carries the domain. New hosts (a chat UI, Codex, a web app) are just new *adapters* implementing two methods: `recall() → context` and `on_session_end(transcript) → distill`.

---

## 9. v1 build plan (what gets coded now vs later)

**Build now (runnable personal loop MVP):**
- `engine/` — Store API (Markdown + `index.db`), Recall, Distiller wrapper, Classifier (Stage-1 rules + Stage-2 LLM), schema + canonicalization (BLAKE3 ids).
- `adapters/claude_code/` — `SessionStart` recall hook, `Stop` distill hook, plugin manifest, the distiller invoked via Agent SDK/API.
- Local end-to-end: a real session → distill → learnings on disk → recalled next session.

**Designed + stubbed now:**
- Global pool: scrub/generalize/canonicalize/sign/queue all implemented locally; the network `submit`/`pull` write to a local outbox/inbox (no server). Ed25519 keypair generated locally.

**Later (post-review loop):**
- The pool server + moderation + corroboration trust.
- Additional host adapters (Codex, chat).
- Embedding-based recall/clustering (v1 uses FTS5 + heuristics; embeddings are an upgrade).
- A `verify`/inspection UI for the review queue.

---

## 10. Decisions open to your override (after review)

1. **Classification = Hybrid** (rules floor → LLM). Alt: LLM-only (more nuance, less safe) or rules-only (predictable, blunt). *My pick stands unless you say otherwise.*
2. **Distill cadence = every ~8 turns + session end.** Hermes uses 10 turns / 10 iterations; tune to taste.
3. **Distiller model.** Cache-warm same-model (cheapest if Agent SDK path) vs. dedicated cheap model (Haiku-class). v1 supports both; default = whatever the host session uses, via SDK.
4. **`UserPromptSubmit` just-in-time recall** off by default (protects prefix cache). Toggle on if you want sharper mid-session pulls.
5. **Human gate before publish = mandatory in v1.** Could later offer an "auto-publish high-confidence, fully-scrubbed `meta-agent`/`language-behavior` learnings" mode — but not until trust is proven.

---

*Next: build `engine/` + the Claude Code adapter per §9, then we review and loop.*
