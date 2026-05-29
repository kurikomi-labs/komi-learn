"""komi-learn — Claude Code adapter glue for the Curator.

Wires the host-agnostic engine curator to: the user's stores, an LLM consolidator,
a 7-day cadence guard (so it runs rarely), CURATION_REPORT.md output, and the
``komi-learn curate`` command. Runtime-safe: never raises into a hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import paths
from ...engine.store import Store
from ...engine.curator import curate, render_report, DEFAULT_STALE_DAYS, DEFAULT_CONFIDENCE_FLOOR

CURATE_INTERVAL_DAYS = 7.0


def run_curate(*, cwd: str = "", dry_run: bool = False, use_llm: bool = True):
    """Run a curation pass over the personal (and project) store, write the report,
    and return the CurationReport. Safe to call directly (the CLI does) or from a
    detached worker (the cadence trigger does)."""
    # Honor the user's recall.semantic preference here too: the detached curate
    # worker is a fresh process that didn't see the recall hook's env export, so
    # without this a user who disabled semantic would still get semantic clustering.
    try:
        from ..hooklib import _apply_semantic_pref
        _apply_semantic_pref(paths)
    except Exception:
        pass

    consolidator = None
    if use_llm:
        try:
            from .llm import build_consolidator
            consolidator = build_consolidator()
        except Exception:
            consolidator = None

    store = Store(paths.personal_root(), index_path=paths.index_path())
    rep = curate(store, consolidator=consolidator,
                 stale_days=DEFAULT_STALE_DAYS,
                 confidence_floor=DEFAULT_CONFIDENCE_FLOOR,
                 dry_run=dry_run)

    # write the human-readable report (skip on dry-run? keep it — useful preview)
    try:
        report_path = paths.personal_root() / "CURATION_REPORT.md"
        report_path.write_text(render_report(rep), encoding="utf-8")
        rep.notes.append(f"report: {report_path}")
    except Exception:
        pass
    return rep


# ── cadence trigger (called from SessionStart) ─────────────────────────────

def maybe_curate_in_background() -> None:
    """If >= CURATE_INTERVAL_DAYS since the last curation, spawn a detached worker.
    Throttled via state.json. Never blocks session start."""
    try:
        if not _curate_due():
            return
        cmd = [sys.executable, "-m", "komi.adapters.claude_code.curate", "--worker"]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "stdin": subprocess.DEVNULL,
                  "cwd": str(Path(__file__).resolve().parents[3])}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def _curate_due() -> bool:
    """Atomic+locked cadence check (concurrent sessions safe)."""
    now = time.time()

    def _mut(state: dict) -> bool:
        last = float(state.get("last_curated", 0) or 0)
        if now - last < CURATE_INTERVAL_DAYS * 86400:
            return False
        state["last_curated"] = now
        return True

    return bool(paths.update_state(_mut))


def _worker() -> None:
    try:
        run_curate()
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        _worker()
