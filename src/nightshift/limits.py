"""Claude usage limits (v2 design, Step 0: the budget gate).

When the account's usage limit hits, ``claude -p`` stops without doing any work and
returns an error result like "You've hit your session limit · resets 2:30pm (...)".
The session transcript records the exact reset as a Unix timestamp
(``apiErrorParams.rate_limit_info.resetsAt``). The daemon pauses until then instead
of blocking slices; if the time can't be found it retries every ``FALLBACK_WAIT``
seconds (a retry while still limited fails instantly and costs no tokens).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

FALLBACK_WAIT = 15 * 60  # seconds to wait when the reset time is unknown
RESET_GRACE = 60  # seconds after the reported reset before resuming

_LIMIT_RE = re.compile(r"(?i)hit your [a-z -]*limit|usage limit reached")


class UsageLimitReached(Exception):
    """An agent run stopped because the Claude usage limit was hit."""

    def __init__(self, session_id: str | None, reset_session: str | None = None):
        super().__init__("usage limit reached")
        # session_id: the build session to resume. reset_session: the session whose
        # transcript records the limit (a reviewer's or resolver's own, if it hit it).
        self.session_id = session_id
        self.reset_session = reset_session if reset_session is not None else session_id


def is_usage_limit(text: str) -> bool:
    return bool(_LIMIT_RE.search(text or ""))


def _claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def reset_time(session_id: str | None, root: Path | None = None) -> float | None:
    """The limit's reset time (Unix seconds) from the session transcript, if recorded."""
    if not session_id:
        return None
    base = (root if root is not None else _claude_dir()) / "projects"
    for transcript in base.glob(f"*/{session_id}.jsonl"):
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if "usage_limit_reached" not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = (record.get("apiErrorParams") or {}).get("rate_limit_info") or {}
            resets = info.get("resetsAt")
            if isinstance(resets, int | float):
                return float(resets)
    return None


def resume_at(resets_at: float | None, now: float) -> float:
    """When the daemon should try again."""
    if resets_at is not None and resets_at > now:
        return resets_at + RESET_GRACE
    return now + FALLBACK_WAIT
