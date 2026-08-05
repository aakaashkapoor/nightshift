"""Load the central Nightshift config (SPEC §8).

One central file (``~/.nightshift/config.yaml``) holds global ``defaults`` plus a
per-repo entry for each managed repo. ``check`` is always per-repo. String values
support ``${VAR}`` environment interpolation (e.g. for secrets referenced from
``~/.nightshift/secrets.env``). A repo can be resolved by its config name or by a
filesystem path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

DEFAULT_CONFIG_PATH = Path.home() / ".nightshift" / "config.yaml"

_yaml = YAML(typ="safe")
_yaml_rt = YAML()  # round-trip, for writing config back out
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def register_repo(
    config_path: Path | str | None,
    *,
    name: str,
    path: Path | str,
    check: str,
    source: str = "local-md",
    base_branch: str = "main",
    symlink_dirs: list | None = None,
    push: bool = False,
    sync: str | None = None,
) -> Path:
    """Add/update a repo entry in the central config (concierge / `nsh init`)."""
    target = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    data: dict = {}
    if target.exists():
        data = _yaml.load(target.read_text(encoding="utf-8")) or {}
    repos = data.setdefault("repos", {})
    entry: dict[str, Any] = {
        "path": str(path),
        "source": source,
        "check": check,
        "base_branch": base_branch,
    }
    if symlink_dirs:
        entry["symlink_dirs"] = symlink_dirs
    if push:
        entry["push"] = True
    if sync:
        entry["sync"] = sync
    repos[name] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        _yaml_rt.dump(data, handle)
    return target


class UnknownRepoError(ValueError):
    """Raised when no registered repo matches a given name or path."""


def _interpolate(value: Any) -> Any:
    """Recursively expand ``${VAR}`` in strings; unknown vars are left as-is."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


@dataclass
class RepoConfig:
    """A single repo's resolved settings (defaults merged in)."""

    name: str
    path: Path
    check: str
    source: str = "local-md"
    base_branch: str = "main"
    pr: dict = field(default_factory=lambda: {"enabled": False})
    external_review: dict = field(
        default_factory=lambda: {"required": False, "provider": "github-pr"}
    )
    tracker: dict = field(default_factory=lambda: {"type": "none"})
    max_parallel: int = 5
    symlink_dirs: list = field(default_factory=list)
    push: bool = False  # push base to origin after a successful merge (SPEC §9)
    # Runs in repo_path right after a successful merge, before push (e.g. `npm
    # install`) — a symlinked dir like node_modules can end up stale in the repo
    # even though the merge is clean; see integrate_branch's docstring.
    sync: str | None = None


@dataclass
class Config:
    defaults: dict
    repos: dict  # name -> raw repo entry

    @classmethod
    def parse(cls, text: str) -> Config:
        data = _interpolate(_yaml.load(text) or {})
        return cls(defaults=data.get("defaults") or {}, repos=data.get("repos") or {})

    @classmethod
    def load(cls, path: Path | str | None = None) -> Config:
        p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(f"Nightshift config not found: {p}")
        return cls.parse(p.read_text(encoding="utf-8"))

    def repo(self, key: str) -> RepoConfig:
        """Resolve a repo by config name or filesystem path."""
        found = self._find(key)
        if found is None:
            raise UnknownRepoError(f"No registered repo matches {key!r}")
        name, entry = found
        return self._resolve(name, entry)

    def _find(self, key: str) -> tuple[str, dict] | None:
        if key in self.repos:
            return key, self.repos[key]
        target = Path(key).expanduser().resolve()
        for name, entry in self.repos.items():
            raw_path = entry.get("path")
            if raw_path and Path(raw_path).expanduser().resolve() == target:
                return name, entry
        return None

    def _resolve(self, name: str, entry: dict) -> RepoConfig:
        if "check" not in entry:
            raise ValueError(f"repo {name!r} is missing a 'check' command")
        return RepoConfig(
            name=name,
            path=Path(entry["path"]).expanduser() if entry.get("path") else Path("."),
            check=entry["check"],
            source=entry.get("source", "local-md"),
            base_branch=entry.get("base_branch", "main"),
            pr=entry.get("pr", {"enabled": False}),
            external_review=entry.get(
                "external_review", {"required": False, "provider": "github-pr"}
            ),
            tracker=entry.get("tracker", {"type": "none"}),
            max_parallel=entry.get("max_parallel", self.defaults.get("max_parallel", 5)),
            symlink_dirs=entry.get("symlink_dirs", []),
            push=entry.get("push", False),
            sync=entry.get("sync"),
        )
