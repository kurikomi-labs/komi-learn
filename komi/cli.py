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


# ── commands ───────────────────────────────────────────────────────────────

def cmd_install(args) -> int:
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


def cmd_uninstall(args) -> int:
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

    pi = sub.add_parser("install", help="set up komi-learn for Claude Code (one command)")
    pi.add_argument("--pool", metavar="URL", default=None,
                    help="global pool repo URL (e.g. https://github.com/kurikomi-labs/komi-pool)")
    pi.add_argument("--api-key", metavar="KEY", default=None,
                    help="Anthropic API key for distillation (else uses env / claude CLI)")
    pi.add_argument("--nudge-turns", type=int, default=8,
                    help="distill every N turns (default 8)")
    pi.add_argument("--allow-incomplete", action="store_true",
                    help="install even if required checks fail (distillation may not work)")
    pi.set_defaults(func=cmd_install)

    pd = sub.add_parser("doctor", help="diagnose the install and suggest fixes")
    pd.set_defaults(func=cmd_doctor)

    ps = sub.add_parser("status", help="show config + learning counts")
    ps.set_defaults(func=cmd_status)

    py = sub.add_parser("sync", help="sync the global pool now")
    py.set_defaults(func=cmd_sync)

    pl = sub.add_parser("login", help="log in for free OAuth distillation (claude CLI)")
    pl.set_defaults(func=cmd_login)

    pc = sub.add_parser("curate", help="consolidate the learning library now (normally ~weekly)")
    pc.add_argument("--dry-run", action="store_true", help="preview changes without applying")
    pc.add_argument("--no-llm", action="store_true", help="prune only; don't merge clusters")
    pc.set_defaults(func=cmd_curate)

    pu = sub.add_parser("uninstall", help="remove komi-learn hooks (keeps data)")
    pu.add_argument("--purge", action="store_true", help="also delete ~/.claude/komi")
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
