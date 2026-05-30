"""Installer: strict requirements gate, merge-not-clobber, backup, idempotency,
uninstall, doctor, env load.

All against a temp CLAUDE_CONFIG_DIR so the real ~/.claude is never touched.

Install now GATES on a real model verification. Tests that exercise the *post-gate*
behavior use the ``working_model`` fixture to mock a verified model; tests for the
gate itself mock a failing one.
"""

import json

import pytest

from komi.adapters.claude_code import setup, doctor
from komi.adapters.claude_code import requirements as reqmod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    # paths reads the env lazily, so no reload needed; return the dir
    return tmp_path


@pytest.fixture
def working_model(monkeypatch):
    """Make install proceed past the strict gate, regardless of what's installed on
    the test runner. We mock BOTH verify_model() AND collect(): a bare CI runner has
    no `claude` CLI (a REQUIRED check), so mocking only the model still leaves the
    gate tripped and every post-gate test fails (this is what CI caught). Mocking
    collect() to return all-passing required reqs makes these tests hermetic — they
    exercise post-gate install behavior, not the host's environment."""
    ok_model = reqmod.Requirement("model", True, True, "mock model verified")
    monkeypatch.setattr(reqmod, "verify_model", lambda **kw: ok_model)

    def _all_pass(*, api_key=None, pool=False):
        return [
            reqmod.Requirement("python", True, True, "mock python ok"),
            reqmod.Requirement("claude-cli", True, True, "mock claude CLI ok"),
            ok_model,
            reqmod.Requirement("git", True, False, "mock git ok"),
            reqmod.Requirement("signing", True, False, "mock signing ok"),
        ]
    monkeypatch.setattr(reqmod, "collect", _all_pass)
    # setup.py imports these names from reqmod at call time, so patching reqmod is enough
    return ok_model


def _settings(home):
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


# ── the strict gate ─────────────────────────────────────────────────────

def test_install_gates_when_model_unverified(home, monkeypatch):
    """No working model → install STOPS, registers NO hooks, settings untouched."""
    bad = reqmod.Requirement("model", False, True, "no working model path", "claude auth login")
    monkeypatch.setattr(reqmod, "verify_model", lambda **kw: bad)
    rep = setup.install()
    assert rep.gated is True
    assert rep.ok is False
    # crucially: nothing written to settings.json
    assert not (home / "settings.json").exists() or \
        "komi.adapters" not in (home / "settings.json").read_text(encoding="utf-8")


def test_gate_does_not_touch_existing_settings(home, monkeypatch):
    (home / "settings.json").write_text(json.dumps({"alwaysThinkingEnabled": True}),
                                        encoding="utf-8")
    bad = reqmod.Requirement("model", False, True, "no model", "fix it")
    monkeypatch.setattr(reqmod, "verify_model", lambda **kw: bad)
    setup.install()
    s = _settings(home)
    assert s == {"alwaysThinkingEnabled": True}             # byte-for-byte untouched


def test_allow_incomplete_installs_despite_unmet(home, monkeypatch):
    bad = reqmod.Requirement("model", False, True, "no model", "fix it")
    monkeypatch.setattr(reqmod, "verify_model", lambda **kw: bad)
    rep = setup.install(allow_incomplete=True)
    assert rep.gated is False
    s = _settings(home)
    cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
    assert any("komi.adapters" in c for c in cmds)          # hooks installed anyway


# ── post-gate behavior (model verified via fixture) ────────────────────────

def test_install_creates_hooks_and_config(home, working_model):
    rep = setup.install()
    assert rep.ok
    s = _settings(home)
    assert "SessionStart" in s["hooks"]
    cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
    assert any("komi.adapters.claude_code.hook_recall" in c for c in cmds)
    assert (home / "komi" / "config.json").exists()


def test_install_uses_absolute_python_path(home, working_model):
    import os
    setup.install()
    s = _settings(home)
    cmd = next(h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]
               if "komi.adapters" in h["command"])
    # the contract is an ABSOLUTE interpreter path (so the hook can't break on a
    # PATH mismatch), not merely "not the literal string python". Strip surrounding
    # quotes the command may add for paths-with-spaces, then require absoluteness.
    interp = cmd.split(" -m ")[0].strip().strip('"')
    assert os.path.isabs(interp), f"hook interpreter not absolute: {interp!r}"


def test_install_merges_not_clobbers(home, working_model):
    (home / "settings.json").write_text(json.dumps({
        "alwaysThinkingEnabled": True,
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo MINE"}]}]},
    }), encoding="utf-8")
    setup.install()
    s = _settings(home)
    assert s["alwaysThinkingEnabled"] is True
    cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
    assert any("echo MINE" in c for c in cmds)
    assert any("komi.adapters" in c for c in cmds)
    assert (home / "settings.json.komi-bak").exists()


def test_install_is_idempotent(home, working_model):
    setup.install(); setup.install(); setup.install()
    s = _settings(home)
    cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
    assert sum(1 for c in cmds if "komi.adapters" in c) == 1


def test_install_self_heals_stale_hook_command(home, working_model):
    (home / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "python -m komi.adapters.claude_code.hook_recall"}
        ]}]},
    }), encoding="utf-8")
    setup.install()
    s = _settings(home)
    cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]
            if "komi.adapters" in h["command"]]
    assert len(cmds) == 1
    # the stale bare-`python` command must have been upgraded to an absolute path
    import os
    interp = cmds[0].split(" -m ")[0].strip().strip('"')
    assert os.path.isabs(interp), f"stale command not healed to absolute path: {interp!r}"


def test_install_stores_api_key(home, working_model):
    setup.install(api_key="sk-test-key-1234567890")
    env = (home / "komi" / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-test-key-1234567890" in env


def test_uninstall_removes_hooks_keeps_data(home, working_model):
    setup.install()
    setup.uninstall(keep_data=True)
    s = _settings(home)
    cmds = [h["command"] for ev in s.get("hooks", {}).values()
            for e in ev for h in e.get("hooks", [])]
    assert not any("komi.adapters" in c for c in cmds)
    assert (home / "komi" / "config.json").exists()


def test_uninstall_preserves_other_hooks(home, working_model):
    (home / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo MINE"}]}]},
    }), encoding="utf-8")
    setup.install()
    setup.uninstall()
    s = _settings(home)
    cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
    assert any("echo MINE" in c for c in cmds)


def test_doctor_runs_clean_after_install(home, working_model, monkeypatch):
    setup.install()
    # doctor's distillation check also uses verify_model → already mocked OK
    checks = doctor.run_doctor()
    by = {c.name: c for c in checks}
    assert by["install"].status == "pass"
    assert by["hooks"].status == "pass"
    assert by["distillation"].status == "pass"
    assert all(c.status != "fail" for c in checks)


def test_doctor_flags_missing_hooks(home):
    # config dir exists but no install run
    (home / "settings.json").write_text("{}", encoding="utf-8")
    checks = doctor.run_doctor()
    hooks = next(c for c in checks if c.name == "hooks")
    assert hooks.status == "fail"
    assert "install" in hooks.fix.lower()


def test_env_loader_picks_up_stored_key(home, working_model, monkeypatch):
    setup.install(api_key="sk-stored-9876543210")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from komi.adapters.claude_code import llm
    llm._load_komi_env()
    import os
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-stored-9876543210"


def test_api_key_stored_even_when_gated(home, monkeypatch):
    """Key is persisted up front (before the gate), so a passed key survives even
    if some OTHER requirement fails — the user's input isn't lost on a re-run."""
    bad = reqmod.Requirement("model", False, True, "no model", "fix")
    monkeypatch.setattr(reqmod, "verify_model", lambda **kw: bad)
    setup.install(api_key="sk-persist-123456")
    env = (home / "komi" / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-persist-123456" in env
