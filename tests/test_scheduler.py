"""Tests for the dependency DAG + scheduler (SPEC §5)."""

import pytest

from nightshift.scheduler import CycleError, Dag
from nightshift.slice import Slice


def S(sid: str, status: str, deps=None) -> Slice:
    return Slice(id=sid, title=sid, status=status, depends_on=list(deps or []))


def test_only_ready_roots_runnable_initially() -> None:
    dag = Dag.build([S("001", "ready"), S("002", "ready", ["001"]), S("005", "ready")])
    assert {s.id for s in dag.runnable()} == {"001", "005"}


def test_child_runnable_after_parent_done() -> None:
    dag = Dag.build([S("001", "done"), S("002", "ready", ["001"])])
    assert {s.id for s in dag.runnable()} == {"002"}


def test_child_waits_while_parent_not_done() -> None:
    # Parent in-progress -> child not runnable yet.
    dag = Dag.build([S("001", "in-progress"), S("002", "ready", ["001"])])
    assert dag.runnable() == []


def test_blocked_parent_blocks_child() -> None:
    dag = Dag.build([S("001", "blocked"), S("002", "ready", ["001"])])
    assert dag.runnable() == []


def test_cycle_detected() -> None:
    with pytest.raises(CycleError):
        Dag.build([S("001", "ready", ["002"]), S("002", "ready", ["001"])])


def test_unknown_dependency_raises() -> None:
    with pytest.raises(ValueError):
        Dag.build([S("002", "ready", ["999"])])


def test_parents_and_children() -> None:
    dag = Dag.build([S("001", "done"), S("002", "ready", ["001"]), S("003", "ready", ["001"])])
    assert set(dag.children("001")) == {"002", "003"}
    assert dag.parents("002") == ["001"]


def test_topo_order_respects_deps() -> None:
    dag = Dag.build([S("001", "ready"), S("002", "ready", ["001"]), S("003", "ready", ["002"])])
    order = dag.topo_order()
    assert order.index("001") < order.index("002") < order.index("003")
