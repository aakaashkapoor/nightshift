"""Models per role (v2 design, Step 3)."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.config import DEFAULT_ROLE_MODELS, Config, RepoConfig
from nightshift.daemon import Daemon
from nightshift.executor import Executor, RunOutput
from nightshift.resolver import Resolver
from nightshift.source import LocalMdSource
from nightshift.worktree import WorktreeManager


def _cfg(**kw) -> RepoConfig:
    return RepoConfig(name="demo", path=Path("."), check="true", **kw)


def test_model_for_defaults() -> None:
    cfg = _cfg(model="opus")
    assert DEFAULT_ROLE_MODELS == {"S": "sonnet", "review": "sonnet", "resolve": "sonnet"}
    assert [cfg.model_for(r) for r in ("S", "review", "resolve")] == ["sonnet"] * 3
    assert cfg.model_for("M") == "opus"  # no default for M: the repo model
    assert cfg.model_for("L") == "opus"


def test_model_for_without_any_model_is_none() -> None:
    assert _cfg().model_for("M") is None


def test_model_for_override_wins() -> None:
    cfg = _cfg(model="opus", models={"S": "haiku", "L": "fable"})
    assert cfg.model_for("S") == "haiku"
    assert cfg.model_for("L") == "fable"
    assert cfg.model_for("review") == "sonnet"


def test_config_reads_models_from_repo_then_defaults() -> None:
    text = """
defaults:
  models: { S: haiku }
repos:
  a: { path: /a, check: "true", models: { M: opus } }
  b: { path: /b, check: "true" }
"""
    cfg = Config.parse(text)
    assert cfg.repo("a").models == {"M": "opus"}
    assert cfg.repo("b").models == {"S": "haiku"}


def test_with_model_keeps_everything_else() -> None:
    calls = []

    def runner(argv, cwd, stdin) -> RunOutput:
        calls.append(argv)
        return RunOutput(0, "{}", "")

    base = Executor(
        runner=runner,
        permission_mode="plan",
        model="opus",
        claude_bin="c",
        effort="high",
    )
    other = base.with_model("sonnet")
    assert other is not base and base.model == "opus"
    other.execute_prompt(".", "hi")
    assert calls[0] == [
        "c", "-p", "--output-format", "json", "--permission-mode", "plan",
        "--model", "sonnet", "--effort", "high",
    ]  # fmt: skip
    assert base.with_model(None).model is None


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def execute_prompt(self, cwd, prompt, resume_session=None):
        self.calls.append((str(cwd), resume_session))
        return Executor(runner=lambda *a: RunOutput(0, "{}", "")).execute_prompt(cwd, prompt)


def test_resolver_escalates_on_the_second_call_for_a_path() -> None:
    first, second = Recorder(), Recorder()
    r = Resolver(first, escalate=second)  # type: ignore[arg-type]
    r("/a")
    r("/b")  # a different path starts over on the cheap model
    r("/a")
    r("/a")
    assert [c[0] for c in first.calls] == ["/a", "/b"]
    assert [c[0] for c in second.calls] == ["/a", "/a"]


def test_resolver_without_escalate_keeps_using_its_executor() -> None:
    only = Recorder()
    r = Resolver(only)  # type: ignore[arg-type]
    r("/a")
    r("/a")
    assert len(only.calls) == 2


# --- the daemon picks the model by slice size --------------------------------------


def _git(cwd, *args) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(r)], check=True, capture_output=True)
    _git(r, "config", "user.email", "t@e.local")
    _git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    (r / ".slices").mkdir()
    return r


def test_daemon_runs_each_size_on_its_model(repo, tmp_path) -> None:
    for sid, size in (("small", "S"), ("medium", "M")):
        (repo / ".slices" / f"{sid}.md").write_text(
            f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n## Goal\nx\n\n## Size\n{size}\n",
            encoding="utf-8",
        )
    models: dict[str, str] = {}

    def agent(argv, cwd, stdin) -> RunOutput:
        models[Path(cwd).name] = argv[argv.index("--model") + 1]
        (Path(cwd) / f"{Path(cwd).name}.txt").write_text("x", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")

    d = Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(
            name="demo", path=repo, check="echo ok", base_branch="main", model="opus"
        ),
        executor=Executor(runner=agent, model="opus"),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
    )
    assert sorted(r.status for r in d.tick()) == ["done", "done"]
    assert models == {"small": "sonnet", "medium": "opus"}
