# HERMES AGENT: MEMORY AND LEARNING SYSTEM — TECHNICAL WRITEUP

## 1. THE NUDGE MECHANISM

### What is a Nudge?

A nudge is an inactivity-triggered background review spawning a forked AIAgent that replays the conversation with a special review prompt.

### Trigger Thresholds

**MEMORY NUDGE** — Turn-based counter in agent._memory_nudge_interval:
- Default: 10 turns
- Config key: memory.nudge_interval
- Set at: agent/agent_init.py line 1067
- Checked at: agent/conversation_loop.py:548-556
- Logic: agent._turns_since_memory increments each turn; when >= nudge_interval, spawn background review and reset

**SKILL NUDGE** — Iteration-based counter in agent._skill_nudge_interval:
- Default: 10 tool iterations
- Config key: skills.creation_nudge_interval
- Set at: agent/agent_init.py line 1187
- Checked at: agent/codex_runtime.py:124-129

### NUDGE PROMPT TEXT (EXACT QUOTES)

**Memory Review Prompt** (agent/background_review.py:31-45):

"Review the conversation above and consider saving to memory if appropriate.

Focus on:
1. Has the user revealed things about themselves — their persona, desires, preferences, or personal details worth remembering?
2. Has the user expressed expectations about how you should behave, their work style, or ways they want you to operate?

If something stands out, save it using the memory tool. If nothing is worth saving, just say 'Nothing to save.' and stop."

**Skill Review Prompt** (agent/background_review.py:47-159, 113 lines):

Opens: "Review the conversation above and update the skill library. Be ACTIVE — most sessions produce at least one skill update, even if small..."

Key directives:
- TARGET SHAPE: CLASS-LEVEL SKILLS with rich SKILL.md + references/
- NOT a flat list of one-session-one-skill entries
- Hard rule 1: DO NOT touch bundled/hub-installed skills
- Hard rule 2: DO NOT delete (archive only)
- Hard rule 3: DO NOT touch pinned skills
- Hard rule 4: DO NOT judge on usage counters alone
- Hard rule 5: DO NOT reject on basis of distinct triggers

Consolidation strategy: Identify PREFIX CLUSTERS, ask "what UMBRELLA CLASS?", pick one of 3 methods:
  a) MERGE INTO EXISTING UMBRELLA — patch, add labeled section, archive sibling
  b) CREATE NEW UMBRELLA SKILL — skill_manage action=create, archive siblings
  c) DEMOTE TO REFERENCES/TEMPLATES/SCRIPTS — move to umbrella's subdirs

Expected output (EXACT YAML format, curator.py:468-489):
\\\yaml
consolidations:
  - from: <old-skill-name>
    into: <umbrella-skill-name>
    reason: <one sentence>
prunings:
  - name: <skill-name>
    reason: <one sentence>
\\\

### When Nudges Trigger

After every turn completes. Runs in DAEMON THREAD (background, non-blocking):
- Called at: agent/conversation_loop.py:4578-4584
- Function: agent._spawn_background_review(messages_snapshot, review_memory, review_skills)
- Forked agent: tool whitelist (memory + skill only), nudges disabled recursively
- No observable UI — only side effects are disk writes

---

## 2. MEMORY LAYERS (FILE PATHS & SCHEMAS)

### Exact Paths

Built-in stores:
- MEMORY.md: ~/.hermes/memories/MEMORY.md (2200 chars, config: memory.memory_char_limit)
- USER.md: ~/.hermes/memories/USER.md (1375 chars, config: memory.user_char_limit)
- Locks: MEMORY.md.lock, USER.md.lock (fcntl Unix / msvcrt Windows)
- Drift backups: MEMORY.md.bak.<timestamp>, USER.md.bak.<timestamp>

Curator:
- State: ~/.hermes/skills/.curator_state (JSON)
- Reports: ~/.hermes/logs/curator/<YYYYMMDD-HHMMSS>/REPORT.md

External provider:
- Location: ~/.hermes/plugins/memory/<provider_name>/
- Config: config.yaml key memory.provider
- Secrets: ~/.hermes/.env (mode 0600)

### File Formats

Built-in memory files: Markdown with § delimiters (Unicode U+00A7)

ENTRY_DELIMITER = \"\n§\n\" (tools/memory_tool.py:55)

Example MEMORY.md:
\\\
PostgreSQL 16: BETWEEN excludes upper bound, use >= and <=
§
Project uses Go 1.22 + sqlc; migrations in migrations/
§
User prefers direct answers, no verbose explanations
\\\

Curator state JSON:
\\\json
{
  "last_run_at": "2024-05-29T14:15:32.123456+00:00",
  "last_run_duration_seconds": 127,
  "last_run_summary": "Consolidated PR skills into pr-triage umbrella",
  "last_run_summary_shown_at": null,
  "last_report_path": "~/.hermes/logs/curator/20240529-141532/REPORT.md",
  "paused": false,
  "run_count": 5
}
\\\

### Schema

MemoryStore class (tools/memory_tool.py):
- memory_entries: List[str] (live state, mutable)
- user_entries: List[str] (live state, mutable)
- memory_char_limit: int (2200 default)
- user_char_limit: int (1375 default)
- _system_prompt_snapshot: Dict[str, str] (frozen at load, immutable during session)

Methods:
- load_from_disk() — read, dedupe, scan threats, freeze snapshot
- add(target, content) → Dict — append, check limits, persist
- replace(target, old_text, new_content) → Dict — substring match, swap, persist
- remove(target, old_text) → Dict — substring match, delete, persist
- format_for_system_prompt(target) → Optional[str] — return frozen snapshot
- save_to_disk(target) — atomic tempfile + os.replace

---

## 3. CURATION LOGIC

### How Curator Decides WHAT to Write

**Memory nudge**: User guidance in prompt — focus on persona, preferences, expectations about behavior

**Skill nudge**: 113-line detailed prompt with explicit signals:
- User corrected style/tone/format (FIRST-CLASS)
- User corrected workflow/approach
- Non-trivial technique/fix/workaround
- Loaded skill turned out wrong/missing (patch NOW)

Preference order (from prompt):
1. UPDATE CURRENTLY-LOADED SKILL (check /skill-name or skill_view in conversation)
2. UPDATE EXISTING UMBRELLA (skills_list + skill_view search)
3. ADD SUPPORT FILE under umbrella (references/, templates/, scripts/)
4. CREATE NEW CLASS-LEVEL UMBRELLA (name must be class-level, NOT PR#/error/codename/'fix-X')

### Automatic State Transitions (Non-LLM)

Function: curator.apply_automatic_transitions(now) (curator.py:273-314)

Pure state machine (no LLM call):
- ACTIVE → STALE: no activity >= 30 days (config: curator.stale_after_days)
- ACTIVE/STALE → ARCHIVED: no activity >= 90 days (config: curator.archive_after_days)
- STALE → ACTIVE: activity after being stale (automatic reactivation)

Pinned skills: Never transitioned (bypass all auto-transitions)

Constants (curator.py:56-59):
- DEFAULT_STALE_AFTER_DAYS = 30
- DEFAULT_ARCHIVE_AFTER_DAYS = 90
- DEFAULT_INTERVAL_HOURS = 24 * 7 (7 days)
- DEFAULT_MIN_IDLE_HOURS = 2

### Curator Review (LLM-Driven Consolidation)

When: Triggered by maybe_run_curator() when:
- curator.enabled == True (config)
- Not paused
- Last run >= get_interval_hours() ago (default: 168 hours = 7 days)
- Agent idle >= get_min_idle_hours() (default: 2 hours)

Prompt (curator.py:309-543):

"You are running as Hermes' background skill CURATOR. This is an UMBRELLA-BUILDING consolidation pass, not a passive audit..."

"The goal of the skill collection is a LIBRARY OF CLASS-LEVEL INSTRUCTIONS AND EXPERIENTIAL KNOWLEDGE. A collection of hundreds of narrow skills where each one captures one session's specific bug is a FAILURE of the library — not a feature."

Hard rules (non-negotiable):
1. DO NOT touch bundled/hub-installed skills
2. DO NOT delete (archive only)
3. DO NOT touch pinned skills
4. DO NOT judge on usage counters alone (judge on CONTENT)
5. DO NOT reject consolidation for pairwise distinctness

Consolidation workflow:
1. Scan full list, identify PREFIX CLUSTERS
2. For each 2+ member cluster: ask "what UMBRELLA CLASS?"
3. Choose consolidation path (a/b/c above)
4. Emit structured YAML block with consolidations + prunings lists

---

## 4. MEMORY.MD / USER.MD PERSISTENCE

### What Writes Them

memory_tool(action=add) called by background review agent after nudge trigger

### Structure

Entries: Freeform text, delimited by \n§\n

MEMORY.md examples:
- Environment facts ("PostgreSQL 16: BETWEEN excludes upper")
- Project conventions ("Go + sqlc, migrations in migrations/ dir")
- Tool quirks ("Docker Desktop required for docker daemon on Mac")
- Lessons learned ("Nil dereference panics in Go")

USER.md examples:
- Persona ("Works in fintech, prefers Rust, timezone PST")
- Preferences ("Concise output, direct, no verbose explanations")
- Communication style ("Pragmatic, impatient with basics")
- Pet peeves ("Don't explain basic concepts")

### How They're Injected

**Frozen snapshot pattern**:
1. Session start: MemoryStore.load_from_disk() reads MEMORY.md + USER.md
2. Entries scanned for threats (injection/exfil patterns) — strict scope
3. Any threat-matched entry replaced with [BLOCKED: ...] in snapshot only
4. Frozen snapshot captured and immutable during entire session
5. format_for_system_prompt() returns snapshot for injection
6. Mid-session tool calls mutate live memory_entries/user_entries + disk, NOT snapshot
7. Next session: new snapshot from updated disk

Injection point (system_prompt.py:306-311):
\\\python
if agent._memory_store:
    mem_block = agent._memory_store.format_for_system_prompt("memory")
    if mem_block:
        parts["memory"] = mem_block
    
    user_block = agent._memory_store.format_for_system_prompt("user")
    if user_block:
        parts["user"] = user_block
\\\

Rendered format (memory_tool.py:431-449):
\\\
════════════════════════════════════════════════
MEMORY (your personal notes) [50% — 1100/2200 chars]
════════════════════════════════════════════════
Entry 1
§
Entry 2
\\\

No truncation; entire snapshot injected (within char limits).

---

## 5. RETRIEVAL

### Full File Injection at Session Start

Built-in: Complete frozen snapshot injected verbatim, no ranking/search

External provider: Optional prefetch() method called before each turn
- Retrieved context wrapped in <memory-context> fences
- Scrubbed by StreamingContextScrubber during streaming
- NOT injected into system prompt (no prefix cache invalidation)

### On-Session-End

Optional provider hook: on_session_end(messages)
- Called at real boundaries (CLI exit, /reset, gateway timeout)
- NOT called after every turn
- Provider can extract/summarize full conversation

---

## 6. THRESHOLDS (EXACT NUMBERS)

- Memory nudge: 10 turns (agent_init.py:1067)
- Skill nudge: 10 iterations (agent_init.py:1187)
- MEMORY.md char limit: 2200 (agent_init.py:1071)
- USER.md char limit: 1375 (agent_init.py:1072)
- Curator interval: 168 hours = 7 days (curator.py:56)
- Curator min idle: 2 hours (curator.py:57)
- Skill stale: 30 days (curator.py:58)
- Skill archive: 90 days (curator.py:59)

---

## 7. CRITICAL INVARIANTS

1. Frozen snapshot — system prompt never mutates mid-session (prefix cache stable)
2. Immediate atomic writes — memory tool uses tempfile + os.replace for safety
3. Concurrent drift detection — file locking + round-trip check prevents data loss
4. Background isolation — nudge agents tool-whitelisted, nudges disabled recursively
5. Pinned skills never auto-transitioned — curator skips any pinned
6. One external provider max — MemoryManager enforces mutual exclusion
7. Threat scanning on load — poisoned entries replaced with [BLOCKED: ...] in snapshot only
