"""In-game situations: the move after winning material, after a mistake, etc."""

from datetime import UTC, datetime

from coach.profile import weakness_model as wm
from coach.session.plan import remember
from coach.store.db import Game, Move


def _game(color="white"):
    return Game(id=1, source="lichess", source_id="x", account="me", color=color,
                result="win", pgn="", played_at=datetime.now(UTC).replace(tzinfo=None))


def _move(ply, eval_cp=0, bucket="ok", secs=None):
    return Move(game_id=1, ply=ply, side="white", is_user_move=True, san="e4", uci="e2e4",
                fen_before="", eval_before_cp=eval_cp, error_bucket=bucket, seconds_spent=secs)


def test_states_are_read_from_before_the_move():
    g = _game()
    prev = _move(1, eval_cp=0, bucket="blunder", secs=90)
    cur = _move(3, eval_cp=250)
    st = wm._states(cur, g, prev, median_s=6.0)
    assert set(st) == {"after_gaining", "after_own_error", "after_long_think"}

    assert "winning" in wm._states(_move(5, eval_cp=400), g, None, None)
    # Black's point of view is flipped.
    assert "losing" in wm._states(_move(5, eval_cp=400), _game("black"), None, None)
    # Already winning doesn't count as "just gained".
    assert "after_gaining" not in wm._states(_move(5, eval_cp=900), g, _move(3, eval_cp=500), None)
    # A long think is relative to the player's own pace in that game.
    assert "after_long_think" not in wm._states(cur, g, _move(1, secs=40), median_s=30.0)


def _row(cp, states=()):
    return wm._Row(cp_loss=cp, bucket="ok", pillar="tactics", motif=None, endgame_type=None,
                   eco=None, opening=None, color="white", time_trouble=False,
                   context="balanced", punish_miss=False, half="recent", speed="blitz",
                   weight=1.0, decision_w=1.0, critical=False, seconds=None, states=states)


def test_the_finding_is_the_situation_that_breaks_the_player():
    rows = [_row(20) for _ in range(180)] + [_row(400) for _ in range(20)]
    rows += [_row(400, ("after_gaining",)) for _ in range(8)]
    rows += [_row(20, ("after_gaining",)) for _ in range(12)]
    gs = wm._game_state(rows, wm._summ)
    st = gs["states"]["after_gaining"]
    assert st["blunder_ratio"] > 3
    assert gs["finding"]["state"] == "after_gaining"
    assert "relax" in gs["finding"]["advice"]


def test_too_little_evidence_is_not_a_finding():
    rows = [_row(20) for _ in range(100)] + [_row(400, ("after_own_error",)) for _ in range(3)]
    assert wm._game_state(rows, wm._summ)["finding"] is None


def test_close_block_remembers_the_habit_first():
    prof = {"game_state": {"finding": {"blunder_ratio": 2.4, "label": "the move after a long think",
                                       "advice": "Don't snap.", "state": "after_long_think"}},
            "tactics_motifs": [{"key": "fork"}]}
    assert "2.4x" in remember(prof) and "long think" in remember(prof)
    assert remember({"tactics_motifs": [{"key": "fork"}]}).endswith("forks two pieces.")
    assert remember({}) is None
