# komi-learn — Review through the "AI Engineer" lens

*Re-review of the codebase against the obligations of the `engineering-ai-engineer`
persona: bias/fairness, privacy-preserving ML, interpretability/transparency, model
drift, inference latency/cost, MLOps lifecycle, adversarial robustness.*

This is deliberately a **different lens** from the earlier correctness/security review
(`docs` + `test_review_fixes.py`). That pass found bugs; this one asks "is this a
well-behaved *ML system*?" I'm honest below about which persona obligations genuinely
apply to komi-learn and which don't — komi-learn orchestrates LLM calls and ranks
recalled context, it doesn't train models, so some classical-ML concerns map and some
don't.

---

## How each persona obligation maps to komi-learn

| Persona obligation | Maps? | Why |
|---|---|---|
| **Bias / fairness across demographic groups** | ⚠️ Reframed | There are no demographic groups or protected classes here. But there IS a *ranking-fairness* analogue: the recall ranker can develop a **rich-get-richer popularity bias** that buries newer/rarer-but-relevant learnings. That's the real "fairness" issue for this system. |
| **Privacy-preserving techniques** | ✅ Strong | Central to the design — the deterministic safety floor, evidence-stripping, pseudonymous signing. Already a first-class concern. |
| **Interpretability / transparency** | ⚠️ Partial | The curation report + `[community]` labels + human-readable Markdown give good transparency. But the **recall ranker is a black box to the user** — they can't see *why* a learning surfaced. |
| **Model drift detection + retraining triggers** | ⚠️ Missing analogue | No model is trained, so no weight drift. But there's a real analogue: **the learning corpus drifts** (stale facts accrete, the user's preferences change) and nothing surfaces it. |
| **Inference latency / cost** | ❌ Gap | The distiller and judge make real LLM calls with **no cost/latency tracking, no caching on the CLI path**. An ML engineer would never ship inference with zero observability. |
| **MLOps lifecycle / monitoring** | ⚠️ Partial | `doctor`/`status` give some health view; there's no metrics on the loop itself (how many distills, hit rate, cost). |
| **Adversarial robustness** | ✅ Mostly | Recalled pool content is framed as data-not-instructions (good). But there's **no cap/rate-limit on how much untrusted community content floods a single recall**, and no dedup of recalled content. |
| **A/B testing / accuracy metrics** | ❌ Doesn't map | No served model to A/B; "accuracy" of a learning isn't measurable the way a classifier's is. Honestly not applicable — I won't invent it. |

---

## Findings (ordered by real impact)

### 1. Recall popularity-bias feedback loop  *(the "fairness" issue)*
**`komi/engine/recall.py`** — `salience = confidence·(1+reused)`, and recall calls
`_mark_recalled`, and corroboration bumps confidence. The loop:

> a learning surfaces → it's marked recalled / reused → its salience rises → it
> surfaces *more* → newer or rarer-but-relevant learnings are crowded out.

This is the classic recommender popularity-bias trap. Over months, recall ossifies
around a handful of "greatest hits" and stops surfacing fresh knowledge. **Fix:**
dampen the reuse term (log, not linear), and separate "was shown" (weak signal) from
"was actually useful" (strong signal) so merely-surfacing doesn't inflate rank.

### 2. Identity recall is unbounded
**`recall.py`** — *every* active identity learning is injected each session (only
char-truncated at the very end). As the user model grows (it's designed to grow
forever), the identity block bloats the prompt and **older identity facts never age
out**, even when contradicted by newer ones (preference drift). **Fix:** rank + cap
identity like JIT learnings; let recency/confidence decide which persona facts lead.

### 3. No inference cost / latency observability
**`distill.py` / `llm_cli.py`** — the distiller and judge make LLM calls with no
record of tokens, latency, or count. An ML engineer's first instinct: you can't
optimize or trust what you don't measure. **Fix:** record per-pass distill telemetry
(count, duration, candidate count) to state; surface in `status`.

### 4. Distiller candidates not capped or deduped
**`distill.py`** — `_parse_candidates` accepts whatever the model returns; a
misbehaving or prompt-injected model could emit hundreds of "learnings" in one pass,
all written to disk. **Fix:** cap candidates per pass (e.g. 12) and dedup by content.

### 5. No corpus-drift surfacing
Nothing tells the user (or the curator) that the corpus is going stale — e.g. "60% of
your learnings are >90 days old and never reused." **Fix:** compute a cheap drift/health
metric (age + confidence + reuse distribution) and surface it in `doctor`/`curate`.

### 6. Untrusted community content not rate-limited in recall
**`recall.py`** — pool (`scope=global`) learnings are framed as data, but a single
recall could be dominated by community items if they out-rank personal ones. Defense
in depth for a *public, untrusted* source argues for a **cap on community items per
recall** and dedup of recalled content by id. **Fix:** cap community share; dedup.

### 7. Recall opacity (interpretability)
The user can't see why something surfaced. Low priority, but the persona values it:
optionally annotate recalled items with the dominant ranking reason (e.g. "matched
your current files"). Deferred — nice-to-have, not a correctness issue.

---

## What I deliberately did NOT add (honesty over persona-cosplay)

- **Demographic bias testing** — there are no demographic features in this data. Adding
  a "fairness metric across groups" would be theater. The ranking-bias fix (#1) is the
  honest version of the obligation.
- **A/B testing framework** — nothing to A/B; no served model with measurable accuracy.
- **Model retraining triggers** — no model weights. The corpus-drift surfacing (#5) is
  the real analogue.

Applying a persona well means honoring its *intent* where it maps and refusing to fake
it where it doesn't. Fixes #1–#6 are implemented; see `test_ai_eng_fixes` (and the
git history) for the changes.
