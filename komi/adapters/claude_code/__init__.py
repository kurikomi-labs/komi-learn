"""Claude Code adapter for komi-learn.

Zero friction by construction:
  • SessionStart hook → inject recalled learnings as additionalContext
  • Stop / SubagentStop hook → spawn the distiller detached (never blocks the turn)

Both are thin: they read the hook JSON from stdin, call the engine, and either
print a hook response (recall) or fork-and-exit (distill). No slash commands.
"""
