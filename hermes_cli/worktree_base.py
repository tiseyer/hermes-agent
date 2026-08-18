"""Shared "freshest base ref" resolution for git worktree creation.

Extracted from ``cli.py::_resolve_worktree_base`` (the ``hermes -w`` CLI
worktree bootstrap) so the Kanban dispatcher's worktree materialization
(``hermes_cli/kanban_db.py::_ensure_git_worktree``) can reuse the exact same
contract instead of hardcoding ``HEAD`` as the branch base.

A standalone clone's local ``HEAD`` can lag the remote significantly — the
``~/.hermes/hermes-agent`` clone the dispatcher runs from is only updated by
``hermes update``, not on every dispatch tick. Branching a new worktree off
that stale ``HEAD`` roots every new task branch on an old base, which makes
review diffs balloon with unrelated upstream commits the task never touched.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from hermes_cli._subprocess_compat import noninteractive_git_env

_log = logging.getLogger(__name__)


def resolve_worktree_base(
    repo_root: str,
    fetch_timeout: float = 5,
    freshness_window: float = 300,
) -> tuple:
    """Resolve the freshest base ref to branch a new worktree from.

    Strategy (each step falls back to the next on failure):
      1. If the current branch tracks an upstream, refresh and use that
         upstream ref — so a deliberate feature-branch checkout tracks its
         own remote, not the default branch.
      2. Else refresh the remote's default branch (``origin/HEAD`` → e.g.
         ``origin/main``) and use it.
      3. Else fall back to ``HEAD`` (offline, no remote, or detached) — the
         old behavior, never worse than before.

    "Refresh" is deliberately cheap:

    - The fetch is SKIPPED entirely when the repo's ``FETCH_HEAD`` is
      younger than *freshness_window* seconds — a base fetched moments ago
      cannot have meaningfully moved.
    - The fetch is capped at *fetch_timeout* seconds. On timeout or failure
      we fall back to the locally-known remote-tracking ref (labelled
      "cached") instead of cascading into a second fetch attempt.

    Returns ``(base_ref, label)`` where *base_ref* is a git revision
    suitable for ``git worktree add ... <base_ref>`` and *label* is a short
    human-readable description for logs/session banners.
    """

    def _git(args, timeout: float = 20):
        return subprocess.run(
            ["git", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=repo_root,
            stdin=subprocess.DEVNULL,
            env=noninteractive_git_env(),
        )

    def _ref_exists(ref: str) -> bool:
        try:
            return _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"]).returncode == 0
        except Exception:
            return False

    def _fetch_head_age() -> Optional[float]:
        """Seconds since the last fetch in this repo, or None if unknown."""
        try:
            gd = _git(["rev-parse", "--git-dir"])
            if gd.returncode != 0:
                return None
            git_dir = Path(gd.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = Path(repo_root) / git_dir
            fetch_head = git_dir / "FETCH_HEAD"
            if not fetch_head.exists():
                return None
            return max(0.0, time.time() - fetch_head.stat().st_mtime)
        except Exception:
            return None

    def _refresh(remote: str, branch: str, ref: str) -> tuple:
        """Return (ref, label) after a cheap best-effort refresh of *ref*.

        Never raises, never fetches twice, never blocks longer than
        *fetch_timeout*.
        """
        age = _fetch_head_age()
        if age is not None and age < freshness_window and _ref_exists(ref):
            return ref, f"{ref} (fetched {int(age)}s ago)"
        try:
            fetched = _git(["fetch", remote, branch], timeout=fetch_timeout)
            if fetched.returncode == 0:
                return ref, f"{ref} (fetched)"
            reason = "fetch failed"
        except subprocess.TimeoutExpired:
            reason = f"fetch timed out after {fetch_timeout:g}s"
        except Exception as e:
            reason = f"fetch error: {e}"
        if _ref_exists(ref):
            _log.debug("worktree base: %s — using cached %s", reason, ref)
            return ref, f"{ref} (cached — {reason})"
        return "HEAD", f"HEAD (local — {reason}, no cached {ref})"

    # 0. Explicit override — an operator/board that knows the integration
    #    branch pins it here (e.g. HERMES_WORKTREE_BASE_REF=origin/develop).
    import os as _os
    _override = (_os.environ.get("HERMES_WORKTREE_BASE_REF") or "").strip()
    if _override and "/" in _override:
        remote, branch = _override.split("/", 1)
        return _refresh(remote, branch, _override)

    # 1. Current branch's upstream, if it tracks one.
    try:
        up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if up.returncode == 0:
            upstream = up.stdout.strip()  # e.g. "origin/main"
            if upstream and "/" in upstream:
                remote, branch = upstream.split("/", 1)
                return _refresh(remote, branch, upstream)
    except Exception as e:
        _log.debug("worktree base: upstream resolution failed: %s", e)

    # 1.5. Git-flow integration branch. Repos that carry an
    #      ``origin/develop`` integrate there, not on the remote default
    #      branch — a task branch rooted on origin/main in such a repo is
    #      born ~N commits stale and every coder must rebase before work
    #      (live-repro: wt/t_31521123 landed 20 commits behind develop).
    try:
        if _ref_exists("refs/remotes/origin/develop"):
            return _refresh("origin", "develop", "origin/develop")
    except Exception as e:
        _log.debug("worktree base: develop-branch check failed: %s", e)

    # 2. Remote default branch (origin/HEAD).
    try:
        # Resolve the remote's default branch symref.
        head_ref = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
        default_ref = ""
        if head_ref.returncode == 0:
            default_ref = head_ref.stdout.strip().replace("refs/remotes/", "", 1)
        if not default_ref:
            # origin/HEAD not set locally; ask the remote (network — capped
            # like the fetch so a stalled connection can't hang startup).
            show = _git(["remote", "show", "origin"], timeout=max(fetch_timeout, 5))
            for line in show.stdout.splitlines():
                line = line.strip()
                if line.startswith("HEAD branch:"):
                    _branch = line.split(":", 1)[1].strip()
                    # A remote with no default branch reports "(unknown)";
                    # don't construct a bogus "origin/(unknown)" ref from it.
                    if _branch and _branch != "(unknown)":
                        default_ref = "origin/" + _branch
                    break
        if default_ref and "/" in default_ref:
            remote, branch = default_ref.split("/", 1)
            return _refresh(remote, branch, default_ref)
    except Exception as e:
        _log.debug("worktree base: default-branch resolution failed: %s", e)

    # 3. Fall back to local HEAD (offline / no remote / detached).
    return "HEAD", "HEAD (local — could not reach remote)"
