# komi-learn — Deep Research Report
### Continuous, zero-friction learning for AI agents

*Research compiled 2026-05-29. Primary sources: the Hermes Agent source tree (nousresearch/hermes-agent v0.15.x), official Claude Code & Agent SDK docs (2026), and recent academic work on agent memory & skill learning.*

---

## 0. The thesis

Today's AI agents are **amnesiac geniuses**. Each session starts from zero. They re-derive the same workarounds, re-learn your preferences, repeat corrected mistakes, and never compound. The fix is an externalized learning loop: the agent watches its own work, distills durable lessons, persists them outside the context window, and reloads them next time — **automatically, with no command to invoke.**

Hermes Agent proved this can be a *first-class architectural feature* rather than a bolt-on. Letta proved it produces **measurable capability gains** (+36.8% relative on Terminal Bench 2.0). A 2026 protocol paper ("Portable Agent Memory") proved learnings can be made **portable and provenance-verified across vendors** — which is exactly what a shared "Global Learnings" pool requires.

**komi-learn** is the synthesis: Hermes' loop, made model-agnostic, universal across personas, and extended with a public, anonymized, cryptographically-trustworthy global knowledge layer.

---

## 1. How Hermes Agent's learning system actually works

> Reverse-engineered from source. Several widely-repeated blog claims are **wrong**; the real mechanics are below with file references.

### 1.1 The background review loop — the heart of it

The single most important mechanism is in `agent/background_review.py`. After a turn, the main loop *may* call `spawn_background_review`, which:

1. Spawns a **daemon thread** running a **forked copy of the agent** (`_run_review_in_thread`).
2. The fork **replays the conversation snapshot** and is asked one question: *"should any skill/memory be saved or updated?"*
3. The fork runs with a **tool whitelist of only the memory + skill tools** — every other tool is denied at runtime.
4. Writes go straight to the memory/skill stores on disk. **The main conversation and its prompt cache are never touched.**

Three design choices make this cheap and safe:

- **Prefix-cache reuse.** The fork inherits the parent's *cached system prompt verbatim* (`review_agent._cached_system_prompt = agent._cached_system_prompt`) and pins `session_start`/`session_id`, so its outbound request hits the **same Anthropic prefix cache** the parent warmed. The source cites a measured **~26% end-to-end cost reduction** from this alone (PR #17276). This is a critical lesson: *a learning pass that reuses the warm cache is nearly free.*
- **No side effects.** `skip_memory=True` stops the fork from polluting external memory providers; `max_iterations=16`; `quiet_mode`; dangerous commands auto-deny (the fork can't prompt a human).
- **One source of truth for prompts.** Three prompt variants — `_MEMORY_REVIEW_PROMPT`, `_SKILL_REVIEW_PROMPT`, `_COMBINED_REVIEW_PROMPT` — selected by which trigger fired.

### 1.2 The triggers (myth-corrected)

| Mechanism | Reality (from source) | Common blog myth |
|---|---|---|
| Memory nudge | every **10 turns** (`memory.nudge_interval`) | — |
| Skill nudge | every **10 tool iterations** (`skills.creation_nudge_interval`) | "after 5+ tool calls" ✗ |
| Curator (consolidation) | every **7 days** (168h), min idle 2h; stale 30d, archive 90d | — |
| Insights | **on-demand only**, pure SQL aggregation, **no LLM**, **not persisted** | "every 15 tasks the agent reflects" ✗ |

The takeaway: the *real* loop is **turn/iteration-cadence background review** plus a **slow periodic curator**, not a task-counter reflection.

### 1.3 What the review prompt teaches — and what it forbids

The `_SKILL_REVIEW_PROMPT` is a masterclass. Its philosophy:

- **Be ACTIVE.** *"most sessions produce at least one skill update… A pass that does nothing is a missed learning opportunity, not a neutral outcome."* `'Nothing to save.'` is allowed but is explicitly *not the default*.
- **User frustration is a FIRST-CLASS skill signal.** *"stop doing X", "this is too verbose", "why are you explaining", "you always do Y and I hate it"* → embed the corrected preference **in the skill that governs that class of task**, so the next session starts already fixed. (Memory says *who the user is*; skills say *how to do this task for this user*.)
- **Preference order for where a lesson goes:** (1) patch a currently-loaded skill → (2) patch an existing umbrella skill → (3) add a support file (`references/`, `templates/`, `scripts/`) → (4) only then create a new class-level skill.

The **anti-capture list** is as valuable as the capture logic — these are the failure modes that make naive "save everything" systems rot:

- ❌ **Environment-dependent failures** (missing binaries, "command not found", unconfigured creds). *The user can fix these — they aren't durable rules.*
- ❌ **Negative claims about tools** ("browser tools don't work", "X is broken"). *"These harden into refusals the agent cites against itself for months after the actual problem was fixed."*
- ❌ **Transient errors that resolved.** If a retry worked, the lesson is the *retry pattern*, not the failure.
- ❌ **One-off task narratives** ("summarize today's market" is not a class of work).
- ✅ When a tool fails due to setup, capture the **FIX** (the install command, the env var) under a troubleshooting skill — never the bare "this doesn't work".

> This list is the difference between a system that gets *smarter* and one that accumulates **self-imposed constraints** until it's afraid of its own tools. komi-learn adopts it wholesale.

### 1.4 Memory model

- **Two built-in stores:** `MEMORY.md` (the agent's own notes) and `USER.md` (the user profile), at `~/.hermes/memories/`. Plain Markdown, entries separated by `\n§\n` (U+00A7). Soft char limits (~2200 / ~1375).
- **Full-file injection via a frozen snapshot.** At session start: load → dedupe (`dict.fromkeys`) → threat-scan (replace injection-y entries with `[BLOCKED]` *in the snapshot only*) → **freeze**. The frozen block goes into the system prompt and is *immutable for the whole session* (so the prefix cache holds). Mid-session writes hit disk immediately but don't change the live snapshot — they land next session.
- **Cross-session recall:** sessions indexed in **SQLite + FTS5**; optional external providers (Honcho, Mem0, Hindsight) can add semantic prefetch.

### 1.5 Skill model — "umbrellas", not snowflakes

- **Class-level skills.** The target library shape is a *small* set of rich, class-level skills, each a `SKILL.md` plus `references/` (session-specific detail + condensed knowledge banks), `templates/` (copy-and-modify starters), `scripts/` (re-runnable probes). The explicit anti-goal: *"a collection of hundreds of narrow skills where each one captures one session's specific bug is a FAILURE of the library."*
- **The curator** runs slowly (7-day cadence), finds **prefix-clusters** of overlapping skills, and **consolidates them into umbrellas** or demotes them to support files. It **archives, never deletes** (max destructive action). Bundled/hub-installed skills are protected; pinned skills can be content-updated but not archived.
- **Frontmatter** follows the `agentskills.io` open standard: `name`, `description` (with an embedded *"Use when…"*), `version`, `author`, `license`, `platforms`, `metadata.<vendor>.tags`, `related_skills`, `prerequisites`.

### 1.6 User modeling

Hermes uses **Honcho** (optional) for *dialectic* user modeling: a **user peer** and an **AI peer** per session, with knobs for cadence (how often it reasons), depth (1–3 reasoning passes), and intensity, plus cold-start vs warm-start prompt strategies. ("Hermes profiles" are something different — fully isolated agent instances, not a learned model.)

---

## 2. The broader field — what else informs the design

### 2.1 Letta "Skill Learning" (2026) — empirical proof + the reflection pattern

- **Two-stage learning:** (1) **Reflection** — given the agent's trajectory, evaluate whether it solved the task, whether each step was justified, and what repeats could be abstracted; optionally enrich with verifier feedback. (2) **Creation** — feed the reflection to a learning agent that uses a skill-creator to write a skill with *approaches, pitfalls, and verification strategies*.
- **Results (Terminal Bench 2.0, 89 tasks, Sonnet 4.5 + extended thinking):** trajectory-only skills → **+21.1% relative** (and −15.7% cost, −10.4% tool calls); trajectory **+ feedback** → **+36.8% relative**.
- **Key finding:** *feedback-informed skills that encode failure modes are more robust than success-only skills.* → komi-learn must capture **what went wrong and how it was fixed**, not just what worked. (This independently validates Hermes' frustration-as-first-class-signal.)
- **Memory hierarchy:** an evolving **system prompt** (agent-specific state) + evolving **skill files** (task-specific, interchangeable between agents). Model-agnostic: a strong model can write skills a weaker model later uses.
- Avoids the **RecoveryBench degradation trap** (raw errors in-context *hurt* performance) by distilling errors into separate skill files rather than leaving them in the trajectory.

### 2.2 Voyager (the origin of skill libraries)

A skill library of **verified executable programs** that grows through exploration; retrieval by embedding similarity over skill descriptions, deterministic code execution. Lesson: *a skill is only worth keeping if it's been verified to work.* komi-learn carries a verification notion into procedural learnings.

### 2.3 Reflexion

Linguistic self-feedback stored as **episodic memory** so the agent learns from failures across attempts. Lesson: natural-language reflection is a legitimate, durable memory substrate.

### 2.4 "Portable Agent Memory" (PAM) — the Global-Learnings blueprint

This 2026 protocol paper solves the precise problem of moving learnings **across heterogeneous agents (Claude/GPT/Gemini) with verifiable trust** — i.e., what a public global pool needs.

- **Five memory types** `M = (E, S, P, W, I)`: **E**pisodic (events), **S**emantic (subject-predicate-object facts with confidence), **P**rocedural (skills/workflows with usage stats), **W**orking (transient), **I**dentity (persona/prefs). komi-learn maps cleanly onto this.
- **Content-addressed integrity:** each entry's `id` is the **BLAKE3 hash of its canonical JSON**; entries form a **Merkle-DAG** (`parent_ids`) so a semantic fact links to the episodic observation it derived from. Tampering with any entry invalidates everything downstream. The DAG root is **Ed25519-signed** by the operator.
- **Capability tokens** for scoping: permissions `{read, write, derive, redact, export, rehydrate}` over scope expressions (entry-list / component-type / **tag-predicate** with any_of·all_of·none_of / wildcard). This is how you express *personal vs shared vs global* without an all-or-nothing switch.
- **Redaction pipeline** = provenance-preserving deletion: a redacted entry keeps its DAG position but its content becomes a typed token → satisfies GDPR Art. 17 erasure **and** Art. 20 portability without breaking the hash chain.
- **Structural injection defense:** recalled memory is wrapped in typed boundary markers (`[PAM:DATA:semantic] … [/PAM:DATA]`) with an explicit directive to treat the content as **data, not instructions**, plus three escaping passes (boundary / role-marker / instruction). Essential when ingesting *public, untrusted* global learnings.
- **Re-hydration pipeline:** Verify → Filter → Rank → Compress → Format → Frame → Inject, with relevance = `0.2·recency + 0.3·salience + 0.4·similarity + 0.1·depth`.
- Reference SDK reports **Transfer Continuity 0.83–0.92** vs a no-memory baseline of 0.28–0.45 across three model families.

---

## 3. The Claude Code substrate — can we build zero-friction here? (Yes.)

The brief demands **no slash commands — it just does it, like Hermes.** Claude Code's extension surface supports exactly this:

| Need | Mechanism | Notes |
|---|---|---|
| **Load learnings with zero friction** | `SessionStart` & `UserPromptSubmit` hooks return `hookSpecificOutput.additionalContext` | Injected straight into the model's context. No command. ✅ |
| **Trigger a learning pass after work** | `Stop` / `SubagentStop` hooks fire when Claude finishes | Hooks are shell scripts; they can spawn a detached background process. ✅ |
| **Run the distill pass as a real agent** | **Claude Agent SDK** `query(...)` with an `AgentDefinition` (supports `background: true`, `memory: "user"\|"project"`) | A hook shells out to a tiny SDK wrapper — this is our analogue of Hermes' forked review agent. ✅ |
| **Persist learnings** | Auto-memory dir `~/.claude/projects/<encoded-cwd>/memory/MEMORY.md` + topic files; project/user `CLAUDE.md` with `@import` | `MEMORY.md` (first ~25KB) auto-loads at startup. ✅ |
| **Cross-session recall** | Transcripts are **JSONL** at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` | No built-in index — we build one (SQLite/FTS). Default retention 30d. |
| **Distribute the whole thing** | **Plugin**: `.claude-plugin/plugin.json` bundling `hooks/`, `skills/`, `agents/`, `.mcp.json` — hooks auto-register when enabled | This is komi-learn's shipping vehicle. ✅ |

**Hard constraints discovered (these shape the design):**

1. **Hooks can't spawn subagents directly** — they're shell scripts. We need a small **Agent SDK wrapper** (Python/TS) that the hook invokes. (Or do the distill with a direct Anthropic API call — see architecture doc.)
2. **Background subagents auto-deny permission prompts** (can't ask a human) — so the distill pass must run with a **read-mostly + write-to-learning-store-only** tool set. Mirrors Hermes' whitelist exactly.
3. **Subagents can't spawn subagents** — only the top level can; no nested delegation.
4. **Transcripts are machine-local** and expire (default 30d) — cross-machine/global sync must mirror deliberately.
5. The **frozen-snapshot lesson from Hermes applies here too**: inject learnings at `SessionStart`, don't mutate context mid-turn, to preserve Claude Code's own prefix cache.

---

## 4. Synthesis — the principles komi-learn inherits

1. **Background, forked, cache-warm distillation.** Learn in a separate pass that reuses the warm prompt prefix; never disturb the live turn. *(Hermes)*
2. **Capture corrections & failure-fixes, not just successes.** Feedback-informed learnings are measurably more robust. *(Letta + Hermes)*
3. **A strict anti-capture list.** Refuse environment failures, negative tool claims, transient errors, one-off narratives. This is what prevents rot. *(Hermes)*
4. **Umbrellas over snowflakes + a slow curator.** Consolidate toward few rich class-level skills; archive, never delete. *(Hermes)*
5. **Separate "who the user is" (Identity/USER) from "how to do this task" (Procedural/skills).** Two stores, two purposes. *(Hermes + PAM)*
6. **Zero friction via hooks.** Inject at `SessionStart`, distill at `Stop`, persist to the auto-memory dir. No slash commands. *(Claude Code)*
7. **Verified, provenance-carrying, injection-safe units for anything shared.** Content-addressed IDs, derivation DAG, capability-scoped sharing, redaction for erasure, data-not-instructions framing. *(PAM)*
8. **Model-agnostic substrate.** Learnings are plain files/records, portable across Claude/Codex/others; a strong model can teach a weaker one. *(Letta + PAM)*

These eight principles drive the architecture in `02-architecture.md`.

---

## Appendix — source map

- **Hermes loop:** `agent/background_review.py` (review fork + the three prompts), `agent/curator.py` (7-day consolidation), `agent/insights.py` (on-demand aggregation, no LLM), `agent/skill_utils.py` (frontmatter parsing), `tools/memory_tool.py` (`§` delimiter, frozen snapshot, char limits).
- **Claude Code:** hooks / skills / memory / plugins / sub-agents / settings docs + Agent SDK (subagents, sessions) — code.claude.com/docs (2026).
- **Academic:** Letta "Skill Learning" (letta.com/blog/skill-learning); "Portable Agent Memory" (arXiv 2605.11032); "Externalization in LLM Agents" (arXiv 2604.08224); Voyager; Reflexion.
