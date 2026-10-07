"""SQLite schema + session helpers for the coaching bot.

One local file (``coach.db``). SQLAlchemy 2.0 ORM. All timestamps are UTC.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from coach.config import DB_PATH


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_source_game"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))          # "lichess" | "chesscom" | "pgn"
    source_id: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(64), index=True)  # our username in this game
    color: Mapped[str] = mapped_column(String(5))            # "white" | "black"
    result: Mapped[str] = mapped_column(String(8))           # "win" | "loss" | "draw"
    termination: Mapped[str | None] = mapped_column(String(32), nullable=True)

    eco: Mapped[str | None] = mapped_column(String(8), nullable=True)
    opening: Mapped[str | None] = mapped_column(String(128), nullable=True)
    speed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    time_control: Mapped[str | None] = mapped_column(String(16), nullable=True)

    white: Mapped[str | None] = mapped_column(String(64), nullable=True)
    black: Mapped[str | None] = mapped_column(String(64), nullable=True)
    white_elo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    black_elo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opponent_elo: Mapped[int | None] = mapped_column(Integer, nullable=True)

    played_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ply_count: Mapped[int] = mapped_column(Integer, default=0)
    pgn: Mapped[str] = mapped_column(Text)

    analyzed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deep_analyzed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    moves: Mapped[list[Move]] = relationship(back_populates="game", cascade="all, delete-orphan")


class Move(Base):
    __tablename__ = "moves"
    __table_args__ = (UniqueConstraint("game_id", "ply", name="uq_game_ply"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    ply: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(5))
    is_user_move: Mapped[bool] = mapped_column(Boolean, index=True)

    san: Mapped[str] = mapped_column(String(8))
    uci: Mapped[str] = mapped_column(String(6))
    fen_before: Mapped[str] = mapped_column(String(96))
    clock_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Filled by analysis (annotate.py).
    eval_before_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eval_after_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_move_uci: Mapped[str | None] = mapped_column(String(6), nullable=True)
    best_line: Mapped[str | None] = mapped_column(Text, nullable=True)     # space-sep UCI
    # What actually punishes the move that was played: the engine's PV from the
    # position *after* it. "What would have happened to me" is the question a
    # learner has, and `best_line` (the line after the move they should have
    # played) does not answer it.
    refutation_line: Mapped[str | None] = mapped_column(Text, nullable=True)  # space-sep UCI
    second_best_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cp_loss: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mate_missed: Mapped[bool] = mapped_column(Boolean, default=False)
    mate_allowed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Filled by classify.py.
    forcingness: Mapped[float | None] = mapped_column(Float, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(12), nullable=True)          # opening/middlegame/endgame
    pillar: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # opening/tactics/positional/endgame/quiet
    error_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)   # ok/inaccuracy/mistake/blunder
    motif: Mapped[str | None] = mapped_column(String(64), nullable=True)          # comma-sep tactical motifs
    endgame_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    context: Mapped[str | None] = mapped_column(String(12), nullable=True)        # attacking/defending/balanced
    punish_miss: Mapped[bool] = mapped_column(Boolean, default=False)             # failed to punish opp's prior error
    time_trouble: Mapped[bool] = mapped_column(Boolean, default=False)
    # Wall-clock seconds spent on this move, derived from the clock comments in
    # the PGN (previous clock of the same side, minus this one, plus increment).
    seconds_spent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How much genuine choice the position offered, 0..1. A forced recapture
    # contributes a cp_loss of 0 that dilutes every average it lands in; this
    # weight lets the profile average over decisions rather than over moves.
    decision_w: Mapped[float | None] = mapped_column(Float, nullable=True)

    game: Mapped[Game] = relationship(back_populates="moves")


class Puzzle(Base):
    __tablename__ = "puzzles"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    ply: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "own-game" | "library" | "endgame" | "repertoire"
    source: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    fen: Mapped[str] = mapped_column(String(96))
    solution: Mapped[str] = mapped_column(Text)              # space-sep UCI, full line
    pillar: Mapped[str] = mapped_column(String(16), index=True)
    motif: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="find-best")  # find-best | punish | defend
    rating_est: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How much better the best move is than the second best, in cp. A puzzle is
    # only fair to mark "wrong" when this is large - otherwise the solver may
    # have found an equally good move. None means "not measured" (library set).
    uniqueness_cp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The line that punishes the move originally played here, so a failed attempt
    # can show what would have happened rather than only naming the right move.
    refutation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Time control the mistake originally happened at, so drills can target the
    # speed the weakness actually shows up in.
    speed: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # Drill sets only: a human name ("Lucena position", "Caro-Kann as black") and,
    # for endgames, the result to be preserved ("win" | "draw") - endgame drills
    # are judged on holding the result, not on matching one stored line.
    name: Mapped[str | None] = mapped_column(String(96), nullable=True)
    goal: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # True when the stored line opens with the *opponent's* move, so the solver
    # plays it before handing the board over. Used by Lichess-style puzzles and by
    # black repertoire lines, which necessarily start with White's first move.
    opponent_first: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    srs: Mapped[SrsCard | None] = relationship(back_populates="puzzle", uselist=False,
                                                 cascade="all, delete-orphan")


class SrsCard(Base):
    __tablename__ = "srs_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    puzzle_id: Mapped[int] = mapped_column(ForeignKey("puzzles.id", ondelete="CASCADE"), unique=True)
    stability: Mapped[float] = mapped_column(Float, default=0.0)
    difficulty: Mapped[float] = mapped_column(Float, default=5.0)
    due_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    reps: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    last_grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_review: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Milliseconds the last solve took, and a running mean, so the scheduler can
    # grade on outcome + speed rather than asking the user to introspect.
    last_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    wins: Mapped[int] = mapped_column(Integer, default=0)

    puzzle: Mapped[Puzzle] = relationship(back_populates="srs")


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("source", "username", name="uq_sync"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    username: Mapped[str] = mapped_column(String(64))
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_cursor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProfileSnapshot(Base):
    __tablename__ = "profile_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    account: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)  # JSON


class Setting(Base):
    """Tiny key/value store for adaptive state (running sparring Elo, rotation
    counters, toggles set from the UI)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def get_setting(session, key: str, default: str | None = None) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


class TrainingSession(Base):
    """One day's guided hour.

    The plan is generated once from the weakness profile and then stored, so the
    session survives a reload, a laptop lid, or moving from desk to phone: the
    app reopens on the block you were in rather than asking you to choose again.
    """

    __tablename__ = "training_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # JSON: the ordered list of blocks with their budgets and targets.
    plan: Mapped[str] = mapped_column(Text)
    block_index: Mapped[int] = mapped_column(Integer, default=0)
    # JSON: per-block progress (puzzles solved, game ids played, seconds spent).
    progress: Mapped[str] = mapped_column(Text, default="{}")
    focus_pillar: Mapped[str | None] = mapped_column(String(16), nullable=True)
    speed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    elapsed_s: Mapped[int] = mapped_column(Integer, default=0)
    # When the *current* block was entered. Kept server-side so the block's
    # budget survives a reload or a move from desk to phone - a clock the browser
    # owned would restart every time the page did.
    block_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def make_engine(db_path: Path | None = None):
    """Build an engine with the pragmas we want on every connection.

    WAL lets the background Stockfish pass write while the UI reads, instead of
    the two blocking each other; ``busy_timeout`` makes the rare genuine
    contention wait rather than raise "database is locked".
    """
    path = db_path or DB_PATH
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def _migrate(engine) -> None:
    """Add columns this build expects but an older database file lacks.

    SQLAlchemy's ``create_all`` only creates missing *tables*, so a schema that
    grew a column silently keeps working until something reads it. SQLite's
    ``ALTER TABLE ... ADD COLUMN`` is cheap and non-destructive, which is all we
    need for a local single-user database.

    ``ADD COLUMN`` cannot carry the column's index with it, so indexes are
    created in a second pass: a column declared ``index=True`` and then added to
    an existing table would otherwise never get one.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                ddl = col.type.compile(engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        default = f" DEFAULT {int(val)}"
                    elif isinstance(val, (int, float)):
                        default = f" DEFAULT {val}"
                    elif isinstance(val, str):
                        default = f" DEFAULT '{val}'"
                conn.execute(text(
                    f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{default}'
                ))
                added.append(f"{table.name}.{col.name}")

    if not added:
        return
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have_idx = {i["name"] for i in inspector.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name in have_idx:
                    continue
                cols = ", ".join(c.name for c in idx.columns)
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {idx.name} ON {table.name} ({cols})"
                ))


def init_db(db_path: Path | None = None):
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


# One engine + one sessionmaker per database path, built on first use. Rebuilding
# these per request meant a fresh connection pool and a full `create_all` on every
# single API call.
_FACTORIES: dict[str, sessionmaker] = {}
_FACTORY_LOCK = threading.Lock()


def session_factory(db_path: Path | None = None) -> sessionmaker:
    key = str(db_path or DB_PATH)
    factory = _FACTORIES.get(key)
    if factory is not None:
        return factory
    with _FACTORY_LOCK:
        factory = _FACTORIES.get(key)
        if factory is None:
            factory = sessionmaker(
                bind=init_db(db_path), expire_on_commit=False, future=True
            )
            _FACTORIES[key] = factory
        return factory
