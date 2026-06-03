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
from komi.adapters import hooklib


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
    ev, src = hooklib.classify_event({"hook_event_name": "SessionStart", "source": "startup"},
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
    """build_recall_block(fresh=False) must NOT re-mirror the pool (the compaction
    perf path now lives in hooklib; the Claude Code shim threads fresh through)."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths
    importlib.reload(paths)
    mirrored = {"called": False}
    monkeypatch.setattr(hooklib, "_mirror_pool",
                        lambda pm, store: mirrored.__setitem__("called", True))
    hooklib.build_recall_block(paths, cwd=str(tmp_path), fresh=False)
    assert mirrored["called"] is False       # no pool re-mirror on compaction
    # and fresh=True DOES mirror
    hooklib.build_recall_block(paths, cwd=str(tmp_path), fresh=True)
    assert mirrored["called"] is True


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
    huge = "x" * (hooklib._MAX_STDIN_BYTES + 100)
    monkeypatch.setattr(hooklib.sys, "stdin", io.StringIO(huge))
    assert hooklib.read_stdin_json() == {}          # over cap → safe empty dict


def test_normal_stdin_parses(monkeypatch):
    monkeypatch.setattr(hooklib.sys, "stdin", io.StringIO('{"hook_event_name":"SessionStart"}'))
    assert hooklib.read_stdin_json()["hook_event_name"] == "SessionStart"


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

# ── update verifies the AGENT's hook interpreter, not just the CLI ────────────

def test_interpreter_from_command_quoted_and_plain(home):
    setup = home
    assert setup._interpreter_from_command(
        '"C:\\Program Files\\Python\\python.exe" -m komi.adapters.claude_code.hook_recall') \
        == "C:\\Program Files\\Python\\python.exe"
    assert setup._interpreter_from_command(
        '/usr/bin/python3 -m komi.adapters.claude_code.hook_compact --compact') == "/usr/bin/python3"
    assert setup._interpreter_from_command("") is None


def test_hook_interpreters_extracts_pinned_python(home):
    setup = home
    setup._install_hooks()
    interps = setup.hook_interpreters()
    # the installer pins sys.executable; that's the one the agent runs under
    assert len(interps) == 1
    import os, sys
    # the extracted interpreter must actually equal sys.executable (quotes stripped,
    # case-normalized on Windows) — NOT just be non-empty
    assert os.path.normcase(interps[0].strip('"')) == os.path.normcase(sys.executable)


def test_hook_interpreters_empty_without_install(home):
    setup = home
    assert setup.hook_interpreters() == []


def test_verify_agent_updated_match(home, monkeypatch, capsys):
    from komi import cli, updater
    import komi.adapters.claude_code.setup as setup_mod
    monkeypatch.setattr(setup_mod, "hook_interpreters", lambda: ["/usr/bin/python3"])
    monkeypatch.setattr(updater, "installed_version_via_subprocess",
                        lambda python=None: "0.4.0")
    cli._verify_agent_updated("0.4.0")
    out = capsys.readouterr().out
    assert "agent behavior updated" in out
    assert "0.4.0" in out


def test_verify_agent_updated_detects_stale_other_python(home, monkeypatch, capsys):
    """The critical case: hooks pinned to a DIFFERENT Python that still imports the
    old version — update must loudly say the agent is NOT updated + give the fix."""
    from komi import cli, updater
    import komi.adapters.claude_code.setup as setup_mod
    monkeypatch.setattr(setup_mod, "hook_interpreters", lambda: ["/other/venv/python"])
    # that interpreter reports the OLD version
    monkeypatch.setattr(updater, "installed_version_via_subprocess",
                        lambda python=None: "0.3.0" if python == "/other/venv/python" else "0.4.0")
    cli._verify_agent_updated("0.4.0")
    out = capsys.readouterr().out
    assert "DIFFERENT Python" in out
    assert "/other/venv/python" in out
    assert "pip install --upgrade" in out          # exact fix printed


def test_verify_agent_updated_no_hooks(home, monkeypatch, capsys):
    from komi import cli
    import komi.adapters.claude_code.setup as setup_mod
    monkeypatch.setattr(setup_mod, "hook_interpreters", lambda: [])
    cli._verify_agent_updated("0.4.0")
    out = capsys.readouterr().out
    assert "no Claude Code hooks" in out


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


# ── ultrareview fixes ─────────────────────────────────────────────────────────

def test_emit_suppressed_on_postcompact():
    """_emit must write NOTHING for PostCompact (its stdout is appended verbatim to
    context — a diagnostic JSON blob would be noise)."""
    out = io.StringIO()
    with mock.patch("sys.stdout", out):
        hooklib.emit({}, note="diagnostic", event="PostCompact")
    assert out.getvalue() == ""


def test_emit_writes_json_on_sessionstart():
    out = io.StringIO()
    with mock.patch("sys.stdout", out):
        hooklib.emit({}, note="why", event="SessionStart")
    obj = json.loads(out.getvalue())
    assert obj["_note"] == "why"


def test_emit_swallows_broken_pipe():
    """A closed stdout must not raise out of _emit."""
    class _Boom:
        def write(self, *a): raise BrokenPipeError("closed")
        def flush(self): raise BrokenPipeError("closed")
    with mock.patch("sys.stdout", _Boom()):
        hooklib.emit({}, event="SessionStart")   # must not raise


def test_dedup_path_postcompact_emits_nothing(isolated_state):
    """The sibling-dedup no-op on PostCompact must emit literally nothing (not a
    JSON _note that would pollute the verbatim-stdout context)."""
    hrl = isolated_state
    payload = {"hook_event_name": "PostCompact", "session_id": "Z", "cwd": "."}
    out = io.StringIO()
    with mock.patch.object(hrl, "build_block", lambda c, p, **k: "<komi-recall>x</komi-recall>"), \
         mock.patch.object(hrl, "_compaction_already_served", lambda p, e: True), \
         mock.patch.object(hrl, "_read_stdin_json", lambda: payload), \
         mock.patch("sys.stdout", out):
        rc = hrl.main()
    assert rc == 0
    assert out.getvalue() == ""


def test_hook_capture_bounds_stdin(monkeypatch, tmp_path):
    """hook_capture must apply the same stdin cap as hook_recall, then no-op safely."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths, hook_capture
    importlib.reload(paths)
    importlib.reload(hook_capture)
    huge = "x" * (hooklib._MAX_STDIN_BYTES + 100)
    monkeypatch.setattr(hook_capture.sys, "stdin", io.StringIO(huge))
    # capture records the (empty, over-cap) payload and delegates without exploding
    with mock.patch("sys.stdout", io.StringIO()):
        rc = hook_capture.main(default_event="SessionStart")
    assert rc == 0
    # the capture record should reflect an empty/no-op raw (over the cap)
    cap = hook_capture.capture_path()
    rec = json.loads(cap.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["raw_len"] == 0


def test_install_does_not_disable_active_capture(home):
    """Re-running install while capture is ON must NOT overwrite the capture hooks."""
    setup = home
    setup._install_hooks()
    setup.set_capture(True)
    r = setup._install_hooks()
    data = json.loads(setup.settings_path().read_text(encoding="utf-8"))
    ss = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    pc = [h["command"] for e in data["hooks"]["PostCompact"] for h in e["hooks"]]
    assert any("hook_capture" in c for c in ss)        # still capturing
    assert any("hook_capture" in c for c in pc)
    assert "capture left ON" in r.detail               # and the user is told


def test_set_capture_refuses_when_not_installed(home):
    """capture on/off after uninstall (komi stripped, settings.json kept) must NOT
    silently re-install the hooks."""
    setup = home
    setup._install_hooks()
    setup.uninstall(keep_data=True)        # removes komi hooks, leaves settings.json
    r = setup.set_capture(True)
    assert not r.ok
    assert "isn't installed" in r.detail
    # and no komi hooks were created
    data = json.loads(setup.settings_path().read_text(encoding="utf-8"))
    allcmds = [h.get("command", "") for ev in data.get("hooks", {}).values()
               for e in ev for h in e.get("hooks", [])]
    assert not any("komi.adapters.claude_code" in c for c in allcmds)


def test_verify_agent_updated_same_python_not_called_different(home, monkeypatch, capsys):
    """When the upgrade didn't land but the hook interpreter IS sys.executable, the
    message must say 'did not land in this interpreter' — NOT 'DIFFERENT Python'."""
    from komi import cli, updater
    import komi.adapters.claude_code.setup as setup_mod
    import sys
    monkeypatch.setattr(setup_mod, "hook_interpreters", lambda: [sys.executable])
    monkeypatch.setattr(updater, "installed_version_via_subprocess",
                        lambda python=None: "0.3.0")     # stale: upgrade didn't land
    cli._verify_agent_updated("0.4.0")
    out = capsys.readouterr().out
    assert "did not land in this interpreter" in out
    assert "DIFFERENT Python" not in out


def test_read_state_does_not_write(monkeypatch, tmp_path):
    """paths.read_state must not modify state.json (mtime unchanged)."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths
    importlib.reload(paths)
    paths.update_state(lambda s: s.__setitem__("k", 1))   # create the file
    sp = paths.state_path()
    import os
    before = os.stat(sp).st_mtime_ns
    got = paths.read_state()
    after = os.stat(sp).st_mtime_ns
    assert got.get("k") == 1
    assert before == after                  # read_state wrote nothing
