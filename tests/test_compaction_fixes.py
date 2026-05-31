"""Regression tests for the compaction-review fixes (0.4.0 follow-up):

- hook_compact asserts its own event (no stdin re-routing)
- the emit path is non-fatal (a broken stdout pipe must not wedge the session)
- a compaction re-inject skips the reindex / pool re-mirror (fresh=False)
- the double-injection dedup guard + state.json breadcrumb
- bounded stdin read
- installer matches komi hooks by command shape, not a loose substring
"""

import io
import json
import importlib
from unittest import mock

import pytest

from komi.adapters.claude_code import hook_recall as hr


_BLOCK = "<komi-recall>SAMPLE</komi-recall>"


# ── explicit-event routing (hook_compact knows it's PostCompact) ──────────────

def test_default_event_wins_when_payload_omits_event_name():
    """If the host omits hook_event_name, the entry point's declared event must win
    — a real PostCompact must NOT fall through to the SessionStart JSON format."""
    out = io.StringIO()
    with mock.patch.object(hr, "build_block", lambda c, p, **k: _BLOCK), \
         mock.patch.object(hr, "_compaction_already_served", lambda p, e: False), \
         mock.patch.object(hr, "_record_compaction_served", lambda p, e: None), \
         mock.patch.object(hr, "_read_stdin_json", lambda: {"cwd": "."}), \
         mock.patch("sys.stdout", out):
        rc = hr.main(default_event="PostCompact")
    assert rc == 0
    # PostCompact → plain stdout, NOT json
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.getvalue())
    assert "compacted" in out.getvalue()


def test_payload_event_name_overrides_default():
    """An explicit hook_event_name in the payload still takes precedence."""
    ev, src = hr._classify_event({"hook_event_name": "SessionStart", "source": "startup"},
                                 default_event="PostCompact")
    assert ev == "SessionStart"


def test_hook_compact_entry_passes_postcompact():
    """The hook_compact shim must invoke main with default_event=PostCompact."""
    import komi.adapters.claude_code.hook_compact as hc
    src = __import__("inspect").getsource(hc)
    assert 'default_event="PostCompact"' in src


# ── emit path is non-fatal ────────────────────────────────────────────────────

def test_emit_broken_pipe_does_not_raise():
    """A BrokenPipeError while writing the block must be swallowed (return 0), not
    escape to SystemExit as a traceback."""
    def explode(*a, **k):
        raise BrokenPipeError("host closed the pipe")
    with mock.patch.object(hr, "build_block", lambda c, p, **k: _BLOCK), \
         mock.patch.object(hr, "_compaction_already_served", lambda p, e: False), \
         mock.patch.object(hr, "_record_compaction_served", lambda p, e: None), \
         mock.patch.object(hr, "_emit_block", explode), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "PostCompact"}), \
         mock.patch("sys.stdout", io.StringIO()):
        rc = hr.main()
    assert rc == 0


# ── compaction skips the heavy reindex / pool re-mirror ───────────────────────

def test_compaction_builds_block_with_fresh_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(hr, "build_block",
                        lambda cwd, p, **k: seen.update(k) or _BLOCK)
    monkeypatch.setattr(hr, "_compaction_already_served", lambda p, e: False)
    monkeypatch.setattr(hr, "_record_compaction_served", lambda p, e: None)
    monkeypatch.setattr(hr, "_read_stdin_json",
                        lambda: {"hook_event_name": "PostCompact"})
    with mock.patch("sys.stdout", io.StringIO()):
        hr.main()
    assert seen.get("fresh") is False        # compaction must NOT rebuild the index


def test_session_start_builds_block_with_fresh_true(monkeypatch):
    seen = {}
    monkeypatch.setattr(hr, "build_block",
                        lambda cwd, p, **k: seen.update(k) or _BLOCK)
    monkeypatch.setattr(hr, "_maybe_sync_pool", lambda: None)
    monkeypatch.setattr(hr, "_read_stdin_json",
                        lambda: {"hook_event_name": "SessionStart", "source": "startup"})
    with mock.patch("sys.stdout", io.StringIO()):
        hr.main()
    assert seen.get("fresh") is True


def test_merged_store_fresh_false_skips_reindex(monkeypatch, tmp_path):
    """_merged_store(fresh=False) must not reindex the project or mirror the pool."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths
    importlib.reload(paths)
    importlib.reload(hr)
    mirrored = {"called": False}
    monkeypatch.setattr(hr, "_mirror_pool_into_index",
                        lambda store: mirrored.__setitem__("called", True))
    store = hr._merged_store(str(tmp_path), fresh=False)
    assert mirrored["called"] is False       # no pool re-mirror on compaction
    store.close()


# ── dedup guard + breadcrumb ──────────────────────────────────────────────────

@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths
    importlib.reload(paths)
    importlib.reload(hr)
    return hr


def test_breadcrumb_records_then_sibling_event_dedupes(isolated_state):
    hrl = isolated_state
    payload = {"session_id": "S1"}
    # PostCompact serves it first → records breadcrumb
    hrl._record_compaction_served(payload, "PostCompact")
    # the sibling SessionStart(compact) for the SAME session must now see it served
    assert hrl._compaction_already_served(payload, "SessionStart") is True
    # but the SAME event re-firing is allowed to re-inject (not a sibling dup)
    assert hrl._compaction_already_served(payload, "PostCompact") is False


def test_dedup_is_per_session(isolated_state):
    hrl = isolated_state
    hrl._record_compaction_served({"session_id": "S1"}, "PostCompact")
    # a different session is unaffected
    assert hrl._compaction_already_served({"session_id": "S2"}, "SessionStart") is False


def test_full_flow_second_sibling_event_noops(isolated_state):
    """End-to-end: PostCompact injects + records; the SessionStart(compact) sibling
    for the same session then no-ops (empty JSON), preventing double injection."""
    hrl = isolated_state
    payload_pc = {"hook_event_name": "PostCompact", "session_id": "S9", "cwd": "."}
    payload_ss = {"hook_event_name": "SessionStart", "source": "compact",
                  "session_id": "S9", "cwd": "."}
    out1 = io.StringIO()
    with mock.patch.object(hrl, "build_block", lambda c, p, **k: _BLOCK), \
         mock.patch.object(hrl, "_read_stdin_json", lambda: payload_pc), \
         mock.patch("sys.stdout", out1):
        hrl.main()
    assert _BLOCK in out1.getvalue()            # first one injects

    out2 = io.StringIO()
    with mock.patch.object(hrl, "build_block", lambda c, p, **k: _BLOCK), \
         mock.patch.object(hrl, "_read_stdin_json", lambda: payload_ss), \
         mock.patch("sys.stdout", out2):
        hrl.main()
    obj = json.loads(out2.getvalue())           # second one no-ops to empty JSON
    assert "hookSpecificOutput" not in obj
    assert "_note" in obj


# ── bounded stdin ─────────────────────────────────────────────────────────────

def test_oversized_stdin_is_safe(monkeypatch):
    huge = "x" * (hr._MAX_STDIN_BYTES + 100)
    monkeypatch.setattr(hr.sys, "stdin", io.StringIO(huge))
    assert hr._read_stdin_json() == {}          # over cap → safe empty dict


def test_normal_stdin_parses(monkeypatch):
    monkeypatch.setattr(hr.sys, "stdin", io.StringIO('{"hook_event_name":"SessionStart"}'))
    assert hr._read_stdin_json()["hook_event_name"] == "SessionStart"


# ── installer matches by command shape, not loose substring ───────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths, setup
    importlib.reload(paths)
    importlib.reload(setup)
    return setup


def test_is_komi_command_precise(home):
    setup = home
    assert setup._is_komi_command('/usr/bin/python -m komi.adapters.claude_code.hook_recall')
    assert setup._is_komi_command('"C:\\py.exe" -m komi.adapters.claude_code.hook_compact --compact')
    # a user hook that merely MENTIONS the module path is NOT a komi command
    assert not setup._is_komi_command('python wrapper.py --note "see komi.adapters.claude_code docs"')
    assert not setup._is_komi_command('echo komi.adapters.claude_code')


def test_install_does_not_clobber_lookalike_user_hook(home):
    setup = home
    sp = setup.settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    # a user hook whose command contains the marker substring but isn't a komi hook
    user_cmd = 'python my_wrapper.py --doc "komi.adapters.claude_code"'
    sp.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": user_cmd}]}]}}), encoding="utf-8")
    setup._install_hooks()
    data = json.loads(sp.read_text(encoding="utf-8"))
    cmds = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert user_cmd in cmds                      # untouched
    assert any("hook_recall" in c for c in cmds) # komi added alongside, not over it


def test_uninstall_keeps_lookalike_user_hook(home):
    setup = home
    setup._install_hooks()
    sp = setup.settings_path()
    data = json.loads(sp.read_text(encoding="utf-8"))
    user_cmd = 'python my_wrapper.py --doc "komi.adapters.claude_code"'
    data["hooks"]["SessionStart"].append(
        {"hooks": [{"type": "command", "command": user_cmd}]})
    sp.write_text(json.dumps(data), encoding="utf-8")
    setup.uninstall(keep_data=True)
    data = json.loads(sp.read_text(encoding="utf-8"))
    cmds = [h["command"] for e in data.get("hooks", {}).get("SessionStart", [])
            for h in e["hooks"]]
    assert user_cmd in cmds                      # user hook survived uninstall
    assert not any("hook_recall" in c for c in cmds)  # komi's was removed


# ── capture toggle ────────────────────────────────────────────────────────────

def test_capture_on_off_repoints_hooks(home):
    setup = home
    setup._install_hooks()
    r = setup.set_capture(True)
    assert r.ok
    data = json.loads(setup.settings_path().read_text(encoding="utf-8"))
    ss = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    pc = [h["command"] for e in data["hooks"]["PostCompact"] for h in e["hooks"]]
    assert any("hook_capture" in c for c in ss)
    assert any("hook_capture" in c and "--compact" in c for c in pc)
    # off restores
    setup.set_capture(False)
    data = json.loads(setup.settings_path().read_text(encoding="utf-8"))
    ss = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    pc = [h["command"] for e in data["hooks"]["PostCompact"] for h in e["hooks"]]
    assert any("hook_recall" in c for c in ss)
    assert any("hook_compact" in c for c in pc)
    assert not any("hook_capture" in c for c in ss + pc)
