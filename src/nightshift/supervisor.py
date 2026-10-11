"""Keep the daemon alive (field note #27: it died overnight and nobody knew).

``nsh daemon`` runs this supervisor. It starts the real loop as a child process and
restarts it when it exits with an error, backing off 5s, 10s, 20s ... up to 60s. More
than ``max_restarts`` crashes within an hour means something is persistently wrong:
it gives up and emits ``daemon_exit`` so the master agent hears about it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

from .events import EventLog

WINDOW = 3600.0


def child_argv(*, repo, config, interval: float, log_file) -> list[str]:
    argv = [sys.executable, "-m", "nightshift", "daemon", "--child", "--repo", str(repo)]
    if config is not None:
        argv += ["--config", str(config)]
    argv += ["--interval", str(interval)]
    if log_file is not None:
        argv += ["--log-file", str(log_file)]
    return argv


def _run_child(argv: list[str]) -> int:  # pragma: no cover - real subprocess
    return subprocess.call(argv)


def supervise(
    argv: list[str],
    *,
    events: EventLog,
    run: Callable[[list[str]], int] = _run_child,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    max_restarts: int = 5,
) -> int:
    crashes: list[float] = []
    while True:
        code = run(argv)
        if code == 0:
            events.emit("daemon_exit", reason="stopped")
            return 0
        now = clock()
        crashes = [t for t in crashes if now - t < WINDOW] + [now]
        if len(crashes) > max_restarts:
            reason = (
                f"supervisor gave up: {len(crashes)} crashes within an hour (last exit code {code})"
            )
            events.emit("daemon_exit", reason=reason)
            return code
        delay = min(60, 5 * 2 ** (len(crashes) - 1))
        events.emit("daemon_restarted", code=code, delay=delay)
        sleep(delay)


def run_supervised(
    repo, *, config_path=None, interval: float = 30, log_file=None, home_root=None
) -> int:  # pragma: no cover - wires the real child
    from .config import Config
    from .paths import repo_home

    name = Config.load(config_path).repo(str(repo)).name
    events = EventLog(repo_home(name, home_root) / "events.jsonl")
    return supervise(
        child_argv(repo=repo, config=config_path, interval=interval, log_file=log_file),
        events=events,
    )
