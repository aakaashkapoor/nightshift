"""`nsh wait` and `nsh status`: how the master agent follows the daemon (v2, Step 6)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .control import Control
from .events import ATTENTION, EventLog
from .paths import repo_home
from .prompts import ANSWER_PROMPT
from .runtime import Runtime
from .source import build_source
from .state import StateStore, render_status


def _read_cursor(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def wait_for_attention(
    log: EventLog,
    cursor_path: Path,
    *,
    timeout: float | None,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Block until an ATTENTION event arrives after the saved cursor; [] on timeout."""
    offset = _read_cursor(cursor_path)
    if offset is None:
        offset = log.end_offset()
    deadline = None if timeout is None else clock() + timeout
    while True:
        events, offset = log.read_from(offset)
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(str(offset), encoding="utf-8")
        hits = [e for e in events if e.get("kind") in ATTENTION]
        if hits:
            return hits
        if deadline is not None and clock() >= deadline:
            return []
        sleep(poll)


def _home(repo, config_path, home_root) -> Path:
    name = Config.load(config_path).repo(str(repo)).name
    return repo_home(name, home_root)


def run_wait_cli(repo, *, config_path=None, timeout=None, poll=2.0, home_root=None) -> list[dict]:
    home = _home(repo, config_path, home_root)
    return wait_for_attention(
        EventLog(home / "events.jsonl"), home / "wait.cursor", timeout=timeout, poll=poll
    )


def run_status_cli(repo, *, config_path=None, as_json=False, home_root=None, now=None) -> str:
    snapshot = StateStore(_home(repo, config_path, home_root) / "state.json").snapshot()
    if as_json:
        return json.dumps(snapshot, indent=2)
    return render_status(snapshot, time.time() if now is None else now)


def run_answer_cli(repo, slice_id: str, text: str, *, config_path=None, home_root=None) -> str:
    """Record the owner's answer in the issue and queue the slice to resume with it."""
    repo_cfg = Config.load(config_path).repo(str(repo))
    source = build_source(repo_cfg)
    if source.get(slice_id) is None:
        raise LookupError(f"no slice {slice_id!r}")
    home = repo_home(repo_cfg.name, home_root)
    source.append_to_section(slice_id, "Answers", f"- {text}")
    source.set_status(slice_id, "ready")
    Runtime(home / "runtime.json").record(
        slice_id,
        interrupted=True,
        awaiting_answer=False,
        resume_prompt=ANSWER_PROMPT.format(answer=text),
    )
    EventLog(home / "events.jsonl").emit("answered", slice_id, answer=text)
    return f"answered {slice_id}; it will resume on the next poll"


_CONTROL = {
    "pause": ({"paused": True}, "paused: no new slices will start"),
    "unpause": ({"paused": False}, "unpaused"),
    "stop": ({"stop": True}, "stopping: the daemon exits after the running slices finish"),
}


def run_control_cli(repo, action: str, *, config_path=None, home_root=None) -> str:
    flags, message = _CONTROL[action]
    Control(_home(repo, config_path, home_root) / "control.json").set(**flags)
    return message
