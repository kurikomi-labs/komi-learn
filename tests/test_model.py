"""Content-addressing + serialization invariants."""

from komi.engine.model import Learning, LearningType, Category, Scope, verify_id


def make(**kw) -> Learning:
    base = dict(type=LearningType.PROCEDURAL.value, category=Category.TOOLING.value,
                title="t", body="b", trigger="when x", tags=["a", "b"])
    base.update(kw)
    return Learning(**base).finalize()


def test_id_is_deterministic_over_content():
    a = make(tags=["A", "b"], confidence=0.3)
    b = make(tags=["b", "a"], confidence=0.9)  # diff tag order/case + bookkeeping
    assert a.id == b.id
    assert a.id.split(":")[0] in {"blake3", "blake2b"}


def test_id_changes_with_content():
    a = make(body="one")
    b = make(body="two")
    assert a.id != b.id


def test_verify_id_roundtrip_and_tamper():
    a = make()
    assert verify_id(a.publishable()) is True
    pub = a.publishable()
    pub["body"] = "tampered"
    assert verify_id(pub) is False


def test_publishable_strips_local_provenance():
    a = make()
    a.evidence.session_id = "secret-session"
    pub = a.publishable()
    assert "evidence" not in pub
    assert "usage" not in pub
    assert "lifecycle" not in pub
    assert set(pub) >= {"id", "schema", "type", "category", "title", "body", "trigger", "tags", "provenance"}


def test_from_dict_tolerates_extra_and_missing_keys():
    d = make().to_dict()
    d["unknown_future_field"] = 123
    del d["usage"]
    lng = Learning.from_dict(d)
    assert lng.title == "t"
    assert lng.usage.recalled == 0  # rebuilt default
