"""The supervisor restarts a crashed daemon child."""

from nightshift.events import EventLog
from nightshift.supervisor import supervise


class Runs:
    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = 0

    def __call__(self, argv) -> int:
        self.calls += 1
        return self.codes.pop(0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _kinds(path):
    return [e["kind"] for e in EventLog(path).read_from(0)[0]]


def test_clean_exit_ends_supervisor(tmp_path) -> None:
    log = EventLog(tmp_path / "e.jsonl")
    assert supervise(["x"], run=Runs(0), sleep=lambda s: None, clock=Clock(), events=log) == 0
    assert _kinds(tmp_path / "e.jsonl") == ["daemon_exit"]


def test_crash_restarts_with_backoff(tmp_path) -> None:
    log = EventLog(tmp_path / "e.jsonl")
    sleeps = []
    runs = Runs(1, 3, 0)
    assert supervise(["x"], run=runs, sleep=sleeps.append, clock=Clock(), events=log) == 0
    assert runs.calls == 3
    assert sleeps == [5, 10]
    assert _kinds(tmp_path / "e.jsonl") == ["daemon_restarted", "daemon_restarted", "daemon_exit"]


def test_gives_up_after_too_many_crashes_in_an_hour(tmp_path) -> None:
    log = EventLog(tmp_path / "e.jsonl")
    clock = Clock()
    runs = Runs(*([1] * 10))
    code = supervise(["x"], run=runs, sleep=lambda s: None, clock=clock, events=log, max_restarts=3)
    assert code == 1
    assert runs.calls == 4
    events, _ = log.read_from(0)
    assert events[-1]["kind"] == "daemon_exit"
    assert "gave up" in events[-1]["reason"]


def test_old_crashes_fall_out_of_the_window(tmp_path) -> None:
    clock = Clock()
    runs = Runs(1, 1, 1, 0)

    def sleep(s):
        clock.t += 4000  # each wait pushes past the 1-hour window

    assert (
        supervise(
            ["x"],
            run=runs,
            sleep=sleep,
            clock=clock,
            events=EventLog(tmp_path / "e"),
            max_restarts=1,
        )
        == 0
    )


def test_child_argv_round_trips_options() -> None:
    from nightshift.supervisor import child_argv

    argv = child_argv(repo="r", config=None, interval=60.0, log_file="d.log")
    assert argv[1:4] == ["-m", "nightshift", "daemon"]
    assert argv[4:] == ["--child", "--repo", "r", "--interval", "60.0", "--log-file", "d.log"]
    argv = child_argv(repo="r", config="c.yaml", interval=30.0, log_file=None)
    assert "--config" in argv and "--log-file" not in argv
