"""Babysit helpers behind `nsh pr checkout | merge | comment`.

Each does one thing and fails fast with a clear message (field note #9). The runners
(``gh``, ``git``, ``run``) are injected so the logic is testable without GitHub.
"""

from __future__ import annotations

import json
from pathlib import Path

from .check import run_check
from .config import Config, UnknownRepoError
from .pr import GitHubPR
from .worktree import WorktreeManager


class PrError(Exception):
    """A helper step failed; the message says what to do about it."""


def pr_slice_id(repo: Path, number: int) -> str:
    """Name of the sibling worktree for a PR: ``<repo-name>-pr-<n>``."""
    return f"{repo.name}-pr-{number}"


def checkout(number: int, *, repo: Path, install: str | None, gh, git, run=run_check) -> Path:
    """Check the PR's branch out into a sibling worktree and install its dependencies."""
    head = json.loads(gh("pr", "view", str(number), "--json", "headRefName"))["headRefName"]
    path = repo.parent / pr_slice_id(repo, number)
    if path.exists():
        raise PrError(f"{path} already exists; remove it or run `nsh pr merge {number}`")
    git("fetch", "origin", head)
    git("worktree", "add", str(path), f"origin/{head}")
    if install:
        installed = run(install, path)
        if not installed.passed:
            raise PrError(f"install failed in {path}: {installed.output[-300:].strip()}")
    return path


def merge(number: int, *, repo: Path, pr, worktrees) -> bool:
    """Squash-merge once mergeable, then drop the PR's worktree; True if one was removed."""
    pr.merge(str(number))
    sid = pr_slice_id(repo, number)
    if not worktrees.exists(sid):
        return False
    worktrees.teardown(sid)
    return True


def comment(number: int, text: str, *, gh) -> None:
    """Record what was verified on the PR."""
    if not text.strip():
        raise PrError("say what you verified")
    gh("pr", "comment", str(number), "--body", f"✅ Verified:\n\n{text.strip()}")


def checkout_cli(number: int, repo: Path, *, config_path=None) -> str:
    repo = repo.resolve()
    try:
        repo_cfg = Config.load(config_path).repo(str(repo))
    except (FileNotFoundError, UnknownRepoError) as exc:
        raise PrError(f"{repo} is not registered with Nightshift (run `nsh init`): {exc}") from exc
    pr = GitHubPR(repo)
    path = checkout(
        number, repo=repo, install=repo_cfg.install, gh=pr.gh, git=pr.git, run=run_check
    )
    return str(path)


def merge_cli(number: int, repo: Path) -> str:
    repo = repo.resolve()
    removed = merge(
        number,
        repo=repo,
        pr=GitHubPR(repo),
        worktrees=WorktreeManager(repo, worktrees_root=repo.parent),
    )
    suffix = f"; removed {pr_slice_id(repo, number)}" if removed else ""
    return f"merged #{number}{suffix}"


def comment_cli(number: int, text: str, repo: Path) -> str:
    comment(number, text, gh=GitHubPR(repo.resolve()).gh)
    return f"commented on #{number}"
