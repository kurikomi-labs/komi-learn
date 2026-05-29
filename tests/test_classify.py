"""Classifier: the privacy floor must always win. These are the safety tests."""

from komi.engine.classify import classify, safety_floor, derive_project_terms
from komi.engine.model import Learning, Category, Scope, LearningType


def L(title="t", body="b", trigger="", tags=None, cat=Category.TOOLING.value,
      typ=LearningType.PROCEDURAL.value) -> Learning:
    return Learning(type=typ, category=cat, title=title, body=body,
                    trigger=trigger, tags=tags or []).finalize()


def test_secret_is_rejected_entirely():
    c = classify(L(body="api_key=sk-abc123def456ghi789jklmno"))
    assert c.rejected is True
    assert c.scope == Scope.PERSONAL.value


def test_pii_forced_personal():
    c = classify(L(body="email jane.doe@acme.com for access"))
    assert c.scope == Scope.PERSONAL.value
    assert "pii" in c.reasons


def test_machine_identifier_blocked_from_global():
    c = classify(L(body=r"config at C:\Users\bob\app\conf.yaml"))
    assert c.scope != Scope.GLOBAL.value


def test_project_term_pins_to_project():
    pt = derive_project_terms(r"C:\dev\komi-learn", "git@github.com:kurikomi-labs/komi-learn.git")
    assert "komi-learn" in pt
    c = classify(L(body="in komi-learn run python -m komi", tags=["komi-learn"]), project_terms=pt)
    assert c.scope == Scope.PROJECT.value


def test_environment_always_personal():
    c = classify(L(body="install uv first", cat=Category.ENVIRONMENT.value))
    assert c.scope == Scope.PERSONAL.value


def test_identity_is_personal():
    c = classify(L(body="user likes rust", typ=LearningType.IDENTITY.value))
    assert c.scope == Scope.PERSONAL.value


def test_clean_candidate_globalized_by_judge():
    def judge(lng, context):
        return {"scope": "global", "category": lng.category,
                "generalized_title": "Prefer rg over grep -r",
                "generalized_body": "ripgrep is faster than grep -r and respects .gitignore.",
                "rationale": "general"}
    c = classify(L(title="rg", body="use ripgrep", tags=["ripgrep"]), judge=judge)
    assert c.scope == Scope.GLOBAL.value
    assert c.generalized is not None
    assert c.generalized.id  # finalized


def test_floor_overrides_a_leaky_global_rewrite():
    # The judge says global but leaves a machine path in the rewrite → must downgrade.
    def leaky(lng, context):
        return {"scope": "global", "category": lng.category,
                "generalized_title": "run tests",
                "generalized_body": r"run pytest from C:\Users\bob\proj",
                "rationale": "leaky"}
    c = classify(L(title="pytest", body="run tests", tags=["pytest"]), judge=leaky)
    assert c.scope == Scope.PROJECT.value
    assert "global-rewrite-failed-floor" in c.reasons


def test_no_judge_defaults_to_project_never_global():
    c = classify(L(body="a general approach"))
    assert c.scope == Scope.PROJECT.value
