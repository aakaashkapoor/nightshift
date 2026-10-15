"""Dependency DAG + scheduler (SPEC §5).

Explicit ``depends_on`` is the source of truth. This builds the graph, validates it
(no cycles), and answers the one question the daemon asks each tick: *which slices
are runnable right now?* — i.e. ``ready`` slices whose every parent is ``done``.
Roots (no deps) are runnable as soon as they're ready. A dependency the source does
not return (on hold, not a Nightshift issue, a typo) is never done: its dependant
waits on it instead of breaking the whole graph.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .slice import Slice

READY = "ready"
DONE = "done"
UNKNOWN = "not ready: on hold or not a Nightshift issue"


def _norm(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def overlaps(a: set[str], b: set[str]) -> bool:
    """True if any path in ``a`` equals, contains, or is inside any path in ``b``."""
    for x in map(_norm, a):
        for y in map(_norm, b):
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


class CycleError(ValueError):
    """Raised when the slices' dependencies form a cycle."""


@dataclass
class Dag:
    slices: dict[str, Slice]  # id -> slice, insertion order preserved

    @classmethod
    def build(cls, slices: list[Slice]) -> Dag:
        by_id: dict[str, Slice] = {}
        for s in slices:
            by_id[s.id] = s
        dag = cls(slices=by_id)
        dag._topo()  # validates acyclicity (raises CycleError)
        return dag

    def parents(self, sid: str) -> list[str]:
        return list(self.slices[sid].depends_on)

    def children(self, sid: str) -> list[str]:
        return [s.id for s in self.slices.values() if sid in s.depends_on]

    def status_of(self, sid: str) -> str:
        """A slice's status; a dependency the source did not return is never done."""
        s = self.slices.get(sid)
        return s.status if s is not None else UNKNOWN

    def unknown_deps(self) -> set[tuple[str, str]]:
        """Every (slice, dependency) pair whose dependency the source did not return."""
        return {
            (s.id, d) for s in self.slices.values() for d in s.depends_on if d not in self.slices
        }

    def runnable(self) -> list[Slice]:
        out = []
        for s in self.slices.values():
            if s.status != READY:
                continue
            if all(self.status_of(d) == DONE for d in s.depends_on):
                out.append(s)
        return out

    def dependant_counts(self) -> dict[str, int]:
        """How many slices (transitively) wait on each slice: the critical-path weight."""
        counts: dict[str, int] = {}
        for sid in self.slices:
            seen: set[str] = set()
            stack = self.children(sid)
            while stack:
                child = stack.pop()
                if child not in seen:
                    seen.add(child)
                    stack.extend(self.children(child))
            counts[sid] = len(seen)
        return counts

    def waiting_reason(self, sid: str) -> str | None:
        """Why a ready slice can't run yet (unfinished dependencies), or None."""
        pending = [d for d in self.slices[sid].depends_on if self.status_of(d) != DONE]
        if not pending:
            return None
        return "waiting on " + ", ".join(f"{d} ({self.status_of(d)})" for d in pending)

    def topo_order(self) -> list[str]:
        return self._topo()

    def _topo(self) -> list[str]:
        known = {sid: set(s.depends_on) & self.slices.keys() for sid, s in self.slices.items()}
        indeg = {sid: len(deps) for sid, deps in known.items()}
        children: dict[str, list[str]] = {sid: [] for sid in self.slices}
        for sid, deps in known.items():
            for dep in deps:
                children[dep].append(sid)
        queue = deque(sid for sid, d in indeg.items() if d == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in children[node]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if len(order) != len(self.slices):
            raise CycleError("dependency cycle detected among slices")
        return order
