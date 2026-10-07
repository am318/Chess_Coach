"""Keep the test run off the user's real database.

``coach.config.DB_PATH`` defaults to ``coach.db`` in the repo root, so anything
that calls ``session_factory()`` without an explicit path - the session planner,
the SRS rating seed, the sparring Elo - read and wrote the live coaching
database during ``make test``. One test was even *asserting* on the current
user's weakness profile, which made it pass or fail depending on how they had
been playing.

Setting the environment variable before any ``coach`` module is imported points
the default at a throwaway file for the whole session. pytest imports conftest
first, so this runs early enough.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="coach-tests-"))
os.environ.setdefault("COACH_DB", str(_TMP / "test.db"))
# Likewise for the config: tests read the shipped example, never a developer's
# own coach.toml, so a clean clone and a configured checkout behave the same.
_ROOT = Path(__file__).resolve().parent.parent
shutil.copy(_ROOT / "coach.example.toml", _TMP / "coach.toml")
os.environ["COACH_TOML"] = str(_TMP / "coach.toml")

