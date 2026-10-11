"""Dependency DAG + scheduler (SPEC §5).

Explicit ``depends_on`` is the source of truth. This builds the graph, validates it
(every dependency exists, no cycles), and answers the one question the daemon asks
each tick: *which slices are runnable right now?* — i.e. ``ready`` slices whose
every parent is ``done``. Roots (no deps) are runnable as soon as they're ready.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .slice import Slice

READY = "ready"
DONE = "done"


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
        for s in slices:
            for dep in s.depends_on:
                if dep not in by_id:
                    raise ValueError(f"slice {s.id!r} depends on unknown slice {dep!r}")
        dag = cls(slices=by_id)
        dag._topo()  # validates acyclicity (raises CycleError)
        return dag

    def parents(self, sid: str) -> list[str]:
        return list(self.slices[sid].depends_on)

    def children(self, sid: str) -> list[str]:
        return [s.id for s in self.slices.values() if sid in s.depends_on]

    def runnable(self) -> list[Slice]:
        out = []
        for s in self.slices.values():
            if s.status != READY:
                continue
            if all(self.slices[d].status == DONE for d in s.depends_on):
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
        pending = [d for d in self.slices[sid].depends_on if self.slices[d].status != DONE]
        if not pending:
            return None
        return "waiting on " + ", ".join(f"{d} ({self.slices[d].status})" for d in pending)

    def topo_order(self) -> list[str]:
        return self._topo()

    def _topo(self) -> list[str]:
        indeg = {sid: len(set(s.depends_on)) for sid, s in self.slices.items()}
        children: dict[str, list[str]] = {sid: [] for sid in self.slices}
        for s in self.slices.values():
            for dep in set(s.depends_on):
                children[dep].append(s.id)
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
