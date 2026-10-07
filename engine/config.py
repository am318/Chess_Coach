"""Engine configuration.

Values here are overridable from a JSON strategy file bundled in the submission
(``strategy.json`` at the zip root) so tuning does not require code changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EngineConfig:
    book_path: str | None = "data/book.bin"
    syzygy_path: str | None = None  # off in submission by default (size budget)
    book_max_ply: int = 16
    use_tablebase_in_search: bool = False
    default_moves_to_go: int = 28
    # Memory the transposition table may hold, in MB. The sandbox gives us 2 GB
    # for everything, so this is a budget rather than a target: see
    # engine.search._TT_BYTES_PER_ENTRY for how it becomes an entry count.
    tt_budget_mb: int = 256

    @classmethod
    def load(cls, path: str | Path | None = None) -> EngineConfig:
        cfg = cls()
        if path is None:
            path = Path(__file__).resolve().parent.parent / "strategy.json"
        p = Path(path)
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                for key, value in data.items():
                    if hasattr(cfg, key):
                        setattr(cfg, key, value)
            except (OSError, ValueError):
                pass
        return cfg
