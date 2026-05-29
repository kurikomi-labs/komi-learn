"""komi-learn — install/uninstall/doctor for the OpenAI Codex CLI host.

Mirrors the Claude Code installer but targets Codex: hooks register in
``~/.codex/hooks.json`` (same JSON schema, conveniently), config + key live under
``~/.codex/komi``. Reuses the shared engine + the same safety properties
(backup, merge-not-clobber, idempotent, absolute interpreter path, atomic write).

One Codex-specific note surfaced to the user: Codex requires hooks to be *trusted*
via its ``/hooks`` command (or ``--dangerously-bypass-hook-trust``) before they
run — komi-learn can't trust them for you, so install tells you to do it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths

HOOK_EVENTS = ("SessionStart", "Stop", "SubagentStop")
_HOOK_MODULES = {
    "SessionStart": "komi.adapters.codex.hook_recall",
    "Stop": "komi.adapters.codex.hook_distill",
    "SubagentStop": "komi.adapters.codex.hook_distill",
}
_HOOK_MARKER = "komi.adapters.codex"


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


@dataclass
class InstallReport:
    steps: list[StepResult] = field(default_factory=list)

    def add(self, name, ok, detail="", fix=""):
        self.steps.append(StepResult(name, ok, detail, fix))

    @property
    def ok(self) -> bool:
        core = {"import", "hooks", "config"}
        present = {s.name for s in self.steps}
        return ({"hooks", "config"} <= present and
                all(s.ok for s in self.steps if s.name in core))


def _python_cmd() -> str:
    exe = sys.executable or "python"
    return f'"{exe}"' if " " in exe else exe


def _hook_command(module: str) -> str:
    return f"{_python_cmd()} -m {module}"


def _atomic_write_json(path: Path, data: dict) -> bool:
    try:
        text = json.dumps(data, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def install(*, pool_repo_url: Optional[str] = None, api_key: Optional[str] = None,
            nudge_turns: int = 8) -> InstallReport:
    rep = InstallReport()
    try:
        import komi  # noqa: F401
        rep.add("import", True, f"komi-learn importable ({sys.executable})")
    except Exception as e:
        rep.add("import", False, str(e), fix="pip install komi-learn")
        return rep

    paths.personal_root().mkdir(parents=True, exist_ok=True)
    if api_key:
        _store_openai_key(api_key)

    rep.steps.append(_install_hooks())
    rep.steps.append(_write_config(pool_repo_url=pool_repo_url, nudge_turns=nudge_turns))
    try:
        from ...pool.identity import Contributor
        c = Contributor(paths.keys_dir())
        rep.add("key", True, f"contributor identity ({c.algo})")
    except Exception as e:
        rep.add("key", False, str(e), fix="Optional (pool contributions): pip install pynacl")
    # model is best-effort here (Codex live auth can't be exercised from a sandbox)
    rep.add("model", True, _model_status())
    rep.add("trust", True,
            "Codex requires trusting new hooks — run `codex` then `/hooks` to trust them "
            "(or `codex --dangerously-bypass-hook-trust` for automation).")
    return rep


def _install_hooks() -> StepResult:
    hp = paths.hooks_path()
    try:
        data = {}
        if hp.exists():
            shutil.copy2(hp, hp.with_suffix(".json.komi-bak"))
            try:
                data = json.loads(hp.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                return StepResult("hooks", False, f"{hp} is invalid JSON: {e}",
                                  fix="Repair the JSON (backup at .komi-bak) then re-run.")
            if not isinstance(data, dict):
                return StepResult("hooks", False, "hooks.json is not a JSON object")
        hooks = data.setdefault("hooks", {})
        added, refreshed = [], []
        for event in HOOK_EVENTS:
            arr = hooks.setdefault(event, [])
            want = _hook_command(_HOOK_MODULES[event])
            existing = next((h for entry in arr for h in entry.get("hooks", [])
                             if _HOOK_MARKER in h.get("command", "")), None)
            if existing is None:
                arr.append({"hooks": [{"type": "command", "command": want}]})
                added.append(event)
            elif existing.get("command") != want:
                existing["command"] = want
                refreshed.append(event)
        if not _atomic_write_json(hp, data):
            return StepResult("hooks", False, "failed to write ~/.codex/hooks.json")
        bits = ([f"added: {', '.join(added)}"] if added else []) + \
               ([f"refreshed: {', '.join(refreshed)}"] if refreshed else [])
        return StepResult("hooks", True,
                          f"~/.codex/hooks.json set" + (f" ({'; '.join(bits)})" if bits else " (already current)"))
    except Exception as e:
        return StepResult("hooks", False, str(e))


def _write_config(*, pool_repo_url: Optional[str], nudge_turns: int) -> StepResult:
    try:
        cpath = paths.personal_root() / "config.json"
        cfg = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
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
        cpath.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return StepResult("config", True, f"pool: {pool.get('repo_url') or '(personal-only)'}")
    except Exception as e:
        return StepResult("config", False, str(e))


def _store_openai_key(key: str) -> None:
    env_path = paths.personal_root() / ".env"
    lines = []
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("OPENAI_API_KEY=")]
    lines.append(f"OPENAI_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass


def _model_status() -> str:
    if os.environ.get("OPENAI_API_KEY") or (paths.personal_root() / ".env").exists():
        return "OpenAI API key available for distillation"
    if shutil.which("codex"):
        return "no OPENAI_API_KEY; distillation needs one — komi-learn install --host codex --api-key sk-..."
    return "no model credential — recall works; distillation OFF"


def uninstall(*, keep_data: bool = True) -> InstallReport:
    rep = InstallReport()
    hp = paths.hooks_path()
    try:
        if hp.exists():
            data = json.loads(hp.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {})
            removed = 0
            for ev in list(hooks.keys()):
                kept = []
                for entry in hooks[ev]:
                    eh = [h for h in entry.get("hooks", []) if _HOOK_MARKER not in h.get("command", "")]
                    if eh:
                        kept.append({**entry, "hooks": eh})
                    elif entry.get("hooks"):
                        removed += 1
                if kept:
                    hooks[ev] = kept
                else:
                    hooks.pop(ev, None)
            if not hooks:
                data.pop("hooks", None)
            _atomic_write_json(hp, data)
            rep.add("hooks", True, f"removed {removed} komi-learn hook entr(ies)")
        else:
            rep.add("hooks", True, "no ~/.codex/hooks.json")
    except Exception as e:
        rep.add("hooks", False, str(e))
    if not keep_data:
        shutil.rmtree(paths.personal_root(), ignore_errors=True)
        rep.add("data", True, "removed ~/.codex/komi")
    else:
        rep.add("data", True, "kept learnings + config (--purge to remove)")
    return rep


__all__ = ["install", "uninstall", "InstallReport", "StepResult"]
