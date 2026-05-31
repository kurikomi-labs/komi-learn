"""`komi-learn update`: self-update against PyPI.

Covers the two halves: the pure logic in komi.updater (version compare, PyPI
lookup, install-method detection, upgrade execution) and the CLI command's
routing (up-to-date / newer / --check / undetectable / failure). All network and
subprocess calls are mocked — these tests never touch PyPI or run pip.
"""

import io
import json
from unittest import mock

import pytest

from komi import updater as U
from komi import cli


# ── version comparison ───────────────────────────────────────────────────────

@pytest.mark.parametrize("latest,current,expected", [
    ("0.4.0", "0.3.0", True),
    ("0.3.1", "0.3.0", True),
    ("0.3.0", "0.3.0", False),     # equal is not newer
    ("0.2.0", "0.3.0", False),     # older
    ("0.10.0", "0.9.0", True),     # numeric, not lexicographic
    ("1.0.0", "0.99.0", True),
])
def test_is_newer(latest, current, expected):
    assert U.is_newer(latest, current) is expected


def test_is_newer_tolerates_suffixes():
    # fallback parser must not choke on pre-release-ish strings
    assert U._parse_version("0.4.0rc1") == (0, 4, 0)
    assert U._parse_version("0.4") == (0, 4)
    assert U._parse_version("") == (0,)


# ── PyPI lookup ──────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_check_latest_parses_version():
    with mock.patch("urllib.request.urlopen",
                    return_value=_FakeResp({"info": {"version": "0.5.0"}})):
        assert U.check_latest() == "0.5.0"


def test_check_latest_network_failure_returns_none():
    # offline / PyPI down must be non-fatal — returns None, never raises
    with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")):
        assert U.check_latest() is None


def test_check_latest_malformed_payload_returns_none():
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp({"nope": 1})):
        assert U.check_latest() is None


# ── install-method detection ─────────────────────────────────────────────────

def test_plan_pipx_when_env_set(monkeypatch):
    monkeypatch.setenv("PIPX_HOME", "/home/u/.local/pipx")
    plan = U.plan_upgrade()
    assert plan.manager == "pipx"
    assert plan.cmd == ["pipx", "upgrade", "komi-learn"]
    assert plan.runnable is True


def test_plan_pipx_when_prefix_looks_like_pipx(monkeypatch):
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.setattr(U.sys, "prefix", "/home/u/.local/pipx/venvs/komi-learn")
    assert U.plan_upgrade().manager == "pipx"


def test_plan_pip_uses_running_interpreter(monkeypatch):
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.setattr(U.sys, "prefix", "/usr")
    monkeypatch.setattr(U.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(U, "_pip_available", lambda: True)
    plan = U.plan_upgrade()
    assert plan.manager == "pip"
    assert plan.runnable is True
    # critical: upgrade THIS interpreter, the one the hooks import
    assert plan.cmd[:4] == ["/usr/bin/python3", "-m", "pip", "install"]
    assert "--upgrade" in plan.cmd and "komi-learn" in plan.cmd


def test_plan_falls_back_to_unrunnable_when_no_pip(monkeypatch):
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.setattr(U.sys, "prefix", "/opt/frozen")
    monkeypatch.setattr(U, "_pip_available", lambda: False)
    plan = U.plan_upgrade()
    assert plan.runnable is False     # never guess a command we can't run safely


# ── upgrade execution ────────────────────────────────────────────────────────

def test_run_upgrade_success():
    plan = U.UpgradePlan("pip", ["python", "-m", "pip", "install", "-U", "komi-learn"], True)
    with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
        ok, detail = U.run_upgrade(plan)
    assert ok is True


def test_run_upgrade_nonzero_exit():
    plan = U.UpgradePlan("pip", ["python", "-m", "pip", "install", "-U", "komi-learn"], True)
    with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
        ok, detail = U.run_upgrade(plan)
    assert ok is False and "exited 1" in detail


def test_run_upgrade_unrunnable_plan_refuses():
    plan = U.UpgradePlan("pip", ["pip", "install", "-U", "komi-learn"], runnable=False)
    ok, detail = U.run_upgrade(plan)
    assert ok is False and "no runnable" in detail


def test_run_upgrade_missing_binary():
    plan = U.UpgradePlan("pipx", ["pipx", "upgrade", "komi-learn"], True)
    with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
        ok, detail = U.run_upgrade(plan)
    assert ok is False and "not found" in detail


# ── CLI command routing ──────────────────────────────────────────────────────

def _args(**kw):
    ns = mock.Mock()
    ns.check = kw.get("check", False)
    ns.yes = kw.get("yes", False)
    return ns


def _capture(fn, *a):
    out = io.StringIO()
    with mock.patch("sys.stdout", out):
        rc = fn(*a)
    return rc, out.getvalue()


def test_cmd_update_already_latest(monkeypatch):
    monkeypatch.setattr("komi.__version__", "0.3.0", raising=False)
    with mock.patch.object(U, "check_latest", return_value="0.3.0"):
        rc, out = _capture(cli.cmd_update, _args())
    assert rc == 0
    assert "latest version" in out


def test_cmd_update_offline(monkeypatch):
    with mock.patch.object(U, "check_latest", return_value=None):
        rc, out = _capture(cli.cmd_update, _args())
    assert rc == 1
    assert "couldn't reach PyPI" in out


def test_cmd_update_check_only_does_not_upgrade(monkeypatch):
    monkeypatch.setattr("komi.__version__", "0.3.0", raising=False)
    with mock.patch.object(U, "check_latest", return_value="0.9.0"), \
         mock.patch.object(U, "run_upgrade") as run:
        rc, out = _capture(cli.cmd_update, _args(check=True))
    assert rc == 0
    run.assert_not_called()             # --check must never mutate the environment
    assert "0.3.0 → 0.9.0" in out or "0.3.0 → 0.9.0" in out


def test_cmd_update_undetectable_prints_command(monkeypatch):
    monkeypatch.setattr("komi.__version__", "0.3.0", raising=False)
    bad_plan = U.UpgradePlan("pip", ["pip", "install", "-U", "komi-learn"], runnable=False)
    with mock.patch.object(U, "check_latest", return_value="0.9.0"), \
         mock.patch.object(U, "plan_upgrade", return_value=bad_plan), \
         mock.patch.object(U, "run_upgrade") as run:
        rc, out = _capture(cli.cmd_update, _args(yes=True))
    assert rc == 1
    run.assert_not_called()             # unrunnable → instruct, don't execute
    assert "pip install -U komi-learn" in out


def test_cmd_update_runs_upgrade_and_reports_new_version(monkeypatch):
    monkeypatch.setattr("komi.__version__", "0.3.0", raising=False)
    good_plan = U.UpgradePlan("pip", ["py", "-m", "pip", "install", "-U", "komi-learn"], True)
    with mock.patch.object(U, "check_latest", return_value="0.9.0"), \
         mock.patch.object(U, "plan_upgrade", return_value=good_plan), \
         mock.patch.object(U, "run_upgrade", return_value=(True, "upgraded")) as run, \
         mock.patch.object(U, "installed_version_via_subprocess", return_value="0.9.0"):
        rc, out = _capture(cli.cmd_update, _args(yes=True))
    assert rc == 0
    run.assert_called_once()
    assert "0.9.0" in out


def test_cmd_update_reports_upgrade_failure(monkeypatch):
    monkeypatch.setattr("komi.__version__", "0.3.0", raising=False)
    good_plan = U.UpgradePlan("pip", ["py", "-m", "pip", "install", "-U", "komi-learn"], True)
    with mock.patch.object(U, "check_latest", return_value="0.9.0"), \
         mock.patch.object(U, "plan_upgrade", return_value=good_plan), \
         mock.patch.object(U, "run_upgrade", return_value=(False, "upgrade exited 1")):
        rc, out = _capture(cli.cmd_update, _args(yes=True))
    assert rc == 1
    assert "upgrade failed" in out


# ── version constant is no longer stale ───────────────────────────────────────

def test_package_version_matches_metadata():
    """Regression: __version__ was hardcoded 0.1.0 while pyproject was 0.3.0.
    It must now reflect the installed distribution (or the source fallback)."""
    import komi
    assert komi.__version__ != "0.1.0"
    # parses as a real version tuple
    assert U._parse_version(komi.__version__) >= (0, 3, 0)
