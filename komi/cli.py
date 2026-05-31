"""komi-learn — the user-facing command-line interface.

The command is ``komi-learn`` (a Kurikomi product). One command to set everything
up; a doctor to diagnose; status/sync/uninstall for the rest. Designed so a new
user runs exactly one thing:

    komi-learn install                 # OR: komi-learn install --api-key sk-... \\
                                       #              --pool https://github.com/kurikomi-labs/komi-pool

and recall starts working immediately, with distillation enabled if a model
credential is available.
"""

from __future__ import annotations

import argparse
import json
import sys

PRODUCT = "komi-learn"

# UTF-8 stdout so status glyphs render on Windows consoles too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_TICK = {"pass": "✓", "warn": "!", "fail": "✗", True: "✓", False: "✗"}


def _p(line: str = "") -> None:
    print(line)


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ── commands ───────────────────────────────────────────────────────────────

def _cmd_install_codex(args) -> int:
    _run_wizard_if_enabled(args)
    from komi.adapters.codex import setup as codex_setup
    _p(f"{PRODUCT}: installing for OpenAI Codex CLI…\n")
    rep = codex_setup.install(pool_repo_url=args.pool, api_key=args.api_key,
                              nudge_turns=args.nudge_turns)
    for s in rep.steps:
        _p(f"  {_TICK[s.ok]} {s.name:8} {s.detail}")
        if s.fix and (not s.ok or s.name in ("model", "trust")):
            _p(f"      → {s.fix}")
    _p()
    if rep.ok:
        _p(f"{PRODUCT} is installed for Codex. Recall activates next session.")
        _p("IMPORTANT: run `codex` then `/hooks` to TRUST the new hooks (Codex requires it).")
        return 0
    _p(f"{PRODUCT}: Codex install incomplete — see ✗ above.")
    return 1


def _run_wizard_if_enabled(args) -> None:
    """Run the interactive setup wizard (unless --no-wizard), folding its answers
    back into args so the real installer uses them."""
    if getattr(args, "no_wizard", False):
        return
    from komi.wizard import run_wizard
    choices = run_wizard(
        host=getattr(args, "host", "claude-code"),
        pool_url=args.pool, api_key=args.api_key, nudge_turns=args.nudge_turns,
        assume_yes=getattr(args, "yes", False),
    )
    args.pool = choices["pool_url"]
    args.nudge_turns = choices["nudge_turns"]


def cmd_install(args) -> int:
    if getattr(args, "host", "claude-code") == "codex":
        return _cmd_install_codex(args)
    _run_wizard_if_enabled(args)
    from komi.adapters.claude_code import setup
    _p(f"{PRODUCT}: checking requirements…\n")
    rep = setup.install(pool_repo_url=args.pool, api_key=args.api_key,
                        nudge_turns=args.nudge_turns,
                        allow_incomplete=args.allow_incomplete)

    # If the gate stopped us, show requirements + exact fixes and bail (non-zero).
    if rep.gated:
        for r in rep.requirements:
            tag = "REQUIRED" if r.required else "optional"
            _p(f"  {_TICK[r.ok]} {r.name:12} [{tag}] {r.detail}")
        _p()
        _p(f"{PRODUCT}: setup is incomplete — nothing was installed (your settings are untouched).")
        _p("Fix the REQUIRED item(s) below, then re-run  komi-learn install :\n")
        for r in rep.requirements:
            if r.required and not r.ok and r.fix:
                _p(f"  • {r.name}:")
                for ln in r.fix.split("\n"):
                    _p(f"      {ln}")
        _p("\n(Advanced: --allow-incomplete installs anyway, but distillation won't work until fixed.)")
        return 1

    for s in rep.steps:
        _p(f"  {_TICK[s.ok]} {s.name:12} {s.detail}")
        if not s.ok and s.fix:
            _p(f"      → {s.fix}")
    _p()
    if rep.ok:
        _p(f"{PRODUCT} is installed and verified. Recall + distillation are active in your")
        _p("next Claude Code session — no commands needed.")
        if not args.pool:
            _p("Tip: join the global pool with  komi-learn install --pool <repo-url>")
        _p("Check anytime with:  komi-learn doctor")
        return 0
    _p(f"{PRODUCT}: installed with --allow-incomplete; some features are off until requirements are met.")
    return 0 if args.allow_incomplete else 1


def _host_paths(host: str):
    if host == "codex":
        from komi.adapters.codex import paths
    else:
        from komi.adapters.claude_code import paths
    return paths


def cmd_config(args) -> int:
    """Tinker settings AFTER install — interactive menu, or `config show` / `config set`."""
    from komi.adapters import config_io
    from komi import cli_prompt as PR
    paths = _host_paths(getattr(args, "host", "claude-code"))
    data = config_io.load_raw(paths)

    if getattr(args, "action", None) == "show":
        _p(json.dumps(data, indent=2) if data else "(no config yet — run komi-learn install)")
        return 0

    if getattr(args, "action", None) == "set":
        config_io.set_key(data, args.key, args.value)
        config_io.save_raw(paths, data)
        _p(f"{PRODUCT}: set {args.key} = {config_io.get_key(data, args.key)}")
        return 0

    # interactive menu
    _p(f"{PRODUCT} config — change anything anytime (Enter keeps current).\n")
    # pool
    cur_pool = (data.get("pool", {}) or {}).get("repo_url", "")
    if PR.ask_yes_no("Join / stay in the community pool?", default=bool(cur_pool),
                     summary="Receive useful shared tips + queue your anonymized ones (you approve each share)."):
        from komi.wizard import DEFAULT_POOL_URL
        url = PR.ask_text("Pool repo URL", default=cur_pool or DEFAULT_POOL_URL)
        config_io.set_key(data, "pool.repo_url", url)
        config_io.set_key(data, "pool.require_signature", True)
    else:
        config_io.set_key(data, "pool.repo_url", "")
    # semantic
    cur_sem = (data.get("recall", {}) or {}).get("semantic", True)
    want_sem = PR.ask_yes_no("Use semantic (meaning-based) recall?", default=cur_sem,
                             summary="Smarter recall via a local model (falls back to keyword search if off/unavailable).")
    config_io.set_key(data, "recall.semantic", want_sem)
    if want_sem:
        from komi import model_install
        if not model_install.is_installed():
            if PR.ask_yes_no("Download the model now (~300MB)?", default=True):
                ok, detail = model_install.install_model(quiet=True)
                _p(f"    {'ready' if ok else 'install failed: ' + detail}")
    # cadence
    cur_turns = data.get("nudge_turns", 8)
    turns = PR.ask_text("Distill every N turns", default=str(cur_turns),
                        summary="How often it learns from a session in the background.")
    config_io.set_key(data, "nudge_turns", turns)

    config_io.save_raw(paths, data)
    _p(f"\n{PRODUCT}: saved. Run `komi-learn doctor` to confirm.")
    return 0


def cmd_doctor(args) -> int:
    from komi.adapters.claude_code.doctor import run_doctor
    _p(f"{PRODUCT} doctor:\n")
    checks = run_doctor()
    failed = []
    for c in checks:
        _p(f"  {_TICK[c.status]} {c.name:13} {c.detail}")
        if c.status != "pass" and c.fix:
            _p(f"      → {c.fix}")
        if c.status == "fail":
            failed.append(c.name)
    _p()
    if failed:
        recall_critical = {"install", "hooks", "config"} & set(failed)
        if recall_critical:
            _p(f"{PRODUCT}: recall is NOT working — fix the failed item(s) above.")
        else:
            _p(f"{PRODUCT}: recall works, but the full loop is incomplete "
               f"({', '.join(failed)} failed). Fix the item(s) above for distillation.")
        return 1
    _p(f"{PRODUCT}: healthy — recall and distillation are both verified working.")
    return 0


def cmd_status(args) -> int:
    from komi.adapters.claude_code import config as cfg_mod, paths
    from komi.engine.store import Store
    cfg = cfg_mod.load()
    _p(f"{PRODUCT} status")
    _p(f"  home:        {paths.personal_root()}")
    _p(f"  pool:        {cfg.pool_repo_url or '(not configured)'}")
    _p(f"  nudge_turns: {cfg.nudge_turns}   sync_hours: {cfg.pool_sync_hours}")
    try:
        s = Store(paths.personal_root(), index_path=paths.index_path())
        learns = s.all()
        by_scope = {}
        for l in learns:
            by_scope[l.scope] = by_scope.get(l.scope, 0) + 1
        s.close()
        _p(f"  learnings:   {len(learns)}  ({', '.join(f'{k}:{v}' for k,v in by_scope.items()) or 'none yet'})")
        # corpus health — the honest 'drift' signal for a model-less system
        from komi.engine.curator import corpus_health
        h = corpus_health(learns)
        if h["active"]:
            _p(f"  health:      avg-confidence {h['avg_confidence']}, "
               f"{int(h['stale_share']*100)}% stale-unused, {h['never_reused']} never reused")
            if h["stale_share"] >= 0.5:
                _p(f"               (high stale share — run `komi-learn curate` to consolidate/archive)")
    except Exception as e:
        _p(f"  learnings:   (unavailable: {e})")
    return 0


def cmd_sync(args) -> int:
    from komi.adapters.claude_code import config as cfg_mod
    from komi.pool.github_backend import GitHubPool, PoolConfig
    cfg = cfg_mod.load()
    if not cfg.pool_enabled:
        _p(f"{PRODUCT}: no pool configured. Set one with: komi-learn install --pool <repo-url>")
        return 1
    pool = GitHubPool(PoolConfig(repo_url=cfg.pool_repo_url, cache_dir=cfg.pool_cache_dir,
                                 branch=cfg.pool_branch, require_signature=cfg.pool_require_signature))
    r = pool.sync()
    if r.ok:
        n = len(pool.pull())
        _p(f"{PRODUCT}: synced. {n} learning(s) available from the pool.")
        return 0
    _p(f"{PRODUCT}: sync failed — {r.detail}")
    return 1


def cmd_curate(args) -> int:
    """Run the consolidation pass now (normally automatic ~weekly)."""
    from komi.adapters.claude_code.curate import run_curate
    mode = "preview (no changes)" if args.dry_run else "live"
    _p(f"{PRODUCT}: curating — {mode}…\n")
    rep = run_curate(dry_run=args.dry_run, use_llm=not args.no_llm)
    _p(f"  {rep.summary()}")
    if rep.consolidated:
        for c in rep.consolidated:
            _p(f"    • umbrella: {c['umbrella']}  (absorbed {len(c['absorbed'])})")
    elif rep.clusters:
        _p(f"    • {len(rep.clusters)} cluster(s) flagged "
           + ("(merge needs a model — run without --no-llm)" if args.no_llm else ""))
    if rep.pruned:
        _p(f"    • archived {len(rep.pruned)} stale learning(s) (recoverable)")
    report = paths_report()
    if report:
        _p(f"\n  full report: {report}")
    return 0


def paths_report():
    try:
        from komi.adapters.claude_code import paths
        p = paths.personal_root() / "CURATION_REPORT.md"
        return p if p.exists() else None
    except Exception:
        return None


def cmd_login(args) -> int:
    """Convenience: log in for free OAuth distillation via the claude CLI."""
    import shutil
    import subprocess
    if not shutil.which("claude"):
        _p(f"{PRODUCT}: the `claude` CLI isn't installed, so OAuth login isn't available here.")
        _p("Install Claude Code, or enable distillation with an API key:")
        _p("  komi-learn install --api-key sk-ant-...")
        return 1
    _p(f"{PRODUCT}: launching `claude auth login` (uses your Claude.ai subscription)…\n")
    try:
        rc = subprocess.call(["claude", "auth", "login"], timeout=300)
    except subprocess.TimeoutExpired:
        _p(f"\n{PRODUCT}: login timed out after 5 minutes. Run `claude auth login` directly, then `komi-learn doctor`.")
        return 1
    except Exception as e:
        _p(f"{PRODUCT}: could not launch login — {e}")
        return 1
    if rc == 0:
        _p(f"\n{PRODUCT}: logged in. Distillation will use OAuth (no API key, no per-call cost).")
        _p("Verify with:  komi-learn doctor")
    return rc


def cmd_update(args) -> int:
    """Self-update: check PyPI for a newer komi-learn and upgrade in place.

    Upgrades *this* interpreter's install (the one the hooks import), via pip or
    pipx depending on how komi-learn was installed. Use --check to only report
    whether an update is available. After upgrading, re-run `komi-learn install`
    is NOT required for code — but if a release adds new hook events, run it to
    refresh your settings."""
    import komi
    from komi import updater
    from komi import cli_prompt as PR

    current = getattr(komi, "__version__", "?")
    _p(f"{PRODUCT}: installed {current}. Checking PyPI…")
    latest = updater.check_latest()
    if latest is None:
        _p(f"{PRODUCT}: couldn't reach PyPI (offline?). Try again later, or upgrade manually:")
        _p(f"      {updater.plan_upgrade().display()}")
        return 1
    if not updater.is_newer(latest, current):
        _p(f"{PRODUCT}: you're on the latest version ({current}). Nothing to do.")
        return 0

    _p(f"{PRODUCT}: a newer version is available — {current} → {latest}.")
    plan = updater.plan_upgrade()

    if getattr(args, "check", False):
        _p(f"  to upgrade:  {plan.display()}")
        return 0

    if not plan.runnable:
        # Couldn't identify a safe package manager — never guess, just instruct.
        _p(f"  couldn't auto-detect how {PRODUCT} was installed. Upgrade with:")
        _p(f"      {plan.display()}")
        return 1

    if not getattr(args, "yes", False):
        if not PR.ask_yes_no(f"  Upgrade now via {plan.manager}?", default=True,
                             summary=f"Runs: {plan.display()}"):
            _p(f"  skipped. Upgrade anytime with:  {plan.display()}")
            return 0

    _p(f"\n  upgrading via {plan.manager}…\n")
    ok, detail = updater.run_upgrade(plan)
    if not ok:
        _p(f"\n{PRODUCT}: upgrade failed ({detail}). You can run it manually:")
        _p(f"      {plan.display()}")
        return 1

    # importlib.metadata is cached in this process; read the truth from a fresh one.
    # Only claim a confirmed version when we actually read one — a non-zero pip
    # exit isn't proof komi-learn reached `latest` (pip can no-op or install a
    # pinned older version), so never substitute `latest` and call it confirmed.
    new = updater.installed_version_via_subprocess()
    if new is None:
        _p(f"\n{PRODUCT}: upgrade command finished, but I couldn't confirm the "
           "installed version.")
        _p("  Check it with:  komi-learn update --check")
    else:
        _p(f"\n{PRODUCT}: upgraded {current} → {new}.")
        if updater.is_newer(latest, new):
            _p(f"  note: PyPI shows {latest} but the install reports {new} — "
               "you may be in a different environment than expected.")

    # The CLI upgrade above touched THIS interpreter. But the coding agent's behavior
    # (recall/distill/compaction) is whatever the installed HOOKS import — a possibly
    # different Python. Verify the new code actually reached the agent, not just the
    # CLI, so "the behavior is updated" is a fact, not a hope.
    _verify_agent_updated(new or latest)
    return 0


def _verify_agent_updated(expected: str) -> None:
    """Confirm the agent's hook interpreter(s) now import the upgraded komi-learn.

    The hooks run `python -m komi.adapters.claude_code....` with a pinned interpreter
    (see setup._python_cmd). `pip install -U` only upgrades the env it ran in; if the
    hooks point at a different Python, the CLI is new but the AGENT is still old. We
    ask each hook interpreter what version it imports and report the truth."""
    from komi import updater
    try:
        from komi.adapters.claude_code import setup
        interps = setup.hook_interpreters()
    except Exception:
        return
    if not interps:
        _p("  (no Claude Code hooks installed yet — run  komi-learn install  to enable the agent.)")
        return

    import os as _os
    stale = []
    for interp in interps:
        ver = updater.installed_version_via_subprocess(interp)
        same_as_cli = _os.path.normcase(interp) == _os.path.normcase(sys.executable)
        if ver == expected:
            who = "this interpreter" if same_as_cli else interp
            _p(f"  ✓ agent behavior updated — hooks run {expected} (via {who}).")
        else:
            stale.append((interp, ver))

    if stale:
        _p("  ! the coding agent's hooks use a DIFFERENT Python than the one just "
           "upgraded — the agent is still on the old code:")
        for interp, ver in stale:
            _p(f"      {interp}  (imports komi-learn {ver or 'not installed'})")
        # one clear fix line per stale interpreter
        for interp, _ in stale:
            _p(f"      fix:  \"{interp}\" -m pip install --upgrade {updater.DIST_NAME}")
        _p("    Then the agent picks up the new behavior on its next hook firing.")


def cmd_capture(args) -> int:
    """Diagnostic: capture the raw payloads Claude Code sends to the SessionStart +
    PostCompact hooks, to verify compaction re-injection works on this host.

    `on` re-points those hooks at a recorder (recall still works); run /compact in a
    real Claude Code session; `show` prints what fired; `off` restores normal hooks."""
    from komi.adapters.claude_code import setup, hook_capture
    action = getattr(args, "capture_action", None) or "show"

    if action in ("on", "off"):
        r = setup.set_capture(action == "on")
        _p(f"  {_TICK[r.ok]} {r.detail}")
        if r.ok and action == "on":
            _p("\n  Now: open Claude Code, work a little, then run /compact.")
            _p("  After that:  komi-learn capture show")
            _p("  Restore normal hooks anytime:  komi-learn capture off")
        return 0 if r.ok else 1

    # show
    p = hook_capture.capture_path()
    if not p.exists():
        _p("  no captures yet. Enable with `komi-learn capture on`, then /compact in Claude Code.")
        return 0
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as e:
        _p(f"  could not read {p}: {e}")
        return 1
    if not lines:
        _p("  capture file is empty.")
        return 0
    _p(f"  {len(lines)} captured hook event(s) from {p}:\n")
    for ln in lines[-20:]:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        _p(f"  • entry={rec.get('entry_event')}  hook_event_name={rec.get('hook_event_name')!r}  "
           f"source={rec.get('source')!r}  trigger={rec.get('trigger')!r}")
        _p(f"      keys={rec.get('parsed_keys')}  session_id={rec.get('session_id')!r}")
    _p("\n  (entry = which komi entry point ran; hook_event_name/source/trigger = what the host sent)")
    return 0


def cmd_queue(args) -> int:
    """Inspect + act on the global-contribution review queue (the human gate).

    Nothing reaches the public pool without passing through here. `list` shows what's
    pending; `approve <i>` signs it (binding your GitHub username) and opens a PR;
    `reject <i>` discards it (kept for audit, never published)."""
    from komi import cli_prompt as PR
    from komi.pool.queue import list_queue, set_status, publish_approved
    paths = _host_paths(getattr(args, "host", "claude-code"))
    pending = list_queue(paths.queue_dir(), status="pending-review")
    action = getattr(args, "queue_action", None) or "list"

    if action == "list":
        if not pending:
            _p("  review queue is empty — nothing waiting to be shared.")
            return 0
        _p(f"  {len(pending)} learning(s) pending your approval for the global pool:\n")
        for i, item in enumerate(pending):
            _p(f"  [{i}] {item.learning.title}")
            _p(f"      {_clip(item.learning.body, 100)}")
            _p(f"      {item.learning.category} · id {item.learning.id[:18]}…\n")
        _p("  Approve:  komi-learn queue approve <index>   (opens a PR)")
        _p("  Reject:   komi-learn queue reject <index>")
        return 0

    idx = getattr(args, "index", None)
    if idx is None or idx < 0 or idx >= len(pending):
        _p(f"  no queued item at index {idx}. Run `komi-learn queue list` for indices.")
        return 1
    item = pending[idx]

    if action == "reject":
        set_status(item, "rejected")
        _p(f"  rejected: {item.learning.title}")
        return 0

    # approve → mark approved, then sign (with github_user) + publish via the pool
    from komi.adapters.claude_code import config as cfg_mod
    from komi.pool.identity import Contributor
    from komi.pool.github_backend import GitHubPool, PoolConfig
    cfg = cfg_mod.load()
    if not cfg.pool_enabled:
        _p("  no pool configured (set pool.repo_url) — can't publish.")
        return 1
    gh_user = getattr(cfg, "pool_github_user", "") or ""
    if not gh_user and not PR.ask_yes_no(
            "  No GitHub username set (contribution won't be account-verified). "
            "Contribute anyway?", default=False):
        _p("  Set it with:  komi-learn config set pool.github_user <you>")
        return 1
    set_status(item, "approved")
    pool = GitHubPool(PoolConfig(repo_url=cfg.pool_repo_url, cache_dir=cfg.pool_cache_dir,
                                 branch=cfg.pool_branch, mode=cfg.pool_mode,
                                 require_signature=cfg.pool_require_signature))
    results = publish_approved(paths.queue_dir(), pool, Contributor(paths.keys_dir()),
                               only_id=item.id, github_user=gh_user)
    res = results[0] if results else {"published": False, "reason": "not-processed"}
    if res.get("published"):
        _p(f"  published: {item.learning.title}")
        if res.get("pr_url"):
            _p(f"    PR: {res['pr_url']}")
        return 0
    _p(f"  publish failed: {res.get('reason') or res.get('detail')}  "
       "(still approved; try `komi-learn sync` then retry)")
    return 1


def cmd_forget(args) -> int:
    """Erase learnings matching a query or id (the 'right to be forgotten' path).

    Local learnings are archived by default (recoverable) or hard-deleted with
    --hard. A learning already shared to the public pool can't be unilaterally
    erased — it's archived locally and the removal-PR path is printed."""
    from komi import cli_prompt as PR
    from komi.engine.store import Store
    paths = _host_paths(getattr(args, "host", "claude-code"))
    store = Store(paths.personal_root(), index_path=paths.index_path())
    query = (getattr(args, "query", "") or "").strip()
    if not query:
        _p("  usage: komi-learn forget <text-or-id>   [--hard]")
        return 1

    matches = [l for l in store.all()
               if l.lifecycle.state == "active"
               and query.lower() in f"{l.id} {l.title} {l.body} {l.trigger}".lower()]
    if not matches:
        _p(f"  no active learnings match {query!r}.")
        return 0

    _p(f"  {len(matches)} learning(s) match {query!r}:")
    for l in matches:
        _p(f"    - {l.title}  ({l.scope}, id {l.id[:16]}…)")
    hard = getattr(args, "hard", False)
    if not PR.ask_yes_no(f"  {'DELETE permanently' if hard else 'archive (recoverable)'} "
                         f"these {len(matches)}?", default=False):
        _p("  cancelled.")
        return 0

    for l in matches:
        if l.scope == "global" and l.provenance.origin == "pool":
            _p(f"  ! '{l.title}' came from the public pool — archived locally; to remove")
            _p("    it from the shared pool, open a PR deleting its file in the pool repo.")
            store.archive(l.id)
        elif hard:
            store.delete(l.id)
        else:
            store.archive(l.id)
    _p(f"  {'deleted' if hard else 'archived'} {len(matches)} learning(s).")
    return 0


def cmd_uninstall(args) -> int:
    if getattr(args, "host", "claude-code") == "codex":
        from komi.adapters.codex import setup as codex_setup
        rep = codex_setup.uninstall(keep_data=not args.purge)
    else:
        from komi.adapters.claude_code import setup
        rep = setup.uninstall(keep_data=not args.purge)
    for s in rep.steps:
        _p(f"  {_TICK[s.ok]} {s.name:8} {s.detail}")
    _p(f"\n{PRODUCT}: uninstalled hooks." +
       ("" if args.purge else " Your learnings were kept (use --purge to remove)."))
    return 0


# ── parser ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PRODUCT,
        description="komi-learn — a continuous, zero-friction learning layer for AI agents (by Kurikomi).",
    )
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("install", help="set up komi-learn for a host (Claude Code or Codex)")
    pi.add_argument("--host", choices=["claude-code", "codex"], default="claude-code",
                    help="which agent host to install for (default: claude-code)")
    pi.add_argument("--pool", metavar="URL", default=None,
                    help="global pool repo URL (e.g. https://github.com/kurikomi-labs/komi-pool)")
    pi.add_argument("--api-key", metavar="KEY", default=None,
                    help="model API key for distillation (Anthropic for claude-code, OpenAI for codex)")
    pi.add_argument("--nudge-turns", type=int, default=8,
                    help="distill every N turns (default 8)")
    pi.add_argument("--allow-incomplete", action="store_true",
                    help="install even if required checks fail (distillation may not work)")
    pi.add_argument("--yes", "-y", action="store_true",
                    help="accept all wizard defaults (pool on, semantic on) — for scripts")
    pi.add_argument("--no-wizard", action="store_true",
                    help="skip the interactive wizard; use flags/defaults only")
    pi.set_defaults(func=cmd_install)

    pd = sub.add_parser("doctor", help="diagnose the install and suggest fixes")
    pd.set_defaults(func=cmd_doctor)

    pcfg = sub.add_parser("config", help="change settings anytime (menu, or show/set)")
    pcfg.add_argument("--host", choices=["claude-code", "codex"], default="claude-code")
    csub = pcfg.add_subparsers(dest="action")
    csub.add_parser("show", help="print the current config")
    cset = csub.add_parser("set", help="set a key (e.g. pool.repo_url, recall.semantic, nudge_turns)")
    cset.add_argument("key")
    cset.add_argument("value")
    pcfg.set_defaults(func=cmd_config)

    ps = sub.add_parser("status", help="show config + learning counts")
    ps.set_defaults(func=cmd_status)

    py = sub.add_parser("sync", help="sync the global pool now")
    py.set_defaults(func=cmd_sync)

    pl = sub.add_parser("login", help="log in for free OAuth distillation (claude CLI)")
    pl.set_defaults(func=cmd_login)

    pup = sub.add_parser("update", help="check PyPI and upgrade komi-learn to the latest version")
    pup.add_argument("--check", action="store_true",
                     help="only report whether an update is available; don't upgrade")
    pup.add_argument("--yes", "-y", action="store_true",
                     help="upgrade without the confirmation prompt")
    pup.set_defaults(func=cmd_update)

    pcap = sub.add_parser("capture",
                          help="diagnostic: record what Claude Code sends on /compact")
    capsub = pcap.add_subparsers(dest="capture_action")
    capsub.add_parser("on", help="re-point SessionStart+PostCompact hooks at the recorder")
    capsub.add_parser("off", help="restore the normal hooks")
    capsub.add_parser("show", help="print captured hook payloads (default)")
    pcap.set_defaults(func=cmd_capture)

    pc = sub.add_parser("curate", help="consolidate the learning library now (normally ~weekly)")
    pc.add_argument("--dry-run", action="store_true", help="preview changes without applying")
    pc.add_argument("--no-llm", action="store_true", help="prune only; don't merge clusters")
    pc.set_defaults(func=cmd_curate)

    pq = sub.add_parser("queue", help="review/approve/reject pending pool contributions")
    pq.add_argument("--host", choices=["claude-code", "codex"], default="claude-code")
    qsub = pq.add_subparsers(dest="queue_action")
    qsub.add_parser("list", help="show learnings awaiting your approval (default)")
    qa = qsub.add_parser("approve", help="sign + open a PR for a queued learning")
    qa.add_argument("index", type=int, help="index from `queue list`")
    qr = qsub.add_parser("reject", help="drop a queued learning")
    qr.add_argument("index", type=int, help="index from `queue list`")
    pq.set_defaults(func=cmd_queue)

    pf = sub.add_parser("forget", help="erase learnings matching a query or id")
    pf.add_argument("query", help="text or id to match")
    pf.add_argument("--hard", action="store_true",
                    help="permanently delete (default: archive, recoverable)")
    pf.add_argument("--host", choices=["claude-code", "codex"], default="claude-code")
    pf.set_defaults(func=cmd_forget)

    pu = sub.add_parser("uninstall", help="remove komi-learn hooks (keeps data)")
    pu.add_argument("--host", choices=["claude-code", "codex"], default="claude-code",
                    help="which host to uninstall from (default: claude-code)")
    pu.add_argument("--purge", action="store_true", help="also delete the komi data dir")
    pu.set_defaults(func=cmd_uninstall)

    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
