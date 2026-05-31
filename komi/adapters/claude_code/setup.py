"""komi-learn — automated install/uninstall/doctor for the Claude Code host.

These functions back the ``komi-learn`` CLI. The design goal is **one command,
no config suffering, and never break the user's agent**:

- Recall needs no model and no auth → it ALWAYS works once hooks are installed.
- Distill is best-effort → it uses a model credential if one is available and
  silently no-ops otherwise. The install never fails just because a model isn't
  reachable.

Everything here is idempotent and reversible: settings.json is backed up before
editing and merged (never clobbered), and ``uninstall`` removes only komi-learn's
own hook entries.

User-facing strings always say "komi-learn" (the Kurikomi product), never "komi".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths

HOOK_EVENTS = ("SessionStart", "Stop", "SubagentStop", "PostCompact")
_HOOK_MODULES = {
    "SessionStart": "komi.adapters.claude_code.hook_recall",
    "Stop": "komi.adapters.claude_code.hook_distill",
    "SubagentStop": "komi.adapters.claude_code.hook_distill",
    # PostCompact re-injects recalled learnings after a /compact (the SessionStart
    # hook also fires with source=compact, but that path's injection is unreliable on
    # current Claude Code — issue #15174 — so we register both). hook_compact routes
    # through hook_recall.main(), which emits the format each event supports.
    "PostCompact": "komi.adapters.claude_code.hook_compact",
}
_HOOK_MARKER = "komi.adapters.claude_code"
# Match a komi hook by COMMAND SHAPE — `... -m komi.adapters.claude_code.<module>` —
# not a bare substring. A loose `"komi.adapters.claude_code" in command` test
# false-positives on an unrelated user hook that merely mentions the module path
# (e.g. `python wrapper.py --note "see komi.adapters.claude_code"`), and would then
# silently overwrite (self-heal) or remove (uninstall) that user hook. The regex
# requires the module to be an actual `-m` target.
_HOOK_CMD_RE = re.compile(r"(?:^|\s)-m\s+komi\.adapters\.claude_code\.\w+(?:\s|$)")


def _is_komi_command(command: str) -> bool:
    return bool(_HOOK_CMD_RE.search(command or ""))


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


@dataclass
class InstallReport:
    steps: list[StepResult] = field(default_factory=list)
    requirements: list = field(default_factory=list)   # list[Requirement]
    gated: bool = False                                 # True = stopped on unmet requirements

    def add(self, name, ok, detail="", fix=""):
        self.steps.append(StepResult(name, ok, detail, fix))

    @property
    def ok(self) -> bool:
        # Install succeeds only when not gated AND the core steps are in place.
        if self.gated:
            return False
        core = {"hooks", "config", "import", "model"}
        present = {s.name for s in self.steps}
        if not ({"hooks", "config"} <= present):
            return False
        return all(s.ok for s in self.steps if s.name in core)


# ── helpers ────────────────────────────────────────────────────────────────

def _python_cmd() -> str:
    """Absolute path to THIS interpreter, quoted if it has spaces.

    Using the absolute path (not bare ``python``) makes hooks robust against PATH
    differences between the install shell and Claude Code's hook runtime — the
    single most common cause of "works for me, not for them"."""
    exe = sys.executable or "python"
    return f'"{exe}"' if " " in exe else exe


def _hook_command(module: str) -> str:
    return f"{_python_cmd()} -m {module}"


def settings_path() -> Path:
    return paths.claude_home() / "settings.json"


# ── install ────────────────────────────────────────────────────────────────

def install(*, pool_repo_url: Optional[str] = None,
            api_key: Optional[str] = None,
            nudge_turns: int = 8,
            allow_incomplete: bool = False) -> InstallReport:
    """Full automated setup, GATED on verified requirements.

    Order matters: we (a) persist an explicitly-supplied API key so the model
    verification can see it, (b) run all REQUIRED checks — including a real model
    call — and if any fails we STOP before touching settings.json and return a
    failing report with exact fixes. Only when requirements pass (or
    ``allow_incomplete``) do we register hooks. No silent degradation at install.
    """
    rep = InstallReport()

    # (a) ensure dirs + persist an explicit API key up front, so verify_model sees it
    try:
        paths.personal_root().mkdir(parents=True, exist_ok=True)
    except Exception as e:
        rep.add("import", False, f"cannot create {paths.personal_root()}: {e}")
        return rep
    if api_key:
        _store_api_key(api_key)

    # (b) THE GATE — verify requirements for real
    from . import requirements as reqmod
    reqs = reqmod.collect(api_key=api_key, pool=bool(pool_repo_url))
    rep.requirements = reqs
    unmet = reqmod.unmet_required(reqs)
    # map the core requirement names onto the report's notion of "ok"
    for r in reqs:
        if r.name in ("python", "claude-cli", "model"):
            rep.add(r.name if r.name != "claude-cli" else "import",
                    r.ok, r.detail, r.fix)

    if unmet and not allow_incomplete:
        rep.gated = True
        return rep  # STOP — do not register hooks on an unmet setup

    # (c) requirements satisfied → perform the install
    rep.steps.append(_install_hooks())
    rep.steps.append(_write_config(pool_repo_url=pool_repo_url, nudge_turns=nudge_turns))
    try:
        from ...pool.identity import Contributor
        c = Contributor(paths.keys_dir())
        rep.add("key", True, f"contributor identity ({c.algo})")
    except Exception as e:
        rep.add("key", False, str(e),
                fix="Optional: needed only to contribute to the pool. pip install pynacl")
    rep.steps.append(_model_status_step(api_key))
    if pool_repo_url or _config_has_pool():
        rep.steps.append(_initial_sync())

    return rep


def _store_api_key(key: str) -> None:
    env_path = paths.personal_root() / ".env"
    lines = []
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("ANTHROPIC_API_KEY=")]
    lines.append(f"ANTHROPIC_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass


def _install_hooks() -> StepResult:
    sp = settings_path()
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if sp.exists():
            # back up BEFORE parsing so a corrupt file is preserved, not lost.
            bak = sp.with_suffix(".json.komi-bak")
            shutil.copy2(sp, bak)
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                # Refuse to overwrite a file we can't parse — clear, specific fix.
                return StepResult("hooks", False,
                                  f"~/.claude/settings.json is not valid JSON: {e}",
                                  fix=f"Repair the JSON (a backup is at {bak.name}), then re-run komi-learn install.")
            if not isinstance(data, dict):
                return StepResult("hooks", False, "settings.json is not a JSON object",
                                  fix="Fix settings.json to be a JSON object, then re-run.")
        hooks = data.setdefault("hooks", {})
        added, refreshed = [], []
        for event in HOOK_EVENTS:
            arr = hooks.setdefault(event, [])
            want = _hook_command(_HOOK_MODULES[event])
            # find an existing komi hook for this event (match by command shape,
            # never a loose substring — see _is_komi_command)
            existing = None
            for entry in arr:
                for h in entry.get("hooks", []):
                    if _is_komi_command(h.get("command", "")):
                        existing = h
                        break
                if existing:
                    break
            if existing is None:
                arr.append({"hooks": [{"type": "command", "command": want}]})
                added.append(event)
            elif existing.get("command") != want:
                # self-heal: a stale command (e.g. bare 'python', or an old repo
                # path) gets upgraded to the canonical absolute-interpreter form.
                existing["command"] = want
                refreshed.append(event)
        if not _atomic_write_json(sp, data):
            return StepResult("hooks", False, "failed to write a valid settings.json",
                              fix=f"Check permissions on {sp}; a backup is at {sp.name}.komi-bak")
        detail = f"hooks set for {', '.join(HOOK_EVENTS)}"
        bits = []
        if added:
            bits.append(f"added: {', '.join(added)}")
        if refreshed:
            bits.append(f"refreshed: {', '.join(refreshed)}")
        detail += f" ({'; '.join(bits)})" if bits else " (already current)"
        return StepResult("hooks", True, detail)
    except Exception as e:
        return StepResult("hooks", False, str(e),
                          fix=f"Manually add a SessionStart hook running: {_hook_command(_HOOK_MODULES['SessionStart'])}")


def _atomic_write_json(path: Path, data: dict) -> bool:
    """Write JSON atomically (temp + os.replace) and verify it parses back. Returns
    False if the write or read-back fails, so callers never claim success on a
    half-written or corrupt settings.json."""
    import tempfile
    try:
        text = json.dumps(data, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        json.loads(path.read_text(encoding="utf-8"))   # verify
        return True
    except Exception:
        return False


def _write_config(*, pool_repo_url: Optional[str], nudge_turns: int) -> StepResult:
    try:
        cpath = paths.personal_root() / "config.json"
        cfg = {}
        if cpath.exists():
            cfg = json.loads(cpath.read_text(encoding="utf-8"))
        cfg.setdefault("nudge_turns", nudge_turns)
        cfg.setdefault("recall_k", 8)
        pool = cfg.setdefault("pool", {})
        if pool_repo_url:
            pool["repo_url"] = pool_repo_url
        pool.setdefault("repo_url", pool.get("repo_url", ""))
        pool.setdefault("mode", "pr")
        pool.setdefault("branch", "main")
        pool.setdefault("require_signature", True)
        pool.setdefault("sync_hours", 12)
        pool.setdefault("auto_contribute", False)
        cpath.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        pr = pool.get("repo_url") or "(not set — personal-only until configured)"
        return StepResult("config", True, f"pool: {pr}")
    except Exception as e:
        return StepResult("config", False, str(e))


def _model_status_step(api_key: Optional[str]) -> StepResult:
    """Re-state the (already-verified) model path for the install summary. The gate
    in :func:`install` has already confirmed a working model with a real call; this
    just reports which one, for the user-facing output."""
    from . import requirements as reqmod
    r = reqmod.verify_model(api_key=api_key)
    return StepResult("model", r.ok, r.detail, r.fix)


def _config_has_pool() -> bool:
    try:
        from . import config as cfg_mod
        return bool(cfg_mod.load().pool_repo_url)
    except Exception:
        return False


def _initial_sync() -> StepResult:
    try:
        from . import config as cfg_mod
        from ...pool.github_backend import GitHubPool, PoolConfig
        cfg = cfg_mod.load()
        if not cfg.pool_enabled:
            return StepResult("pool-sync", True, "pool not configured (skipped)")
        pool = GitHubPool(PoolConfig(repo_url=cfg.pool_repo_url, cache_dir=cfg.pool_cache_dir,
                                     branch=cfg.pool_branch,
                                     require_signature=cfg.pool_require_signature))
        r = pool.sync()
        if r.ok:
            n = len(pool.pull())
            return StepResult("pool-sync", True, f"synced; {n} learning(s) available")
        return StepResult("pool-sync", True, f"sync deferred ({r.detail[:60]})",
                          fix="Will retry automatically on next session start.")
    except Exception as e:
        return StepResult("pool-sync", True, f"sync deferred ({e})")


# ── uninstall ────────────────────────────────────────────────────────────

def uninstall(*, keep_data: bool = True) -> InstallReport:
    """Remove komi-learn's hooks from settings.json. Leaves learnings/config by
    default (pass keep_data=False to also remove ~/.claude/komi)."""
    rep = InstallReport()
    sp = settings_path()
    try:
        if sp.exists():
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                rep.add("hooks", False, f"settings.json is invalid JSON: {e}",
                        fix="Repair it by hand; komi-learn won't edit a file it can't parse.")
                return rep
            hooks = data.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                kept = []
                for entry in hooks[event]:
                    entry_hooks = [h for h in entry.get("hooks", [])
                                   if not _is_komi_command(h.get("command", ""))]
                    if entry_hooks:
                        kept.append({**entry, "hooks": entry_hooks})
                    elif entry.get("hooks"):
                        removed += 1
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event, None)
            if not hooks:
                data.pop("hooks", None)
            if _atomic_write_json(sp, data):
                rep.add("hooks", True, f"removed {removed} komi-learn hook entr(ies)")
            else:
                rep.add("hooks", False, "failed to write settings.json (hooks NOT removed)",
                        fix=f"Check permissions on {sp}")
        else:
            rep.add("hooks", True, "no settings.json")
    except Exception as e:
        rep.add("hooks", False, str(e))

    if not keep_data:
        try:
            shutil.rmtree(paths.personal_root(), ignore_errors=True)
            rep.add("data", True, "removed ~/.claude/komi")
        except Exception as e:
            rep.add("data", False, str(e))
    else:
        rep.add("data", True, "kept your learnings + config (use --purge to remove)")
    return rep


# ── diagnostic capture toggle ──────────────────────────────────────────────

# When capture is ON, the SessionStart + PostCompact hooks point at hook_capture
# (which records the raw payload, then delegates to the normal recall). This lets us
# observe what Claude Code actually sends on a /compact. Distill hooks are untouched.
_CAPTURE_COMMANDS = {
    "SessionStart": "komi.adapters.claude_code.hook_capture",
    "PostCompact": "komi.adapters.claude_code.hook_capture --compact",
}
_NORMAL_COMMANDS = {
    "SessionStart": _HOOK_MODULES["SessionStart"],
    "PostCompact": _HOOK_MODULES["PostCompact"],
}


def _hook_command_raw(module_or_cmd: str) -> str:
    """Build a hook command line; ``module_or_cmd`` may include trailing args."""
    parts = module_or_cmd.split(" ", 1)
    mod, extra = parts[0], (parts[1] if len(parts) > 1 else "")
    cmd = f"{_python_cmd()} -m {mod}"
    return f"{cmd} {extra}".strip() if extra else cmd


def set_capture(enabled: bool) -> StepResult:
    """Re-point (or restore) the SessionStart + PostCompact hooks for diagnostics.
    Idempotent; only touches komi's own hook entries."""
    sp = settings_path()
    try:
        if not sp.exists():
            return StepResult("capture", False, "no settings.json — run komi-learn install first")
        data = json.loads(sp.read_text(encoding="utf-8"))
        hooks = data.setdefault("hooks", {})
        target = _CAPTURE_COMMANDS if enabled else _NORMAL_COMMANDS
        changed = []
        for event, mod in target.items():
            want = _hook_command_raw(mod)
            arr = hooks.setdefault(event, [])
            found = False
            for entry in arr:
                for h in entry.get("hooks", []):
                    if _is_komi_command(h.get("command", "")):
                        if h.get("command") != want:
                            h["command"] = want
                            changed.append(event)
                        found = True
                        break
                if found:
                    break
            if not found:
                arr.append({"hooks": [{"type": "command", "command": want}]})
                changed.append(event)
        if not _atomic_write_json(sp, data):
            return StepResult("capture", False, "failed to write settings.json")
        state = "ON" if enabled else "OFF"
        return StepResult("capture", True,
                          f"capture {state}" + (f" (updated: {', '.join(sorted(set(changed)))})"
                                                if changed else " (already set)"))
    except json.JSONDecodeError as e:
        return StepResult("capture", False, f"settings.json invalid JSON: {e}")
    except Exception as e:
        return StepResult("capture", False, str(e))


__all__ = ["install", "uninstall", "InstallReport", "StepResult", "settings_path",
           "HOOK_EVENTS", "set_capture"]
