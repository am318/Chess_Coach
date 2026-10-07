"""Load ``coach.toml`` (see ``coach.example.toml``)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Both paths can be pointed elsewhere with an environment variable, so the app
# can be run against a throwaway copy of the database (a rehearsal, a bug hunt,
# a demo) without touching the real one.
CONFIG_PATH = Path(os.environ.get("COACH_TOML") or (ROOT / "coach.toml"))
DB_PATH = Path(os.environ.get("COACH_DB") or (ROOT / "coach.db"))


@dataclass
class EngineCfg:
    stockfish_path: str = "stockfish"
    analysis_nodes: int = 300_000
    analysis_depth: int = 18
    multipv: int = 3
    workers: int = 3
    # Optional deeper background pass over already-analysed games.
    deep_nodes: int = 2_500_000
    deep_depth: int = 28
    deep_multipv: int = 3

    def deepened(self) -> EngineCfg:
        return EngineCfg(
            stockfish_path=self.stockfish_path,
            analysis_nodes=self.deep_nodes,
            analysis_depth=self.deep_depth,
            multipv=self.deep_multipv,
            workers=self.workers,
        )


@dataclass
class ProfileCfg:
    inaccuracy_cp: int = 50
    mistake_cp: int = 120
    blunder_cp: int = 250
    quiet_forcingness_max: float = 0.25


@dataclass
class StartupCfg:
    auto_sync: bool = True
    auto_analyze: bool = True
    analyze_limit: int = 60      # cap games analysed per app-open (they're slow)
    mine_puzzles: bool = True
    # Keep re-analysing already-analysed games at a deeper budget in the
    # background, using spare compute. Off by default.
    deep_pass: bool = False


@dataclass
class CoachConfig:
    lichess_users: list[str] = field(default_factory=list)
    chesscom_users: list[str] = field(default_factory=list)
    lichess_token: str = ""
    lichess_speeds: list[str] = field(default_factory=lambda: ["blitz", "rapid", "classical"])
    chesscom_speeds: list[str] = field(default_factory=lambda: ["blitz", "rapid", "daily"])
    lichess_max_games: int = 3000
    chesscom_max_games: int = 3000
    # Treat all configured accounts (Lichess + Chess.com) as one player: the
    # profile, puzzles and dashboard aggregate across them by default.
    unified_identity: bool = True
    engine: EngineCfg = field(default_factory=EngineCfg)
    profile: ProfileCfg = field(default_factory=ProfileCfg)
    startup: StartupCfg = field(default_factory=StartupCfg)

    @property
    def all_users(self) -> list[str]:
        return list(self.lichess_users) + list(self.chesscom_users)

    def deepened(self) -> CoachConfig:
        """Copy with the engine budget raised to the deep-pass settings."""
        import dataclasses

        return dataclasses.replace(self, engine=self.engine.deepened())

    @classmethod
    def load(cls, path: Path | None = None) -> CoachConfig:
        path = path or CONFIG_PATH
        if not path.is_file():
            raise SystemExit(
                f"missing {path.name}. Copy coach.example.toml to coach.toml and fill it in."
            )
        data = tomllib.loads(path.read_text())
        acc = data.get("accounts", {})
        lic = data.get("lichess", {})
        cc = data.get("chesscom", {})
        eng = data.get("engine", {})
        prof = data.get("profile", {})
        start = data.get("startup", {})
        return cls(
            lichess_users=list(acc.get("lichess", [])),
            chesscom_users=list(acc.get("chesscom", [])),
            lichess_token=lic.get("token", ""),
            lichess_speeds=list(lic.get("speeds", ["blitz", "rapid", "classical"])),
            chesscom_speeds=list(cc.get("speeds", ["blitz", "rapid", "daily"])),
            lichess_max_games=int(lic.get("max_games", 3000)),
            chesscom_max_games=int(cc.get("max_games", 3000)),
            unified_identity=bool(acc.get("unified", True)),
            engine=EngineCfg(**{k: eng[k] for k in eng if k in EngineCfg.__annotations__}),
            profile=ProfileCfg(**{k: prof[k] for k in prof if k in ProfileCfg.__annotations__}),
            startup=StartupCfg(**{k: start[k] for k in start if k in StartupCfg.__annotations__}),
        )
