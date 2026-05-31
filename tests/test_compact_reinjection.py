"""Compact-aware re-injection: the recall hook must re-emit learnings after a
/compact, in the format each event supports.

Background (see hook_recall module docstring): compaction can drop the learnings
injected at SessionStart, so the agent stops applying them mid-session. We re-inject
on two events — SessionStart(source=compact) via JSON additionalContext, and
PostCompact via plain stdout — because neither is fully reliable alone on current
Claude Code. These tests pin the routing and the installer registration.
"""

import io
import json
import os
import importlib
import tempfile
from unittest import mock

import pytest

from komi.adapters.claude_code import hook_recall as hr


_BLOCK = "<komi-recall>SAMPLE LEARNING</komi-recall>"


def _run_main(payload: dict) -> str:
    """Drive hook_recall.main() with a given stdin payload + a stub recall block.
    Returns whatever it wrote to stdout."""
    out = io.StringIO()
    # Stub the breadcrumb/dedup helpers so routing tests don't touch the real
    # state.json or cross-contaminate each other (each drives one event).
    with mock.patch.object(hr, "build_block", lambda cwd, p, **k: _BLOCK), \
         mock.patch.object(hr, "_maybe_sync_pool", lambda: None), \
         mock.patch.object(hr, "_compaction_already_served", lambda p, e: False), \
         mock.patch.object(hr, "_record_compaction_served", lambda p, e: None), \
         mock.patch.object(hr, "_read_stdin_json", lambda: payload), \
         mock.patch("sys.stdout", out):
        rc = hr.main()
    assert rc == 0
    return out.getvalue()


# ── event routing ────────────────────────────────────────────────────────────

def test_startup_emits_json_additionalcontext_unframed():
    out = _run_main({"hook_event_name": "SessionStart", "source": "startup", "cwd": "."})
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert _BLOCK in ctx
    assert "compacted" not in ctx          # normal start: no re-application framing


def test_sessionstart_compact_emits_json_with_framing():
    out = _run_main({"hook_event_name": "SessionStart", "source": "compact", "cwd": "."})
    obj = json.loads(out)                  # still JSON additionalContext
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert _BLOCK in ctx
    assert "compacted" in ctx              # tells the model these are re-applied


def test_postcompact_emits_plain_stdout_not_json():
    out = _run_main({"hook_event_name": "PostCompact", "trigger": "manual", "cwd": "."})
    # PostCompact uses the plain-stdout add-to-context path, so it must NOT be JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert _BLOCK in out
    assert "compacted" in out


def test_legacy_payload_behaves_as_session_start():
    # a bare/old payload (no hook_event_name) must still inject as SessionStart JSON
    out = _run_main({"cwd": "."})
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert _BLOCK in obj["hookSpecificOutput"]["additionalContext"]


def test_empty_block_postcompact_emits_literally_nothing():
    out = io.StringIO()
    with mock.patch.object(hr, "build_block", lambda cwd, p, **k: ""), \
         mock.patch.object(hr, "_maybe_sync_pool", lambda: None), \
         mock.patch.object(hr, "_compaction_already_served", lambda p, e: False), \
         mock.patch.object(hr, "_record_compaction_served", lambda p, e: None), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "PostCompact", "trigger": "auto"}), \
         mock.patch("sys.stdout", out):
        hr.main()
    # PostCompact stdout is appended verbatim to context — a diagnostic "{}" would
    # be noise, so emit literally nothing.
    assert out.getvalue() == ""


def test_empty_block_sessionstart_emits_empty_json():
    out = io.StringIO()
    with mock.patch.object(hr, "build_block", lambda cwd, p, **k: ""), \
         mock.patch.object(hr, "_maybe_sync_pool", lambda: None), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "SessionStart", "source": "startup"}), \
         mock.patch("sys.stdout", out):
        hr.main()
    # SessionStart additionalContext is structured JSON → empty object is the no-op
    assert out.getvalue() == "{}"


def test_recall_failure_never_breaks_session():
    """On SessionStart a recall failure emits a JSON no-op with a _note diagnostic."""
    def boom(cwd, p, **k):
        raise RuntimeError("store exploded")
    out = io.StringIO()
    with mock.patch.object(hr, "build_block", boom), \
         mock.patch.object(hr, "_maybe_sync_pool", lambda: None), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "SessionStart", "source": "startup"}), \
         mock.patch("sys.stdout", out):
        rc = hr.main()
    assert rc == 0                                   # graceful, non-fatal
    assert "_note" in json.loads(out.getvalue())     # records why it skipped


def test_recall_failure_postcompact_emits_nothing():
    """On PostCompact the same failure must emit literally nothing (no JSON diagnostic
    polluting the verbatim-stdout context) and still be non-fatal."""
    def boom(cwd, p, **k):
        raise RuntimeError("store exploded")
    out = io.StringIO()
    with mock.patch.object(hr, "build_block", boom), \
         mock.patch.object(hr, "_compaction_already_served", lambda p, e: False), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "PostCompact"}), \
         mock.patch("sys.stdout", out):
        rc = hr.main()
    assert rc == 0
    assert out.getvalue() == ""


def test_compaction_skips_background_maintenance():
    """A compaction re-inject must NOT kick off pool sync / curator (those belong to
    a genuine session start; firing them mid-session is wrong)."""
    called = {"sync": False, "curate": False}
    with mock.patch.object(hr, "build_block", lambda cwd, p, **k: _BLOCK), \
         mock.patch.object(hr, "_maybe_sync_pool",
                           lambda: called.__setitem__("sync", True)), \
         mock.patch.object(hr, "_compaction_already_served", lambda p, e: False), \
         mock.patch.object(hr, "_record_compaction_served", lambda p, e: None), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "PostCompact", "trigger": "manual"}), \
         mock.patch("sys.stdout", io.StringIO()):
        hr.main()
    assert called["sync"] is False        # not synced on a compaction event


def test_session_start_does_run_background_maintenance():
    called = {"sync": False}
    with mock.patch.object(hr, "build_block", lambda cwd, p, **k: _BLOCK), \
         mock.patch.object(hr, "_maybe_sync_pool",
                           lambda: called.__setitem__("sync", True)), \
         mock.patch.object(hr, "_read_stdin_json",
                           lambda: {"hook_event_name": "SessionStart", "source": "startup"}), \
         mock.patch("sys.stdout", io.StringIO()):
        hr.main()
    assert called["sync"] is True         # genuine start: maintenance runs


# ── installer registers PostCompact ───────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    from komi.adapters.claude_code import paths, setup
    importlib.reload(paths)
    importlib.reload(setup)
    return setup


def _cmds(setup_mod, event):
    data = json.loads(setup_mod.settings_path().read_text(encoding="utf-8"))
    return [h["command"] for e in data.get("hooks", {}).get(event, [])
            for h in e.get("hooks", [])]


def test_install_registers_postcompact(home):
    setup = home
    setup._install_hooks()
    pc = _cmds(setup, "PostCompact")
    assert len(pc) == 1
    assert "hook_compact" in pc[0]
    assert pc[0].split(" -m ")[0].strip().strip('"') not in ("python", "python3")  # absolute


def test_install_postcompact_idempotent(home):
    setup = home
    setup._install_hooks(); setup._install_hooks(); setup._install_hooks()
    assert len(_cmds(setup, "PostCompact")) == 1


def test_uninstall_removes_postcompact(home):
    setup = home
    setup._install_hooks()
    setup.uninstall(keep_data=True)
    komi_pc = [c for c in _cmds(setup, "PostCompact") if "komi" in c]
    assert komi_pc == []


def test_plugin_manifest_has_postcompact():
    """The plugin install path uses hooks/hooks.json; it must declare PostCompact too."""
    from pathlib import Path
    manifest = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    pc = data["hooks"].get("PostCompact", [])
    cmds = [h["command"] for e in pc for h in e.get("hooks", [])]
    assert any("hook_compact" in c for c in cmds)
