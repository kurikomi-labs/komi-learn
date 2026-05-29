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
deterministic mock; the real one is wired in adapters. See docs/02-architecture.md §6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .model import Learning, Scope, Category


# ── Detector library ──────────────────────────────────────────────────────
# Conservative on purpose: false positives (over-redacting to personal) are
# cheap; false negatives (leaking to global) are not. "When in doubt, personal."

_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{16,}\b"),                 # generic API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                            # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                  # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),                # Slack tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),             # PEM private keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*\S+"),
]

_PII_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),     # email
    re.compile(r"\b(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}\b"),  # phone-ish
    re.compile(r"\b\d{1,5}\s+[A-Z][a-z]+\s+(St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive)\b"),
]

_IDENTIFIER_PATTERNS = [
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+"),                   # Windows home path
    re.compile(r"/(?:home|Users)/[^/\s]+"),                        # *nix home path
    re.compile(r"\bhttps?://(?:\d{1,3}\.){3}\d{1,3}\b"),           # private/raw IP URL
    re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.(?:\d{1,3}\.){1,2}\d{1,3}\b"),
    re.compile(r"(?i)\bhttps?://[a-z0-9-]+\.(?:internal|local|corp|intranet)\b"),
]


@dataclass
class FloorResult:
    blocked: bool                       # True = a hard detector fired
    reasons: list[str] = field(default_factory=list)
    secret: bool = False                # secret → reject entirely, not just demote


def safety_floor(text: str, *, project_terms: Optional[list[str]] = None) -> FloorResult:
    """Run all deterministic detectors over *text* (title+body+trigger+tags joined).

    ``project_terms`` are proper nouns (repo/org/dir names from git + cwd) that, if
    present, pin a learning to *project* scope — generally true, but the project name
    in it would deanonymize. They do not block storage, only globalization.
    """
    reasons: list[str] = []
    secret = False
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
    for term in (project_terms or []):
        if term and len(term) >= 3 and re.search(rf"\b{re.escape(term)}\b", text, re.I):
            reasons.append(f"project-term:{term}")
            break
    return FloorResult(blocked=bool(reasons), reasons=reasons, secret=secret)


# ── LLM judgment (pluggable) ──────────────────────────────────────────────

class ScopeJudge(Protocol):
    """Stage-2. Returns a dict: {scope: 'project'|'global', generalized_body: str,
    generalized_title: str, category: str, rationale: str}. Implementations live in
    adapters (real LLM) and tests (mock)."""
    def __call__(self, learning: Learning, *, context: dict) -> dict: ...


@dataclass
class Classification:
    scope: str
    category: str
    reasons: list[str]
    rejected: bool = False              # secret detected → do not store at all
    generalized: Optional[Learning] = None  # rewritten global-ready form, if scope==global


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
    if floor.secret:
        return Classification(scope=Scope.PERSONAL.value, category=learning.category,
                              reasons=floor.reasons, rejected=True)

    # Environment-category learnings are ALWAYS personal (Hermes anti-capture rule:
    # local setup state must never harden into a shared/global constraint).
    if learning.category == Category.ENVIRONMENT.value:
        return Classification(scope=Scope.PERSONAL.value, category=learning.category,
                              reasons=["environment-always-personal"])

    # Identity learnings are about the user → personal by definition.
    if learning.type == "identity":
        return Classification(scope=Scope.PERSONAL.value, category=learning.category,
                              reasons=["identity-is-personal"])

    if floor.blocked:
        # Has identifiers but isn't a secret → can live as project knowledge,
        # but is barred from global. (PII still forces personal.)
        scope = Scope.PERSONAL.value if "pii" in floor.reasons else Scope.PROJECT.value
        return Classification(scope=scope, category=learning.category, reasons=floor.reasons)

    # Stage 2 — survived the floor; ask the judge whether it's truly general.
    if judge is None:
        # No judge available → safe default is project (never auto-global without judgment).
        return Classification(scope=Scope.PROJECT.value, category=learning.category,
                              reasons=["no-judge-default-project"])

    verdict = judge(learning, context=context or {})
    scope = verdict.get("scope", Scope.PROJECT.value)
    category = verdict.get("category", learning.category)

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
            return Classification(scope=Scope.PROJECT.value, category=category,
                                  reasons=["global-rewrite-failed-floor", *recheck.reasons])
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
