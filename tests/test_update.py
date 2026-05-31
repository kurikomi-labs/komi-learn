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
    # basic release ordering
    ("0.4.0", "0.3.0", True),
    ("0.3.1", "0.3.0", True),
    ("0.3.0", "0.3.0", False),     # equal is not newer
    ("0.2.0", "0.3.0", False),     # older
    ("0.10.0", "0.9.0", True),     # numeric, not lexicographic
    ("1.0.0", "0.99.0", True),
    # zero-padding equivalence: 1.0 == 1.0.0 (must not be a phantom upgrade)
    ("1.0.0", "1.0", False),
    ("1.0", "1.0.0", False),
    # pre-releases sort BEFORE the final release
    ("0.4.0", "0.4.0rc1", True),   # final newer than its rc
    ("0.4.0rc1", "0.4.0", False),  # rc not newer than final
    ("0.4.0rc2", "0.4.0rc1", True),
    ("0.4.0b1", "0.4.0a1", True),  # b > a
    ("0.4.0a1", "0.4.0b1", False),
    # dev sorts before pre; post sorts after final
    ("0.4.0rc1", "0.4.0.dev1", True),
    ("0.4.0.dev1", "0.4.0rc1", False),
    ("1.0.0.post1", "1.0.0", True),
    # epoch dominates the release number
    ("1!2.0", "2.0", True),
    ("2.0", "1!2.0", False),
    # tolerate a leading v/V tag
    ("v0.4.0", "0.3.0", True),
    ("0.4.0", "v0.4.0", False),
    # empty / junk never spuriously beats a real release
    ("", "0.3.0", False),
    ("0.3.0", "", True),
])
def test_is_newer(latest, current, expected):
    assert U.is_newer(latest, current) is expected


def test_parse_version_release_tuple():
    # _parse_version returns the numeric release tuple with trailing zeros stripped
    assert U._parse_version("0.4.0rc1") == (0, 4)   # pre-release suffix dropped
    assert U._parse_version("0.4") == (0, 4)
    assert U._parse_version("1.2.3") == (1, 2, 3)
    assert U._parse_version("v2.0.0") == (2,)       # leading v + trailing zeros
    assert U._parse_version("") == (0,)


def test_version_ordering_is_total_and_self_consistent():
    # an ascending chain must be strictly increasing under is_newer, and each step
    # must be antisymmetric (a>b implies not b>a)
    chain = ["0.4.0.dev1", "0.4.0a1", "0.4.0b1", "0.4.0rc1", "0.4.0",
             "0.4.0.post1", "0.4.1", "0.5.0", "1.0.0", "1!0.0.1"]
    for lo, hi in zip(chain, chain[1:]):
        assert U.is_newer(hi, lo) is True, f"{hi} should be newer than {lo}"
        assert U.is_newer(lo, hi) is False, f"{lo} should NOT be newer than {hi}"


# ── PyPI lookup ──────────────────────────────────────────────────────────────

class _FakeResp:
    """Stands in for the urllib response: https final URL + a bounded read()."""
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def geturl(self): return U.PYPI_JSON_URL
    def read(self, n=-1): return self._b[:n] if (n and n > 0) else self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_check_latest_parses_version():
    with mock.patch.object(U._NO_REDIRECT_OPENER, "open",
                           return_value=_FakeResp({"info": {"version": "0.5.0"}})):
        assert U.check_latest() == "0.5.0"


def test_check_latest_network_failure_returns_none():
    # offline / PyPI down must be non-fatal — returns None, never raises
    with mock.patch.object(U._NO_REDIRECT_OPENER, "open", side_effect=OSError("no network")):
        assert U.check_latest() is None


def test_check_latest_malformed_payload_returns_none():
    with mock.patch.object(U._NO_REDIRECT_OPENER, "open",
                           return_value=_FakeResp({"nope": 1})):
        assert U.check_latest() is None


# ── install-method detection ─────────────────────────────────────────────────

def test_plan_pipx_when_prefix_looks_like_pipx(monkeypatch):
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.setattr(U.sys, "prefix", "/home/u/.local/pipx/venvs/komi-learn")
    monkeypatch.setattr(U.shutil, "which", lambda name: "/usr/local/bin/pipx")
    plan = U.plan_upgrade()
    assert plan.manager == "pipx"
    assert plan.runnable is True
    assert plan.cmd == ["/usr/local/bin/pipx", "upgrade", "komi-learn"]


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
    good_plan = U.UpgradePlan("pip", ["py", "-m", "pip", "install", "-U", "komi-learn"], True)
    with mock.patch.object(U, "check_latest", return_value="0.9.0"), \
         mock.patch.object(U, "plan_upgrade", return_value=good_plan), \
         mock.patch.object(U, "run_upgrade") as run:
        rc, out = _capture(cli.cmd_update, _args(check=True))
    assert rc == 0
    run.assert_not_called()             # --check must never mutate the environment
    # must show the CHECK-specific "to upgrade" line + the actual command (not just
    # the generic "newer version available" line that every non-latest path prints)
    assert "to upgrade:" in out
    assert "py -m pip install -U komi-learn" in out


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


def test_cmd_update_does_not_claim_unconfirmed_version(monkeypatch):
    """If the post-upgrade re-check can't read a version (returns None), we must
    NOT print 'upgraded 0.3.0 → 0.9.0' as if confirmed — pip exiting 0 isn't proof
    the new version actually landed."""
    monkeypatch.setattr("komi.__version__", "0.3.0", raising=False)
    good_plan = U.UpgradePlan("pip", ["py", "-m", "pip", "install", "-U", "komi-learn"], True)
    with mock.patch.object(U, "check_latest", return_value="0.9.0"), \
         mock.patch.object(U, "plan_upgrade", return_value=good_plan), \
         mock.patch.object(U, "run_upgrade", return_value=(True, "upgraded")), \
         mock.patch.object(U, "installed_version_via_subprocess", return_value=None):
        rc, out = _capture(cli.cmd_update, _args(yes=True))
    assert rc == 0
    assert "couldn't confirm" in out
    # the generic "a newer version is available — 0.3.0 → 0.9.0" line is fine; what
    # must NOT appear is the CONFIRMED claim "upgraded ... → 0.9.0"
    assert "upgraded" not in out


# ── security: PyPI fetch hardening ────────────────────────────────────────────

def test_check_latest_refuses_redirect():
    """The lookup must not follow a 3xx — a redirect to http/another host could
    feed a spoofed 'newer' version. The no-redirect opener raises, we return None."""
    import urllib.error
    err = urllib.error.HTTPError(U.PYPI_JSON_URL, 302, "Found", {}, None)
    with mock.patch.object(U._NO_REDIRECT_OPENER, "open", side_effect=err):
        assert U.check_latest() is None


def test_check_latest_rejects_non_https_final_url():
    class _Resp:
        def geturl(self): return "http://evil.example/komi-learn/json"
        def read(self, *a): return b'{"info":{"version":"999.0.0"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with mock.patch.object(U._NO_REDIRECT_OPENER, "open", return_value=_Resp()):
        assert U.check_latest() is None      # downgraded scheme → refuse the answer


def test_check_latest_bounds_oversized_body():
    big = b"x" * (U._MAX_PYPI_BYTES + 1000)
    class _Resp:
        def geturl(self): return U.PYPI_JSON_URL
        def read(self, n=-1): return big[:n] if n and n > 0 else big
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with mock.patch.object(U._NO_REDIRECT_OPENER, "open", return_value=_Resp()):
        # reads at most _MAX_PYPI_BYTES+1, sees it's over the cap → None (no OOM, no parse)
        assert U.check_latest() is None


# ── security: pipx upgrade path ───────────────────────────────────────────────

def test_pipx_plan_uses_absolute_path(monkeypatch):
    monkeypatch.setattr(U, "_is_pipx", lambda: True)
    monkeypatch.setattr(U.shutil, "which", lambda name: "/usr/local/bin/pipx")
    plan = U.plan_upgrade()
    assert plan.manager == "pipx"
    assert plan.runnable is True
    assert plan.cmd[0] == "/usr/local/bin/pipx"   # absolute, never bare "pipx" off PATH
    assert plan.cmd[0] != "pipx"


def test_pipx_plan_refuses_when_pipx_not_found(monkeypatch):
    monkeypatch.setattr(U, "_is_pipx", lambda: True)
    monkeypatch.setattr(U.shutil, "which", lambda name: None)
    plan = U.plan_upgrade()
    assert plan.runnable is False                 # can't resolve pipx → don't guess


def test_is_pipx_bare_env_var_is_not_enough(monkeypatch):
    """A hostile environment setting PIPX_HOME alone must NOT flip a pip install
    into the pipx branch (which could then run a planted `pipx`)."""
    monkeypatch.setenv("PIPX_HOME", "/tmp/attacker")
    monkeypatch.setattr(U.sys, "prefix", "/usr")   # interpreter NOT under PIPX_HOME
    assert U._is_pipx() is False


def test_is_pipx_true_when_prefix_under_pipx_home(monkeypatch):
    monkeypatch.setenv("PIPX_HOME", "/home/u/.local/pipx")
    monkeypatch.setattr(U.sys, "prefix", "/home/u/.local/pipx/venvs/komi-learn")
    assert U._is_pipx() is True


def test_is_pipx_true_from_prefix_substring(monkeypatch):
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.setattr(U.sys, "prefix", "/home/u/.local/pipx/venvs/komi-learn")
    assert U._is_pipx() is True


# ── version: single source of truth ───────────────────────────────────────────

def test_package_version_is_single_source_of_truth():
    """Regression: __version__ was hardcoded 0.1.0 while pyproject was 0.3.0.
    Now pyproject reads komi.__version__ dynamically, so they can't diverge."""
    import komi
    assert komi.__version__ != "0.1.0"
    # at least 0.3.0 (use the version comparator, not raw tuples — 0.3.0 -> (0,3))
    assert not U.is_newer("0.3.0", komi.__version__)  # 0.3.0 is not newer than us


def test_pyproject_uses_dynamic_version():
    """Guard the dynamic-version wiring so nobody re-introduces a static literal
    in pyproject that could drift from komi.__version__."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'attr = "komi.__version__"' in text
    # there must be no static `version = "x.y.z"` under [project]
    import re as _re
    assert not _re.search(r'(?m)^\s*version\s*=\s*"\d', text)
