"""Diagnostic hook — capture the RAW payload Claude Code sends, then behave normally.

Purpose: the compaction re-injection feature is built on assumptions about which
events fire on a ``/compact`` and what field names they carry (``hook_event_name``,
``source`` vs ``trigger``, whether ``additionalContext`` / plain stdout actually
reach the model). We have never observed a real payload. This hook records the exact
stdin we receive — keyed by which entry point invoked it — to a JSONL file, then
delegates to the normal recall path so the session is unaffected.

Enable it with ``komi-learn capture on`` (which re-points the SessionStart +
PostCompact hooks here), run ``/compact`` in a real Claude Code session, then
``komi-learn capture show`` to inspect what actually fired. ``komi-learn capture
off`` restores the normal hooks.

Two entry points so we can tell SessionStart from PostCompact even if the payload
omits the event name:
  ``python -m komi.adapters.claude_code.hook_capture``            (SessionStart)
  ``python -m komi.adapters.claude_code.hook_capture --compact``  (PostCompact)
"""

from __future__ import annotations

import json
import sys
import time

from . import paths


def capture_path():
    return paths.personal_root() / "_hook_capture.jsonl"


def _capture(entry_event: str, raw: str) -> None:
    """Append one capture record. Best-effort; never raises into the hook."""
    try:
        parsed = None
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = None
        rec = {
            "ts": time.time(),
            "entry_event": entry_event,                 # which entry point ran (authoritative)
            "raw_len": len(raw),
            "raw": raw[:8192],                          # cap; payloads are tiny
            "parsed_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else None,
            "hook_event_name": (parsed or {}).get("hook_event_name") if isinstance(parsed, dict) else None,
            "source": (parsed or {}).get("source") if isinstance(parsed, dict) else None,
            "trigger": (parsed or {}).get("trigger") if isinstance(parsed, dict) else None,
            "session_id": (parsed or {}).get("session_id") if isinstance(parsed, dict) else None,
        }
        p = capture_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main(default_event: str = "") -> int:
    # Read stdin ONCE, capture it, then hand the same payload to the real recall
    # path so behavior is unchanged. We re-feed stdin by monkeypatching the reader.
    # Mirror hooklib's stdin bound (set_capture re-points the real events here, so the
    # cap must not be lost on these routes).
    from . import hook_recall
    from .. import hooklib
    raw = ""
    try:
        raw = sys.stdin.read(hooklib._MAX_STDIN_BYTES + 1)
        if len(raw) > hooklib._MAX_STDIN_BYTES:
            raw = ""                      # oversized/garbage → safe no-op
    except Exception:
        raw = ""
    _capture(default_event or "SessionStart", raw)

    # Delegate to the normal recall, feeding it the bytes we already consumed.
    try:
        hook_recall._read_stdin_json = lambda: _safe_json(raw)  # type: ignore
        return hook_recall.main(default_event=default_event)
    except Exception:
        # Even if delegation fails, never break the session.
        sys.stdout.write("{}")
        return 0


def _safe_json(raw: str) -> dict:
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


if __name__ == "__main__":
    ev = "PostCompact" if (len(sys.argv) >= 2 and sys.argv[1] == "--compact") else "SessionStart"
    raise SystemExit(main(default_event=ev))
