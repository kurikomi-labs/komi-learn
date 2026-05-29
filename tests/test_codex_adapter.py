"""Phase 6 — the Codex adapter proves the engine is genuinely host-agnostic.

Same engine, second host (OpenAI Codex CLI), files under $CODEX_HOME. These tests
cover: ABC conformance, the shared hooklib reuse, Codex install (hooks.json), and
the end-to-end distill→recall cycle on the Codex host with no Claude Code.
"""

import importlib
import json

import pytest


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    from komi.adapters.codex import paths as P
    importlib.reload(P)
    return tmp_path


# ── ABC conformance + host independence ─────────────────────────────────────

def test_codex_adapter_is_an_adapter():
    from komi.adapters.base import Adapter
    from komi.adapters.codex import CodexAdapter
    a = CodexAdapter()
    assert isinstance(a, Adapter)
    assert a.name == "codex"


def test_codex_paths_rooted_at_codex_home(codex_home):
    from komi.adapters.codex import paths as P
    assert str(P.codex_home()) == str(codex_home)
    assert P.personal_root() == codex_home / "komi"
    assert P.hooks_path() == codex_home / "hooks.json"


def test_codex_and_claude_paths_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from komi.adapters.codex import paths as CP
    from komi.adapters.claude_code import paths as CCP
    importlib.reload(CP)
    importlib.reload(CCP)
    assert CP.personal_root() != CCP.personal_root()   # never collide


# ── shared hooklib is actually shared ───────────────────────────────────────

def test_codex_uses_shared_hooklib():
    # both adapters' hook modules import the common hooklib — proving reuse, not copy
    import komi.adapters.codex.hook_recall as cr
    import komi.adapters.codex.hook_distill as cd
    from komi.adapters import hooklib
    assert cr.hooklib is hooklib
    assert cd.hooklib is hooklib


# ── install ─────────────────────────────────────────────────────────────────

def test_codex_install_writes_hooks_json(codex_home):
    from komi.adapters.codex import setup as S
    importlib.reload(S)
    rep = S.install()
    assert rep.ok
    h = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert {"SessionStart", "Stop", "SubagentStop"} <= set(h["hooks"])
    cmd = next(x["command"] for e in h["hooks"]["SessionStart"] for x in e["hooks"])
    assert "komi.adapters.codex.hook_recall" in cmd
    assert cmd.split()[0] not in ("python", "python3")    # absolute interpreter


def test_codex_install_idempotent(codex_home):
    from komi.adapters.codex import setup as S
    importlib.reload(S)
    S.install(); S.install()
    h = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    cmds = [x["command"] for e in h["hooks"]["SessionStart"] for x in e["hooks"]]
    assert sum(1 for c in cmds if "komi.adapters.codex" in c) == 1


def test_codex_install_merges_existing_hooks(codex_home):
    (codex_home / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo MINE"}]}]}
    }), encoding="utf-8")
    from komi.adapters.codex import setup as S
    importlib.reload(S)
    S.install()
    h = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    cmds = [x["command"] for e in h["hooks"]["SessionStart"] for x in e["hooks"]]
    assert any("echo MINE" in c for c in cmds)            # user's hook preserved
    assert any("komi.adapters.codex" in c for c in cmds)  # ours added
    assert (codex_home / "hooks.json.komi-bak").exists()


def test_codex_uninstall_removes_only_komi(codex_home):
    (codex_home / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo MINE"}]}]}
    }), encoding="utf-8")
    from komi.adapters.codex import setup as S
    importlib.reload(S)
    S.install()
    S.uninstall()
    h = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    cmds = [x["command"] for e in h["hooks"]["SessionStart"] for x in e["hooks"]]
    assert any("echo MINE" in c for c in cmds)            # survives
    assert not any("komi.adapters.codex" in c for c in cmds)


# ── the headline proof: distill→recall on the Codex host ────────────────────

class _ScriptedGPT:
    def complete(self, *, system, user):
        assert "<session-transcript>" in user             # data-fence applied
        return json.dumps([{"type": "procedural", "category": "tooling",
            "title": "cargo test --workspace runs all crates",
            "body": "In a multi-crate Rust workspace, cargo test --workspace runs every crate's tests.",
            "trigger": "testing a rust workspace", "tags": ["rust", "cargo"], "signal": "technique"}])

    def __call__(self, lng, *, context):
        return {"scope": "project", "category": lng.category, "rationale": "p"}


def test_codex_distill_then_recall_same_engine(codex_home):
    from komi.adapters.codex import paths as P
    from komi.adapters.codex import CodexAdapter
    from komi.adapters.base import RecallContext
    from komi.engine.store import Store
    from komi.engine.distill import distill
    importlib.reload(P)

    m = _ScriptedGPT()
    personal = Store(P.personal_root(), index_path=P.index_path())
    res = distill(
        [{"role": "user", "text": "run all tests in this rust workspace"},
         {"role": "assistant", "text": "cargo test --workspace"}],
        personal_store=personal, queue_dir=P.queue_dir(), llm=m, judge=m,
        session_id="codex-1", cwd=str(codex_home),
    )
    assert res.candidates == 1
    assert any("cargo test" in l.title.lower() for l in personal.all())

    # fresh session recalls it through the Codex adapter
    block = CodexAdapter().recall(RecallContext(
        cwd=str(codex_home), prompt_hint="running tests in this rust workspace"))
    assert "cargo test" in block.lower()
    # and it's the SAME engine fence (data-not-instructions framing)
    assert "<komi-recall>" in block
