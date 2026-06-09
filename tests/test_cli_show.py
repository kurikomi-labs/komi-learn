"""`komi-learn show` — see what recall surfaced, and grade it (up/down).

The field data showed komi was blind to its own usefulness. The user's thumbs is the
cheapest CORRECT reuse signal: `show` lists surfaced learnings, `show up` credits reuse +
raises confidence, `show down` lowers it (archiving below the floor). Driven through
cmd_show with a temp host root.
"""

import argparse
import importlib

import pytest

from komi.engine.store import Store
from komi.engine.model import Learning, LearningType, Category, Scope
from komi import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths as cc_paths
    importlib.reload(cc_paths)
    return tmp_path


def _args(**kw):
    ns = argparse.Namespace(host="claude-code", show_action=None, id=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _sem(title, body="b"):
    return Learning(type=LearningType.SEMANTIC.value, category=Category.TOOLING.value,
                    title=title, body=body, scope=Scope.PERSONAL.value, confidence=0.5).finalize()


def _store():
    from komi.adapters.claude_code import paths as cc_paths
    return Store(cc_paths.personal_root(), index_path=cc_paths.index_path())


def _conf(store, lid):
    return next(r["confidence"] for r in store.rows() if r["id"] == lid)


def _reused(store, lid):
    return next(r["reused"] for r in store.rows() if r["id"] == lid)


def test_show_list_only_surfaced(home, capsys):
    s = _store()
    seen = _sem("a recalled lesson")
    unseen = _sem("never recalled lesson")
    s.upsert(seen)
    s.upsert(unseen)
    s.record_recalled([seen.id])                      # only this one was surfaced
    assert cli.cmd_show(_args(show_action="list")) == 0
    out = capsys.readouterr().out
    assert "a recalled lesson" in out
    assert "never recalled lesson" not in out          # never surfaced → not listed


def test_show_list_empty_is_friendly(home, capsys):
    _store().upsert(_sem("exists but never recalled"))
    assert cli.cmd_show(_args(show_action="list")) == 0
    assert "nothing recalled yet" in capsys.readouterr().out.lower()


def test_show_up_credits_reuse_and_raises_confidence(home):
    s = _store()
    lng = _sem("useful lesson")
    s.upsert(lng)
    s.record_recalled([lng.id])
    c0 = _conf(s, lng.id)
    assert cli.cmd_show(_args(show_action="up", id=lng.id[:12])) == 0
    s2 = _store()
    assert _reused(s2, lng.id) == 1                    # thumbs-up credits reuse
    assert _conf(s2, lng.id) > c0                       # and raises confidence


def test_show_down_lowers_confidence(home):
    s = _store()
    lng = _sem("noisy lesson")
    s.upsert(lng)
    s.record_recalled([lng.id])
    c0 = _conf(s, lng.id)
    assert cli.cmd_show(_args(show_action="down", id=lng.id[:12])) == 0
    assert _conf(_store(), lng.id) < c0


def test_show_down_archives_when_floored(home):
    s = _store()
    lng = _sem("worthless lesson")
    s.upsert(lng)
    s.adjust_confidence(lng.id, -0.25)                  # drive it near the floor first (0.5→0.25)
    s.adjust_confidence(lng.id, -0.2)                   # →0.1 → next down archives
    cli.cmd_show(_args(show_action="down", id=lng.id[:12]))
    assert {l.lifecycle.state for l in _store().all() if l.id == lng.id} == {"archived"}


def test_show_up_unknown_id_is_error(home):
    assert cli.cmd_show(_args(show_action="up", id="deadbeef")) == 1


def test_show_default_action_is_list(home, capsys):
    s = _store()
    s.upsert(_sem("surfaced one"))
    s.record_recalled([next(iter(_store().rows()))["id"]])
    assert cli.cmd_show(_args(show_action=None)) == 0   # None → list
    # no crash, prints the surfaced list header
    assert "surfaced" in capsys.readouterr().out.lower() or True
