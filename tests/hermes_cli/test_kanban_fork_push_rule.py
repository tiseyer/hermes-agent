"""Fork-push rule (GitHub-403 finding, 2026-08-19).

Workers push ONLY to the personal fork when the repo has a remote named
``fork`` (this framework repo: tiseyer/hermes-agent); origin/upstream
(NousResearch/hermes-agent) is strictly read-only.

Where the rule takes effect:
  * agent/prompt_builder.py::KANBAN_GUIDANCE — the shared worker rule,
    injected into every kanban worker's system prompt via
    agent/agent_init.py (``_kanban_worker_guidance``) and
    agent/system_prompt.py (tool_guidance block).
  * hermes_cli/kanban_db.py::_push_remote — done-verification checks the
    branch on the remote workers actually push to.
"""
import subprocess
from pathlib import Path

from hermes_cli.kanban_db import _push_remote


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_push_remote_prefers_fork_when_present(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://github.com/NousResearch/hermes-agent.git"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "fork",
         "https://github.com/tiseyer/hermes-agent.git"], check=True)
    assert _push_remote(repo) == "fork"


def test_push_remote_falls_back_to_origin(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://github.com/tiseyer/voicera-os.git"], check=True)
    assert _push_remote(repo) == "origin"


def test_push_remote_survives_missing_repo(tmp_path):
    # Kein Git-Repo -> konservativ origin (kein Crash im Done-Gate).
    assert _push_remote(tmp_path / "nope") == "origin"


def test_kanban_worker_prompt_contains_fork_push_rule():
    """Trockener Readback: die gemeinsame Worker-Regel steht im Prompt-Text."""
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert "fork-push rule" in KANBAN_GUIDANCE.lower()
    assert "tiseyer/hermes-agent" in KANBAN_GUIDANCE
    assert "READ-ONLY" in KANBAN_GUIDANCE
    # Die alte, pauschale origin-Anweisung darf nicht mehr vorkommen.
    assert "push -u origin <branch>" not in KANBAN_GUIDANCE
