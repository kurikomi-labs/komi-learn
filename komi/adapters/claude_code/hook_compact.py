"""PostCompact hook — re-inject recalled learnings after a /compact.

Thin entry point so the installed hooks can register a clear ``hook_compact``
command for PostCompact. The actual logic lives in :mod:`hook_recall` (it routes by
the ``hook_event_name`` on stdin), so this just delegates to keep one source of
truth for the recall build + emit. See hook_recall's module docstring for the
compaction-injection caveats.

Entry point: ``python -m komi.adapters.claude_code.hook_compact``
"""

from __future__ import annotations

from .hook_recall import main

if __name__ == "__main__":
    raise SystemExit(main())
