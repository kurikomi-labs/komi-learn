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
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional

DIST_NAME = "komi-learn"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"
_PYPI_TIMEOUT = 8  # seconds — a stuck lookup must not hang the CLI
_MAX_PYPI_BYTES = 5 * 1024 * 1024  # cap the response read (one project's JSON << this)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses every redirect (returns None)."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


# Module-level opener so we don't rebuild it per call. Refuses 3xx redirects.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


# ── version comparison ───────────────────────────────────────────────────────
#
# A self-contained PEP 440-lite comparator. We deliberately do NOT depend on
# `packaging`: the engine's ethos is zero required deps, and "is 0.4.0 newer than
# 0.3.0" doesn't justify a runtime dependency that may or may not be present
# (depending on whether the lib happened to compute the answer differently per
# environment). This handles the version shapes komi-learn actually ships —
# release tuples, pre-releases (a/b/rc), post/dev, and a leading epoch — with one
# code path, so the answer never varies by environment.

# pre-release phase ranks: anything pre sorts BEFORE the plain release; post AFTER.
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, "c": 2, "pre": 2,
             "preview": 2}
_FINAL = 3   # a plain release (no pre/post/dev) sits between pre and post
_POST = 4
# .devN sorts before everything at the same release (earliest), so it gets a phase
# below the lowest pre-rank.
_DEV = -1

_RELEASE_RE = re.compile(r"^(?:(\d+)!)?(\d+(?:\.\d+)*)(.*)$")
_PRE_RE = re.compile(r"[._-]?(a|b|c|rc|alpha|beta|pre|preview)[._-]?(\d*)")
_POST_RE = re.compile(r"[._-]?(?:post|rev|r)[._-]?(\d*)|-(\d+)")
_DEV_RE = re.compile(r"[._-]?dev[._-]?(\d*)")


def _norm_release(rel: str) -> tuple:
    """Numeric release tuple with trailing zeros stripped so 1.0 == 1.0.0."""
    parts = [int(p) for p in rel.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _version_key(v: str):
    """Map a version string to a tuple that sorts per PEP 440 (for the shapes we
    ship). Unparseable input sorts lowest so it never spuriously beats a real
    release. The key shape is:
        (epoch, release_tuple, phase, phase_num, dev_num)
    where ``phase`` orders dev < pre(a<b<rc) < final < post.
    """
    s = (v or "").strip().lower()
    if s[:1] == "v":          # tolerate a leading v/V tag
        s = s[1:]
    m = _RELEASE_RE.match(s)
    if not m:
        return (-1, (), _DEV, -1, -1)   # lowest possible
    epoch = int(m.group(1) or 0)
    release = _norm_release(m.group(2))
    suffix = m.group(3) or ""

    dev = _DEV_RE.search(suffix)
    pre = _PRE_RE.search(suffix)
    post = _POST_RE.search(suffix)

    # dev takes precedence as the earliest phase; then pre; then post; else final.
    if dev:
        phase, phase_num = _DEV, int(dev.group(1) or 0)
    elif pre:
        phase, phase_num = _PRE_RANK.get(pre.group(1), 2), int(pre.group(2) or 0)
    elif post:
        phase, phase_num = _POST, int(post.group(1) or post.group(2) or 0)
    else:
        phase, phase_num = _FINAL, 0
    dev_num = int(dev.group(1) or 0) if dev else 0
    return (epoch, release, phase, phase_num, dev_num)


# kept as a public helper (tests + back-compat); now returns the release tuple.
def _parse_version(v: str) -> tuple:
    """The numeric release tuple (trailing zeros stripped), e.g. "0.4.0rc1"->(0,4).
    For full ordering (incl. pre/post/epoch) use :func:`is_newer`."""
    return _version_key(v)[1] or (0,)


def is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a strictly newer version than ``current`` (PEP 440-lite)."""
    return _version_key(latest) > _version_key(current)


# ── PyPI lookup ──────────────────────────────────────────────────────────────

def check_latest(*, timeout: int = _PYPI_TIMEOUT) -> Optional[str]:
    """Return the latest version string on PyPI, or ``None`` if it can't be
    determined (offline, PyPI down, malformed payload). Never raises."""
    try:
        req = urllib.request.Request(
            PYPI_JSON_URL, headers={"Accept": "application/json",
                                    "User-Agent": f"{DIST_NAME}-updater"})
        # Refuse redirects: the hardcoded https literal only guarantees the FIRST
        # hop. urllib's default handler would follow a 3xx to any host/scheme,
        # including http — a MITM could downgrade the lookup and feed us a fake
        # "newer" version to coerce an upgrade. PyPI's JSON API doesn't redirect
        # cross-scheme here, so refusing is safe.
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # nosec B310 - https, no-redirect
            if not (resp.geturl() or "").lower().startswith("https://"):
                return None
            # Bound the read: a hostile endpoint must not be able to stream a huge
            # body and OOM the CLI. One project's JSON is far under this cap.
            raw = resp.read(_MAX_PYPI_BYTES + 1)
        if len(raw) > _MAX_PYPI_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
        ver = (data.get("info") or {}).get("version")
        return ver or None
    except Exception:
        return None


# ── install-method detection ─────────────────────────────────────────────────

def _is_pipx() -> bool:
    """Are we running from a pipx-managed venv? pipx installs each app into
    ``.../pipx/venvs/<app>/``.

    We key off the *interpreter prefix* — the one signal an attacker can't set
    just by exporting an env var. A bare ``PIPX_HOME`` is NOT sufficient on its
    own: that would let a hostile environment flip a plain pip install into the
    pipx branch (and then a planted ``pipx`` on PATH would run). The env var only
    *corroborates* a prefix that already lives under it.
    """
    prefix = (sys.prefix or "").replace("\\", "/")
    low = prefix.lower()
    if "/pipx/venvs/" in low or "/pipx/shared" in low:
        return True
    # If PIPX_HOME is set AND our interpreter actually lives under it, trust it.
    pipx_home = (os.environ.get("PIPX_HOME") or "").replace("\\", "/")
    if pipx_home and prefix.startswith(pipx_home):
        return True
    return False


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
        # pipx manages its own venv; pip -U inside it is the wrong tool. Resolve
        # pipx to an ABSOLUTE path (never an unqualified "pipx" off PATH — that
        # would let a planted binary earlier in PATH run during `update`). If we
        # can't find it, refuse and print the command rather than guess.
        pipx_path = shutil.which("pipx")
        if not pipx_path:
            return UpgradePlan("pipx", ["pipx", "upgrade", DIST_NAME], runnable=False)
        return UpgradePlan("pipx", [pipx_path, "upgrade", DIST_NAME], runnable=True)
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
    # Pass the dist name as argv (sys.argv[1]) rather than interpolating it into
    # the code string — keeps the snippet free of string-building even though
    # DIST_NAME is a trusted literal.
    code = (
        "import sys\n"
        "try:\n"
        "    from importlib.metadata import version\n"
        "    sys.stdout.write(version(sys.argv[1]))\n"
        "except Exception:\n"
        "    pass\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code, DIST_NAME],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip()
        return out or None
    except Exception:
        return None


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
]
