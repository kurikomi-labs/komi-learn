"""komi-learn — self-update: check PyPI for a newer release and upgrade in place.

`komi-learn update` should Just Work regardless of how the user installed us. The
hard part isn't the PyPI check — it's upgrading *the right environment*. A blind
`pip install -U` can hit the wrong interpreter, fight a pipx-managed venv, or need
permissions it doesn't have. So we detect the install method first and run the
command that matches it (pip-into-this-interpreter, or `pipx upgrade`). If we
genuinely can't tell, we print the command instead of guessing and breaking the
user's environment.

Everything here is best-effort and network-failure-safe: a flaky PyPI lookup never
raises out of `check_latest`, it just returns ``None``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional

DIST_NAME = "komi-learn"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"
_PYPI_TIMEOUT = 8  # seconds — a stuck lookup must not hang the CLI


# ── version comparison ───────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple:
    """Best-effort PEP 440-ish parse into a comparable tuple of ints.

    We prefer `packaging.version` when it's importable (it almost always is, since
    pip ships it), because it handles pre-releases / epochs correctly. Without it
    we fall back to splitting the leading ``X.Y.Z`` numeric release segment — good
    enough to tell "0.3.0 < 0.4.0", which is all `update` needs.
    """
    nums = []
    for part in (v or "").strip().split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break  # stop at the first non-digit (e.g. "0rc1" -> 0)
        if digits == "":
            break
        nums.append(int(digits))
    return tuple(nums) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a strictly newer version than ``current``."""
    try:
        from packaging.version import Version  # type: ignore

        return Version(latest) > Version(current)
    except Exception:
        return _parse_version(latest) > _parse_version(current)


# ── PyPI lookup ──────────────────────────────────────────────────────────────

def check_latest(*, timeout: int = _PYPI_TIMEOUT) -> Optional[str]:
    """Return the latest version string on PyPI, or ``None`` if it can't be
    determined (offline, PyPI down, malformed payload). Never raises."""
    try:
        req = urllib.request.Request(
            PYPI_JSON_URL, headers={"Accept": "application/json",
                                    "User-Agent": f"{DIST_NAME}-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - https literal
            data = json.loads(resp.read().decode("utf-8"))
        ver = (data.get("info") or {}).get("version")
        return ver or None
    except Exception:
        return None


# ── install-method detection ─────────────────────────────────────────────────

def _is_pipx() -> bool:
    """Heuristic: are we running from a pipx-managed venv? pipx installs each app
    into ``.../pipx/venvs/<app>/`` and sets PIPX_* env vars in its shims."""
    if os.environ.get("PIPX_HOME") or os.environ.get("PIPX_BIN_DIR"):
        return True
    prefix = (sys.prefix or "").replace("\\", "/").lower()
    return "/pipx/venvs/" in prefix or "/pipx/shared" in prefix


def _pip_available() -> bool:
    try:
        import pip  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class UpgradePlan:
    """How we intend to upgrade. ``cmd`` is the argv to run; ``manager`` is for
    display; ``runnable`` is False when we couldn't determine a safe command (then
    ``cmd`` is the human-facing suggestion to print, not to execute)."""
    manager: str
    cmd: list
    runnable: bool

    def display(self) -> str:
        return " ".join(self.cmd)


def plan_upgrade() -> UpgradePlan:
    """Decide the upgrade command for *this* environment."""
    if _is_pipx():
        # pipx manages its own venv; pip -U inside it is the wrong tool.
        return UpgradePlan("pipx", ["pipx", "upgrade", DIST_NAME], runnable=True)
    if _pip_available():
        # Upgrade into the very interpreter that's running komi-learn — this is the
        # one whose `import komi` the hooks use. Mirrors model_install.py.
        return UpgradePlan(
            "pip",
            [sys.executable, "-m", "pip", "install", "--upgrade", DIST_NAME],
            runnable=True,
        )
    # Couldn't find a package manager we trust to drive — hand the user a command
    # rather than risk corrupting a standalone/frozen install.
    return UpgradePlan("pip", ["pip", "install", "--upgrade", DIST_NAME], runnable=False)


# ── upgrade execution + re-verify ────────────────────────────────────────────

def installed_version_via_subprocess() -> Optional[str]:
    """Read komi-learn's version in a *fresh* interpreter.

    importlib.metadata caches distribution info for the life of a process, so after
    an in-process pip upgrade the running interpreter still reports the OLD version.
    Shelling out to a clean python gets the truth post-upgrade.
    """
    code = (
        "import sys\n"
        "try:\n"
        "    from importlib.metadata import version\n"
        f"    sys.stdout.write(version('{DIST_NAME}'))\n"
        "except Exception:\n"
        "    pass\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip()
        return out or None
    except Exception:
        return None


@dataclass
class UpdateResult:
    ok: bool
    detail: str
    old_version: str = ""
    new_version: str = ""
    ran: bool = False  # whether an upgrade command was actually executed


def run_upgrade(plan: UpgradePlan, *, timeout: int = 1200) -> tuple[bool, str]:
    """Execute the upgrade command. Returns (ok, detail). Output streams to the
    user's terminal so they see pip's progress (and any resolver errors)."""
    if not plan.runnable:
        return False, "no runnable upgrade command for this environment"
    try:
        r = subprocess.run(plan.cmd, timeout=timeout)
    except FileNotFoundError:
        return False, f"`{plan.cmd[0]}` not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "upgrade timed out"
    except Exception as e:
        return False, f"upgrade failed to launch: {e}"
    if r.returncode != 0:
        return False, f"upgrade exited {r.returncode}"
    return True, "upgraded"


__all__ = [
    "DIST_NAME", "PYPI_JSON_URL",
    "check_latest", "is_newer", "plan_upgrade", "UpgradePlan",
    "run_upgrade", "installed_version_via_subprocess",
    "UpdateResult",
]
