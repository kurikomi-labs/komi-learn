"""komi-learn — the Classifier (HYBRID: deterministic floor → LLM judgment).

Decides the *scope* of a distilled learning: ``personal`` / ``project`` / ``global``.
This is the "used with thought; some knowledge is global" logic, and it is the
single most safety-critical component, because a wrong "global" can leak private
data into a public pool. So the design is defense-in-depth:

  STAGE 0  SECRETS         any credential/key/token  → REJECT outright (never stored
                            even personally in cleartext; the distiller should not
                            have surfaced it, but we backstop).
  STAGE 1  IDENTIFIER FLOOR  deterministic detectors for PII + machine/project
                            identifiers. Anything matching can NEVER be global —
                            it is forced down to project or personal. The LLM
                            cannot reason around this floor.
  STAGE 2  LLM JUDGMENT     only on what survives the floor: is this generally
                            true and useful to anyone, or project-specific? The
                            LLM also returns a *generalization rewrite* that strips
                            residual specificity from a global candidate.

The LLM is injected as a callable so the engine runs (and tests pass) with a
deterministic mock; the real one is wired in adapters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .model import Learning, Scope, Category, Visibility


# ── Detector library ──────────────────────────────────────────────────────
# Conservative on purpose: false positives (over-redacting to personal) are
# cheap; false negatives (leaking to global) are not. "When in doubt, personal."

# NOTE: this detector set is mirrored verbatim in the pool repo's CI verifier
# (pool-repo-template/.github/scripts/verify.py). A parity test asserts they match.
# When in doubt the floor over-rejects (to personal) — false positives are cheap,
# false negatives leak. Each entry names what it catches.
# Quantifiers are UPPER-bounded (not open `{n,}`) as defense-in-depth: it keeps
# matches cheap on pathological input and is good hygiene. (Measured ReDoS on the
# prior open forms was negligible, and safety_floor also caps input length below —
# this is belt-and-suspenders, not a fix for a live ReDoS.)
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,120}\b"),  # OpenAI/Stripe sk-/sk_/sk_live_/rk_live_/pk_
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                            # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),                            # AWS temp access key
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,80}\b"),                  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{10,400}"),                 # Google OAuth access token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,120}\b"),              # GitHub classic tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_\-]{20,120}\b"),          # GitHub fine-grained PAT (allow hyphens)
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,120}\b"),               # GitLab PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,120}\b"),            # Slack tokens
    re.compile(r"\bxapp-[0-9]+-[A-Za-z0-9-]{10,120}\b"),           # Slack app token
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,80}\.[A-Za-z0-9_\-]{16,80}\b"),  # SendGrid
    re.compile(r"\bnpm_[A-Za-z0-9]{30,120}\b"),                    # npm token
    re.compile(r"\bdop_v1_[a-f0-9]{32,120}\b"),                    # DigitalOcean token
    re.compile(r"\bAC[a-f0-9]{32}\b"),                             # Twilio Account SID
    re.compile(r"\bhf_[A-Za-z0-9]{20,120}\b"),                     # HuggingFace token
    re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"),     # PEM private keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,400}\.[A-Za-z0-9_-]{8,400}\.[A-Za-z0-9_-]{8,400}\b"),  # JWT
    # connection strings carrying a password: scheme://user:pass@host(/path?query)
    re.compile(r"\b[a-z][a-z0-9+.\-]{0,20}://[^\s:/@]{1,100}:[^\s:/@]{1,100}@[^\s]{1,200}", re.I),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token|bearer|client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"]{6,120}"),
]

_PII_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,100}\.[A-Za-z]{2,10}\b"),     # email
    re.compile(r"\b(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,5}\d{2,4}\b"),  # phone-ish (intl-tolerant)
    re.compile(r"\b\d{1,5}\s+[A-Z][a-z]{1,20}\s+(St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive)\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                           # US SSN
    re.compile(r"\b(?:\d[ -]{0,2}){13,16}\b"),                     # credit-card-ish (13-16 digits)
]

# Business-CONFIDENTIAL content. These carry no secret/PII *pattern* (so the
# secret/PII/identifier floors never catch them), but they must never be committed
# to a repo or pooled: equity/cap tables, fundraising, revenue, comp, internal
# strategy, M&A. A match forces visibility=private (local-only) and bars global.
# Conservative-by-design: a false positive only makes a sharable lesson local
# (cheap); a false negative leaks confidential business data (expensive). Word-
# boundaried + case-insensitive; this is a topic floor, not a credential scanner.
#
# Tuning note: this floor is a COARSE deterministic net, paired with the LLM judge
# (the real recall lever for paraphrase) and a fail-safe-private default when no
# judge ran. Patterns are ANCHORED to financial/equity context — bare words like
# "shares", "vesting", "valuation", "series a", "merger", "runway", "compensation"
# collide with ordinary engineering prose (DB shares, cache vesting, expression
# valuation, time-series A, git merge, animation runway, latency compensation), so
# matching them unqualified would gut the committable file. Precision matters as
# much as recall here.
_CONFIDENTIAL_PATTERNS = [
    re.compile(r"(?i)\bcap[\s-]?table\b"),
    # equity SHARES — require an equity qualifier so "4 shares of the cache" / a
    # GraphQL "preferred shares" field don't match.
    re.compile(r"(?i)\b(?:authorized|unissued|outstanding|founder'?s?|treasury|vested|unvested)\s+shares?\b"),
    re.compile(r"(?i)\bshares?\s+(?:issued|outstanding|authorized|vested|granted\s+to)\b"),
    re.compile(r"(?i)\bshares?\s+of\s+(?:common|preferred)\s+stock\b"),
    re.compile(r"(?i)\b\d[\d,.]*\s*(?:k|m|mm|million|billion)?\s+shares?\s+(?:to\s+the\s+founder|issued|authorized|outstanding|vested|granted|of\s+(?:common|preferred)\b)"),
    re.compile(r"(?i)\b(?:option\s+pool|stock\s+option\s+(?:plan|grant)|\bRSUs?\b|equity\s+(?:grant|stake|compensation|package)|vesting\s+(?:schedule|cliff)|vesting\s+period\s+for\s+(?:shares|equity|options))\b"),
    re.compile(r"(?i)\b(?:par\s+value\s+(?:of\s+)?\$|fully[\s-]?diluted|pre[\s-]?money|post[\s-]?money)\b"),
    re.compile(r"(?i)\b(?:company|business|startup|pre[\s-]?money|post[\s-]?money)\s+valuation\b|\bvaluation\s+(?:of\s+\$|cap\b)"),
    # equity-as-percentage / ownership — the most common way ownership is stated.
    re.compile(r"(?i)\b\d{1,3}\s?%\s+(?:of\s+the\s+company|equity|stake|ownership|fully[\s-]?diluted)\b"),
    re.compile(r"(?i)\b(?:owns?|holds?|keeps?|retains?|gets?|receives?)\s+\d{1,3}\s?%\s+(?:of\s+the\s+company|equity|stake|ownership)\b"),
    re.compile(r"(?i)\bfounder\s+(?:owns?|holds?|keeps?|retains?|gets?)\b"),
    # fundraising — "series [a-d]" anchored to funding context (not time-series A).
    re.compile(r"(?i)\b(?:fundrais\w+|seed\s+round|series\s+[a-d]\s+(?:round|funding|financing|investment)|term\s+sheet|convertible\s+note\s+(?:at|for|with|of|round)|angel\s+investor|venture\s+capital|cap\s+raise|liquidation\s+preference|friends\s+and\s+family\s+(?:round|raise)|409a)\b"),
    re.compile(r"\bSAFE\s+(?:note|round|financing|agreement)\b"),   # all-caps SAFE acronym (not "safe note about X")
    # revenue/financials — revenue/ARR/MRR are business-confidential in this context.
    re.compile(r"\b(?:ARR|MRR)\b"),   # case-SENSITIVE: the acronyms are uppercase; lowercase `arr` is a variable name
    re.compile(r"(?i)\b(?:monthly\s+recurring\s+revenue|annual\s+recurring\s+revenue|net\s+revenue|gross\s+revenue|revenue\s+(?:was|is|target|projection)|revenue\s+of\s+(?:\$|\d|about|around|roughly)|in\s+revenue|profit\s+(?:was|of|last)|net\s+income|gross\s+margins?\s+(?:are|were)\s+\d|profit\s+margins?\s+(?:are|were)\s+\d)\b"),
    re.compile(r"(?i)\b(?:cash\s+runway|months?\s+of\s+(?:cash|runway)|monthly\s+burn|we'?re?\s+burning\s+(?:\$|\d|cash|money)|we\s+burn\s+(?:\$|\d|cash)|burn\s+rate\s+(?:of|is)\s+\$?\d)"),
    # compensation — qualified so "compensate for latency" / saga "compensation" miss.
    re.compile(r"(?i)\b(?:employee|executive|founder|engineer|hire)\s+(?:salary|salaries|compensation|comp\b)|(?:salary|comp)\s+(?:band|package|range)|equity\s+compensation|base\s+salary\s+of\s+\$?\d|\$\d[\d,]*\s*(?:k|/yr|/year|base)\b"),
    # M&A / exit — plain words too ("Google approached us about buying the company").
    re.compile(r"(?i)\b(?:acquisition\s+(?:offer|target|talks)|merger\s+(?:&|and|agreement|with)|merger\s+and\s+acquisition|due\s+diligence\s+(?:on\s+(?:the\s+)?(?:company|acquisition|deal)|process)|(?:acqui\w+|buy\w*|purchas\w+|sell\w*|sold)\s+(?:the\s+|our\s+|us\b)?(?:company|startup|business)|\bexit\s+(?:strategy|valuation)\b|exit\s+the\s+company)\b"),
    re.compile(r"(?i)\b(?:moat\s+(?:vs|against)|competitive\s+moat|unreleased\s+(?:roadmap|product)|trade\s+secret|under\s+NDA|business[\s-]confidential)\b"),
    re.compile(r"(?i)\b(?:Stripe\s+Atlas|\bCarta\b|\bPulley\b|Delaware\s+C-?Corp|incorporation\s+defaults?)\b"),
]

_IDENTIFIER_PATTERNS = [
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]{1,200}"),            # Windows home path
    re.compile(r"/(?:home|Users)/[^/\s]{1,200}"),                 # *nix / macOS home path
    re.compile(r"/root/[^/\s]{1,200}"),                           # root home
    re.compile(r"\bhttps?://(?:\d{1,3}\.){3}\d{1,3}\b"),           # raw IPv4 URL
    re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.(?:\d{1,3}\.){1,2}\d{1,3}\b"),  # private IPv4
    re.compile(r"\bhttps?://\[[0-9a-fA-F:]{1,100}\]"),             # IPv6 URL
    re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{0,4}\b"),  # bare IPv6
    re.compile(r"(?i)\bhttps?://[a-z0-9-]{1,100}\.(?:internal|local|corp|intranet|lan)\b"),
    re.compile(r"(?i)\b[a-z0-9-]{1,100}\.onion\b"),                # tor hidden service
]


@dataclass
class FloorResult:
    blocked: bool                       # True = a hard detector fired
    reasons: list[str] = field(default_factory=list)
    secret: bool = False                # secret → reject entirely, not just demote
    confidential: bool = False          # business-confidential → force private + bar global


def safety_floor(text: str, *, project_terms: Optional[list[str]] = None) -> FloorResult:
    """Run all deterministic detectors over *text* (title+body+trigger+tags joined).

    ``project_terms`` are proper nouns (repo/org/dir names from git + cwd) that, if
    present, pin a learning to *project* scope — generally true, but the project name
    in it would deanonymize. They do not block storage, only globalization.
    """
    # Bound the scanned length: a learning is short prose, and capping the input
    # is the simplest robust guard against any pathological-input slowdown.
    if text and len(text) > 20000:
        text = text[:20000]
    reasons: list[str] = []
    secret = False
    confidential = False
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            reasons.append("secret/credential")
            secret = True
            break
    for pat in _PII_PATTERNS:
        if pat.search(text):
            reasons.append("pii")
            break
    for pat in _IDENTIFIER_PATTERNS:
        if pat.search(text):
            reasons.append("machine-identifier")
            break
    for pat in _CONFIDENTIAL_PATTERNS:
        if pat.search(text):
            reasons.append("business-confidential")
            confidential = True
            break
    for term in (project_terms or []):
        if term and len(term) >= 3 and re.search(rf"\b{re.escape(term)}\b", text, re.I):
            reasons.append(f"project-term:{term}")
            break
    return FloorResult(blocked=bool(reasons), reasons=reasons, secret=secret,
                       confidential=confidential)


# ── LLM judgment (pluggable) ──────────────────────────────────────────────

class ScopeJudge(Protocol):
    """Stage-2. Returns a dict: {scope: 'personal'|'project'|'global', visibility:
    'shareable'|'private', generalized_body: str, generalized_title: str, category:
    str, rationale: str}. ``visibility`` lets the LLM catch confidential content the
    regex floor misses (paraphrased financials/strategy) — 'private' forces local-
    only storage and bars the pool. Implementations live in adapters (real LLM) and
    tests (mock)."""
    def __call__(self, learning: Learning, *, context: dict) -> dict: ...


@dataclass
class Classification:
    scope: str
    category: str
    reasons: list[str]
    rejected: bool = False              # secret detected → do not store at all
    generalized: Optional[Learning] = None  # rewritten global-ready form, if scope==global
    visibility: str = Visibility.SHAREABLE.value  # shareable | private (private bars global + commit)
    # True ONLY for content the deterministic confidential floor flagged (cap table, KYC,
    # financials). Distinct from visibility=private (which also covers merely-unvetted
    # content). Recall quarantines on THIS, so unvetted-but-harmless craft still surfaces.
    confidential: bool = False


def _personal_visibility(learning: "Learning", judge, context) -> str:
    """Visibility for an always-personal learning (identity/environment) whose regex
    floor came back clean. Mirrors the main pipeline's fail-safe: ask the judge if
    one exists (private if it flags private OR abstains — unvetted), else with no
    judge fail safe to private. Only an explicit shareable judgment yields shareable."""
    if judge is None:
        return Visibility.PRIVATE.value          # unvetted local content stays in .local
    try:
        verdict = judge(learning, context=context or {})
    except Exception:
        return Visibility.PRIVATE.value
    return (Visibility.SHAREABLE.value
            if verdict.get("visibility") == Visibility.SHAREABLE.value
            else Visibility.PRIVATE.value)


def classify(
    learning: Learning,
    *,
    project_terms: Optional[list[str]] = None,
    judge: Optional[ScopeJudge] = None,
    context: Optional[dict] = None,
) -> Classification:
    """Full hybrid pipeline. Pure + deterministic given a fixed ``judge``."""
    joined = " \n ".join([
        learning.title or "", learning.body or "", learning.trigger or "",
        " ".join(learning.tags or []),
    ])

    # Stage 0/1 — the floor.
    floor = safety_floor(joined, project_terms=project_terms)
    # Confidential content (cap tables, fundraising, revenue, strategy) is marked
    # PRIVATE no matter what scope it lands in, so it routes to local-only storage
    # and is barred from the pool. Computed once; stamped on every outcome below.
    vis = Visibility.PRIVATE.value if floor.confidential else Visibility.SHAREABLE.value
    # `conf` tracks the DETERMINISTIC confidential signal specifically (not bare
    # visibility=private). Recall quarantines on this — so fail-safe-private content
    # that is merely unvetted (not floor-flagged) still surfaces to the user's agent.
    conf = floor.confidential

    if floor.secret:
        return Classification(scope=Scope.PERSONAL.value, category=learning.category,
                              reasons=floor.reasons, rejected=True, visibility=vis,
                              confidential=conf)

    # Environment-category and identity learnings are ALWAYS personal — but personal
    # scope does NOT mean "safe to commit": a user's ~/.claude (or a project's
    # .claude/komi) can be version-controlled, and identity/env is the most likely
    # place for casual business disclosure ("I'm the CTO of <stealth co>", "set
    # STEALTH=1 until launch"). So they get the SAME fail-safe + judge vetting as
    # every other local path: confidential floor → private; else if a model can vet
    # it, ask; else (no judge) fail safe to private rather than committing unvetted.
    if (learning.category == Category.ENVIRONMENT.value or learning.type == "identity"):
        reason = ("environment-always-personal" if learning.category == Category.ENVIRONMENT.value
                  else "identity-is-personal")
        pvis = vis
        if not floor.confidential:
            pvis = _personal_visibility(learning, judge, context)
        return Classification(scope=Scope.PERSONAL.value, category=learning.category,
                              reasons=[reason], visibility=pvis, confidential=conf)

    if floor.blocked:
        # Has identifiers/confidential content but isn't a secret → can live as
        # project knowledge, but is barred from global. (PII still forces personal;
        # confidential forces private storage via ``vis``.)
        scope = Scope.PERSONAL.value if "pii" in floor.reasons else Scope.PROJECT.value
        return Classification(scope=scope, category=learning.category,
                              reasons=floor.reasons, visibility=vis, confidential=conf)

    # Stage 2 — survived the floor; ask the judge whether it's truly general.
    if judge is None:
        # No judge available → project scope (never auto-global without judgment),
        # and FAIL SAFE on visibility: with no model to vet for confidentiality and
        # a regex floor that misses paraphrased financials/strategy, defaulting to
        # shareable would commit (and could pool) unvetted content. Over-classifying
        # to private (stays in .local, never committed) is the cheap error; leaking a
        # cap table is not. The user gets committable craft once a model is configured.
        return Classification(scope=Scope.PROJECT.value, category=learning.category,
                              reasons=["no-judge-fail-safe-private"],
                              visibility=Visibility.PRIVATE.value)

    verdict = judge(learning, context=context or {})
    scope = verdict.get("scope", Scope.PROJECT.value)
    category = verdict.get("category", learning.category)
    judged_vis = verdict.get("visibility")

    # The LLM flagged private → force private + bar global (second line of defense
    # for paraphrased confidential content the regex floor can't pattern-match).
    if judged_vis == Visibility.PRIVATE.value:
        keep = Scope.PERSONAL.value if scope == Scope.PERSONAL.value else Scope.PROJECT.value
        return Classification(scope=keep, category=category,
                              reasons=[verdict.get("rationale", "llm-private")],
                              visibility=Visibility.PRIVATE.value)

    # FAIL SAFE: a verdict that keeps the learning LOCAL (project/personal) but does
    # NOT explicitly judge it shareable is treated as unvetted → private. This is the
    # NullLLM / abstaining-judge path: with no real confidentiality judgment and a
    # regex floor that misses paraphrased financials, defaulting to shareable would
    # commit unvetted content. A judge asserting global IS an explicit shareability
    # judgment (global is impossible while private), so it is exempt and proceeds to
    # the global path below. Over-classify-to-private is cheap; leaking is not.
    if scope != Scope.GLOBAL.value and judged_vis != Visibility.SHAREABLE.value:
        return Classification(scope=(Scope.PERSONAL.value if scope == Scope.PERSONAL.value
                                     else Scope.PROJECT.value),
                              category=category,
                              reasons=["unvetted-visibility-fail-safe-private"],
                              visibility=Visibility.PRIVATE.value)

    if scope == Scope.GLOBAL.value:
        gen = Learning.from_dict(learning.to_dict())
        gen.title = (verdict.get("generalized_title") or learning.title).strip()
        gen.body = (verdict.get("generalized_body") or learning.body).strip()
        gen.category = category
        gen.scope = Scope.GLOBAL.value
        # CRITICAL: re-run the floor on the *rewritten* text. If the LLM left any
        # identifier in, we refuse to globalize it. The floor always wins.
        recheck = safety_floor(
            " \n ".join([gen.title, gen.body, gen.trigger, " ".join(gen.tags)]),
            project_terms=project_terms,
        )
        if recheck.blocked:
            # The floor always wins: if the rewrite tripped the CONFIDENTIAL floor,
            # that's a strong signal the topic is confidential → private, not the
            # shareable default. (Identifier-only blocks stay shareable+project.)
            return Classification(scope=Scope.PROJECT.value, category=category,
                                  reasons=["global-rewrite-failed-floor", *recheck.reasons],
                                  visibility=(Visibility.PRIVATE.value if recheck.confidential else vis))
        gen.finalize()
        return Classification(scope=Scope.GLOBAL.value, category=category,
                              reasons=[verdict.get("rationale", "llm-global")],
                              generalized=gen)

    return Classification(scope=scope, category=category,
                          reasons=[verdict.get("rationale", "llm-project")])


def derive_project_terms(cwd: str, git_remote: str = "") -> list[str]:
    """Extract proper nouns (dir name, repo, org) that should pin scope to project.
    Cheap + deterministic; the distiller passes these in."""
    import os
    terms: set[str] = set()
    base = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    if base:
        terms.add(base)
    # parse owner/repo from a git remote like git@github.com:org/repo.git or https URL
    m = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", git_remote or "")
    if m:
        terms.add(m.group(1))
        terms.add(m.group(2))
    # drop generic words that would over-match
    return [t for t in terms if t.lower() not in {"src", "app", "main", "code", "tmp", "repo"}]


__all__ = [
    "safety_floor", "FloorResult", "classify", "Classification",
    "ScopeJudge", "derive_project_terms",
]
