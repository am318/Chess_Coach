"""The scoring rules the whole curriculum hangs off.

`ranked_weaknesses[0]` decides what the daily session trains, so how it is
computed deserves tests that state the intent rather than the arithmetic.
"""

import chess

from coach.analysis.classify import (
    classify_move,
    decision_weight,
    tactical_tension,
)
from coach.config import ProfileCfg
from coach.profile import weakness_model as wm

CFG = ProfileCfg()


# ------------------------------------------------------------- decision weight

def test_forced_move_carries_no_decision_weight():
    """King must move out of a single check: not evidence of anything."""
    only = chess.Board("7k/8/8/8/8/8/6q1/7K w - - 0 1")   # Kxg2 or nothing
    assert only.legal_moves.count() == 1
    assert decision_weight(only) == 0.0
    assert decision_weight(chess.Board()) == 1.0    # 20 legal moves


def test_summ_averages_over_decisions_not_moves():
    """Ten forced moves at 0cp should not dilute one real 200cp blunder."""
    real = [(200, 1.0)]
    padded = real + [(0, 0.0)] * 10          # decision weight 0 on the forced ones
    assert wm._summ(padded)["avg_cp_loss"] == wm._summ(real)["avg_cp_loss"] == 200.0


# ----------------------------------------------------------- pillar routing

def test_hanging_piece_in_the_opening_is_tactics_not_openings():
    """A bishop en prise on move four is a tactical problem that happens to be
    early. Filing it under Openings sends the player to study theory for a
    problem theory does not fix.
    """
    # ...d5 attacks the undefended Bc4.
    board = chess.Board("r1bqkbnr/ppp2ppp/2n5/3pp3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4")
    assert tactical_tension(board)
    tags = classify_move(board, chess.Move.from_uci("d2d3"),
                         chess.Move.from_uci("e4d5"), 300, 40, 30, CFG)
    assert tags.phase == "opening"           # phase is still recorded honestly
    assert tags.pillar == "tactics"


def test_a_calm_opening_position_is_not_called_tactical():
    """The tension test has to be able to say no, or the tactics pillar just
    absorbs everything and stops meaning anything."""
    assert not tactical_tension(chess.Board())
    # The quiet Italian: pieces developed, nothing en prise.
    calm = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 4 4")
    assert not tactical_tension(calm)


def test_pillar_never_depends_on_the_size_of_the_error():
    """The anti-circularity rule the whole ranking rests on.

    If blunders routed themselves into one pillar, that pillar's average cp-loss
    and blunder rate would be conditioned on the outcome, the other pillars'
    blunder rates would be zero by construction, and the cross-pillar comparison
    that `severity` performs would be meaningless.
    """
    board = chess.Board("rnbqkb1r/pppp1ppp/5n2/4p3/4P3/2N5/PPPP1PPP/R1BQKBNR w KQkq - 2 3")
    played, best = chess.Move.from_uci("g1f3"), chess.Move.from_uci("g1f3")
    pillars = {
        classify_move(board, played, best, cp_loss, 30, 20, CFG).pillar
        for cp_loss in (0, 60, 150, 400, 2000)
    }
    assert len(pillars) == 1, f"routing moved with the error size: {pillars}"


def test_endgame_keeps_its_own_sharp_positions():
    """A rook ending wants a technique drill, not more tactics puzzles, even when
    the position is forcing."""
    board = chess.Board("8/5k2/8/8/3K4/8/4P3/4R3 w - - 0 40")
    tags = classify_move(board, chess.Move.from_uci("e1a1"), chess.Move.from_uci("d4d5"),
                         300, 500, 40, CFG)
    assert tags.phase == "endgame" and tags.pillar == "endgame"


def test_tension_ignores_a_defended_piece():
    """Attacked but adequately defended is a normal position, not a tactic."""
    # Nc6 attacked by the b5 bishop, defended by the b7 and d7 pawns.
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3")
    assert not tactical_tension(board)


def test_quiet_opening_move_still_routes_to_openings():
    board = chess.Board()
    tags = classify_move(board, chess.Move.from_uci("a2a3"), chess.Move.from_uci("e2e4"),
                         40, 30, 25, CFG)
    assert tags.pillar == "opening"


# ----------------------------------------------------------------- severity

def _pillar(total_cost, avg, n=400, blunder=0.05, weight=None):
    return {"total_cp_cost": total_cost, "avg_cp_loss": avg, "n": n,
            "blunder_rate": blunder, "trend_cp_loss": 0.0,
            "weight": n if weight is None else weight}


def _rank(pillars):
    """Mirror of the ranking arithmetic in build_profile, over synthetic pillars."""
    import math
    total_cost = sum(p["total_cp_cost"] for p in pillars.values()) or 1
    total_rate = sum(p["avg_cp_loss"] for p in pillars.values()) or 1.0
    out = []
    for name, s in pillars.items():
        cost_share = s["total_cp_cost"] / total_cost
        rate_share = s["avg_cp_loss"] / total_rate
        sev = math.sqrt(cost_share * rate_share) * (1 + s["blunder_rate"])
        out.append((name, sev))
    out.sort(key=lambda t: -t[1])
    return [n for n, _ in out]


def test_severity_does_not_let_volume_bury_a_sharp_leak():
    """A pillar seen ten times less often, but three times worse per decision,
    must be able to outrank the bulky one."""
    pillars = {
        "positional": _pillar(total_cost=100_000, avg=20.0, n=5000),
        "endgame": _pillar(total_cost=45_000, avg=90.0, n=500),
    }
    assert _rank(pillars)[0] == "endgame"


def test_severity_still_respects_sheer_cost():
    """Rate alone shouldn't win either: a rare pillar that is only slightly worse
    should not displace one doing most of the damage."""
    pillars = {
        "tactics": _pillar(total_cost=100_000, avg=60.0, n=3000),
        "endgame": _pillar(total_cost=3_000, avg=70.0, n=60),
    }
    assert _rank(pillars)[0] == "tactics"


# ---------------------------------------------------------- time allocation

def _row(cp_loss, seconds, critical, weight=1.0):
    return wm._Row(cp_loss=cp_loss, bucket="ok", pillar="tactics", motif=None,
                   endgame_type=None, eco=None, opening=None, color="white",
                   time_trouble=False, context="balanced", punish_miss=False,
                   half="recent", speed="blitz", weight=weight, decision_w=1.0,
                   critical=critical, seconds=seconds)


def test_time_allocation_reports_the_ratio_it_can_actually_defend():
    """The ratio compares two kinds of *position*, so nothing the player decides
    after seeing the board can bias it. That is the finding."""
    rows = ([_row(180, 2.0, True) for _ in range(15)]      # rushed criticals
            + [_row(40, 20.0, True) for _ in range(15)]    # considered criticals
            + [_row(30, 12.0, False) for _ in range(30)])  # quiet, slow anyway
    ta = wm._time_allocation(rows)
    assert ta["rushed_critical_rate"] == 0.5
    # 11s average on critical vs 12s on quiet: less time where it matters.
    assert ta["allocation_ratio"] < 1.0


def test_time_allocation_refuses_to_call_the_rushed_comparison_a_cost():
    """Time spent is chosen by the player, and they spend it on positions they
    can already see are hard - so cp-loss by time-spent runs backwards. Real data
    has the considered moves scoring worse. The number may be reported; it may
    not be reported as a cost."""
    rows = ([_row(100, 2.0, True) for _ in range(15)]
            + [_row(180, 20.0, True) for _ in range(15)]
            + [_row(30, 12.0, False) for _ in range(30)])
    ta = wm._time_allocation(rows)
    assert "cost_of_rushing" not in ta
    assert ta["rushed_vs_considered_cp"] == -80.0
    assert "confounded" in ta


def test_time_allocation_declines_to_guess_without_data():
    assert "note" in wm._time_allocation([_row(50, 3.0, True)])


# ------------------------------------------------------------- snapshots

def test_snapshots_are_one_per_day_and_prunable(tmp_path):
    """The trend chart assumes a daily-ish cadence, but `build_profile` runs on
    every app open - four launches in an evening drew four points and stored four
    copies of the whole profile."""
    import json
    from datetime import UTC, datetime, timedelta

    from coach.store.db import ProfileSnapshot, session_factory

    db = tmp_path / "snap.db"
    Session = session_factory(db)
    now = datetime.now(UTC).replace(tzinfo=None)   # SQLite stores naive UTC
    yesterday = now - timedelta(days=1)
    with Session() as s:
        # Three from one earlier day, written before the dedupe existed.
        for offset in (0, 3, 7):
            s.add(ProfileSnapshot(account="all", payload="{}",
                                  created_at=yesterday + timedelta(hours=offset)))
        s.add(ProfileSnapshot(account="all", payload="{}", created_at=now))
        s.commit()
        assert s.query(ProfileSnapshot).count() == 4

    assert wm.prune_snapshots(db) == 2          # keeps the last of each day
    with Session() as s:
        assert s.query(ProfileSnapshot).count() == 2

    # And a fresh save replaces today's rather than appending to it.
    with Session() as s:
        wm._save_snapshot(s, None, {"n_moves": 7})
        s.commit()
        rows = s.query(ProfileSnapshot).all()
        assert len(rows) == 2, "today's snapshot should have been replaced, not added"
        today_rows = [r for r in rows if r.created_at.date() == now.date()]
        assert len(today_rows) == 1
        assert json.loads(today_rows[0].payload)["n_moves"] == 7
