"""Shared git concurrency guard.

When the daemon runs slices in parallel threads (SPEC §2), each may issue git
commands against the same repo. A single process-wide lock serializes the (fast)
git operations while the slow part — the headless agent — still runs concurrently.
"""

import threading

# Held only around individual git subprocess calls, never around agent runs.
LOCK = threading.Lock()
