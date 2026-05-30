"""komi-learn - the interactive install wizard (Hermes-style).

Walks a new user through setup with plain one-sentence explanations and simple
Y/n choices, then runs the real host install. The goal: nobody types `[smart]` or
edits config by hand. Non-interactive / --yes resolves every prompt to its default
(pool ON, semantic ON, cadence 8), so scripted installs still work.

Defaults chosen with the user:
  • Community pool: ON by default (framed plainly; contributing still requires
    your per-item approval - joining just gets you the shared knowledge + queues
    your general lessons for review).
  • Semantic recall: ON by default (offers to download the local model for you).
"""

from __future__ import annotations

from typing import Optional

from . import cli_prompt as P
from .adapters import config_io

DEFAULT_POOL_URL = "https://github.com/kurikomi-labs/komi-pool"


def _host_paths(host: str):
    if host == "codex":
        from .adapters.codex import paths
    else:
        from .adapters.claude_code import paths
    return paths


def run_wizard(*, host: str, pool_url: Optional[str], api_key: Optional[str],
               nudge_turns: int, assume_yes: bool) -> dict:
    """Collect setup choices interactively and persist them to the host config.
    Returns a dict of the resolved choices (so the caller can pass api_key/pool to
    the real installer). Does NOT install hooks - the caller does that next."""
    P.ASSUME_YES = assume_yes
    paths = _host_paths(host)
    cfg = config_io.load_raw(paths)

    P.say()
    P.say("  komi-learn - let's set up continuous learning for your agent.")
    P.say(f"  Host: {host}.  (You can change any of this later with `komi-learn config`.)")
    P.say()

    # 1. Semantic recall
    want_semantic = P.ask_yes_no(
        "Enable smarter, meaning-based memory?",
        default=True,
        summary="Finds past lessons by meaning, not just keywords (downloads a "
                "local model, ~300MB, one time, then offline - no API key).",
    )
    config_io.set_key(cfg, "recall.semantic", want_semantic)
    if want_semantic:
        from . import model_install
        if not model_install.is_installed():
            P.say("    downloading the model now (this can take a minute)...")
            ok, detail = model_install.install_model(quiet=True)
            P.say(f"    {'ready' if ok else 'could not install (' + detail + ') - recall will use keywords until installed'}")
    P.say()

    # 2. Community pool - DEFAULT YES, plain framing
    want_pool = P.ask_yes_no(
        "Join the komi community knowledge pool?",
        default=True,
        summary="Get useful, general tips other people's agents have learned - and "
                "share your own ANONYMIZED ones. No personal data ever leaves your "
                "machine, and you approve every single thing before it's shared.",
    )
    if want_pool:
        url = pool_url or P.ask_text(
            "Pool repo URL", default=DEFAULT_POOL_URL,
            summary="Where the shared knowledge lives (default is the official pool).",
        )
        config_io.set_key(cfg, "pool.repo_url", url)
        config_io.set_key(cfg, "pool.require_signature", True)
        # Joining gets you the knowledge + queues your general lessons for YOUR
        # review. Auto-publishing stays OFF - nothing is shared without approval.
        config_io.set_key(cfg, "pool.auto_contribute", False)
        # Trust gate: pull every signed lesson for now (min 1 signer). Raise this
        # later (`komi-learn config set pool.min_corroboration 2`) to only accept
        # lessons several people independently arrived at, once the pool is dense.
        config_io.set_key(cfg, "pool.min_corroboration", 1)
        # GitHub username (optional) - bound into your signed contributions so the
        # pool can confirm it's really you and count distinct people, not just keys.
        # Only used when YOU approve sharing a lesson; nothing auto-publishes.
        gh = P.ask_text(
            "Your GitHub username (optional, press enter to skip)", default="",
            summary="Lets the pool verify your contributions are yours when you share "
                    "one. Leave blank to contribute without it.",
        )
        if gh.strip():
            config_io.set_key(cfg, "pool.github_user", gh.strip().lstrip("@"))
        pool_url = url
    else:
        config_io.set_key(cfg, "pool.repo_url", "")
        pool_url = None
    P.say()

    # 3. Cadence (only ask the curious; default is fine for most)
    config_io.set_key(cfg, "nudge_turns", nudge_turns)

    config_io.save_raw(paths, cfg)
    P.say("  Saved your preferences. Finishing setup...")
    P.say()
    return {"pool_url": pool_url, "api_key": api_key, "nudge_turns": nudge_turns,
            "semantic": want_semantic}


__all__ = ["run_wizard", "DEFAULT_POOL_URL"]
