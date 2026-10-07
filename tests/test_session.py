"""Daily-session planning: time-control choice and block structure."""

from coach.session.plan import _second_focus
from coach.session.timecontrol import (
    TIME_CONTROLS,
    plan_game_block,
    unplayed_speeds,
)


def test_fast_pillars_get_short_games_and_slow_pillars_get_long_ones():
    tactics = plan_game_block("tactics", minutes=25)
    quiet = plan_game_block("quiet", minutes=25)
    assert tactics.time_control.speed == "blitz"
    assert tactics.games >= 2, "tactics wants repetitions, not one long game"
    assert quiet.time_control.est_minutes > tactics.time_control.est_minutes
    assert quiet.games == 1


def test_endgame_block_always_has_an_increment():
    plan = plan_game_block("endgame", minutes=25)
    assert plan.time_control.inc_s > 0


def test_block_respects_its_minute_budget():
    for pillar in ("tactics", "quiet", "endgame", "positional", "opening"):
        plan = plan_game_block(pillar, minutes=25)
        assert plan.minutes <= plan.games * plan.time_control.est_minutes
        assert plan.games >= 1


def test_tiny_budget_still_returns_one_game():
    plan = plan_game_block("quiet", minutes=3)
    assert plan.games == 1
    assert plan.time_control.key in TIME_CONTROLS


def test_profile_steers_to_the_speed_where_the_pillar_actually_hurts():
    profile = {
        "speeds": {
            "blitz": {"n": 900, "pillars": {"tactics": {"n": 300, "avg_cp_loss": 130.0}}},
            "rapid": {"n": 900, "pillars": {"tactics": {"n": 300, "avg_cp_loss": 40.0}}},
        }
    }
    plan = plan_game_block("tactics", minutes=25, profile=profile)
    assert plan.time_control.speed == "blitz"
    assert "blitz" in plan.speed_note

    flipped = {
        "speeds": {
            "blitz": {"n": 900, "pillars": {"tactics": {"n": 300, "avg_cp_loss": 40.0}}},
            "rapid": {"n": 900, "pillars": {"tactics": {"n": 300, "avg_cp_loss": 130.0}}},
        }
    }
    plan2 = plan_game_block("tactics", minutes=25, profile=flipped)
    assert plan2.time_control.speed == "rapid", "should train where the pillar is worse"


def test_no_speed_override_without_enough_evidence():
    thin = {"speeds": {"blitz": {"n": 900, "pillars": {"tactics": {"n": 4, "avg_cp_loss": 200.0}}}}}
    assert plan_game_block("tactics", minutes=25, profile=thin).speed_note == ""


def test_unplayed_speeds_flags_a_blitz_only_history():
    profile = {"speeds": {"blitz": {"n": 4000, "pillars": {}}}}
    missing = unplayed_speeds(profile)
    assert "rapid" in missing and "classical" in missing
    assert "blitz" not in missing


def test_drill_covers_a_weakness_the_game_block_cannot_reach():
    ranked = ["tactics", "endgame", "quiet", "positional", "opening"]
    # A blitz game can't train slow pillars, so the drill should pick one.
    assert _second_focus(ranked, "tactics", "blitz") in {"endgame", "quiet", "positional"}
    # A long game gives few tactical reps, so the drill should supply them.
    assert _second_focus(["quiet", "tactics", "endgame"], "quiet", "rapid") in {
        "tactics", "opening"
    }


def test_drill_never_repeats_the_game_block_pillar():
    ranked = ["tactics", "endgame"]
    assert _second_focus(ranked, "tactics", "blitz") != "tactics"
    assert _second_focus(ranked, "endgame", "rapid") != "endgame"


def test_breadth_is_not_pushed_on_an_already_scattered_repertoire():
    """A player spread over ~28 openings needs consolidation, not more variety.

    ``_repertoire_concentration`` returns a Herfindahl index per colour, so a
    very low value means a very broad repertoire.
    """
    from coach.sparring import opening_picker as op

    assert op._breadth_probability(0.60) == 0.5    # over-narrow -> force variety
    assert op._breadth_probability(0.036) == 0.0   # ~28 effective openings -> none
    assert op._breadth_probability(0.20) == 0.25   # healthy -> occasional variety


def test_the_week_spans_time_controls_not_just_blitz():
    """A permanently top-ranked fast pillar must not schedule blitz seven days a
    week, or the slow pillars never get a clock long enough to train them."""
    from unittest.mock import patch

    import coach.session.plan as sp

    speeds, controls = set(), set()
    for day in range(8):
        with patch.object(sp, "_rotation", lambda d=day: d):
            plan = sp.build_plan(None)
        speeds.add(plan["speed"])
        controls.add(plan["time_control"])
        assert sum(b["minutes"] for b in plan["blocks"]) == sp.DEFAULT_MINUTES
    assert "rapid" in speeds, "a week with no long game trains no slow pillar"
    assert len(controls) >= 3, "the week should vary the clock"


# ------------------------------------------------------- the block time budget


def _temp_plan_module(monkeypatch, tmp_path):
    """Point plan.py's session factory at a throwaway database."""
    from coach.session import plan as sp
    from coach.store.db import session_factory

    factory = session_factory(tmp_path / "session.db")
    monkeypatch.setattr(sp, "session_factory", lambda *a, **k: factory)
    return sp


def test_a_block_reports_its_budget_and_what_is_left_of_it(monkeypatch, tmp_path):
    sp = _temp_plan_module(monkeypatch, tmp_path)
    started = sp.start_session(None, minutes=60)

    block = started["block"]
    assert started["block_budget_s"] == block["minutes"] * 60
    # Just started, so essentially all of it remains.
    assert started["block_elapsed_s"] <= 2
    assert started["block_seconds_left"] > block["minutes"] * 60 - 5
    assert started["session_budget_s"] == started["plan"]["minutes"] * 60


def test_advancing_charges_the_time_spent_and_restarts_the_block_clock(
    monkeypatch, tmp_path
):
    """The hour has to be measured, not just labelled: each block records the
    seconds it actually took, and the next one starts from zero."""
    from datetime import timedelta

    sp = _temp_plan_module(monkeypatch, tmp_path)
    started = sp.start_session(None, minutes=60)

    # Pretend the warm-up took four minutes.
    real_now = sp._now
    monkeypatch.setattr(sp, "_now", lambda: real_now() + timedelta(minutes=4))
    after = sp.advance(started["id"], {"puzzles": 7})

    assert after["block_index"] == 1
    spent = after["progress"]["0"]["seconds"]
    assert 235 <= spent <= 245, spent
    assert after["progress"]["0"]["puzzles"] == 7
    # The new block's clock starts now, not when the session did.
    assert after["block_elapsed_s"] <= 2
    assert after["block_seconds_left"] > after["block"]["minutes"] * 60 - 5


def test_note_progress_cannot_overwrite_the_session_clock(monkeypatch, tmp_path):
    sp = _temp_plan_module(monkeypatch, tmp_path)
    started = sp.start_session(None, minutes=60)
    noted = sp.note_progress(started["id"], {"puzzles": 3, "elapsed_s": 99999})
    assert noted["elapsed_s"] == 0
    assert noted["progress"]["0"]["puzzles"] == 3


def test_warmup_puzzle_count_is_a_backstop_not_a_target(monkeypatch, tmp_path):
    """Two puzzles a minute made an eight-minute warm-up demand sixteen of them.
    The block is bounded by its minutes now, so the count only has to stop a run
    of one-movers - it must sit above what the minutes can hold, without being a
    number nobody can reach.

    It used to be ``minutes * 4``: fifteen seconds a puzzle, against a measured
    median nearer thirty-five, so the counter read "1/32" through a block that
    correctly ended after about a dozen. The cap is sized from the solve times
    actually recorded now, so the bound here is expressed the same way.
    """
    sp = _temp_plan_module(monkeypatch, tmp_path)
    plan = sp.build_plan(None, minutes=60)
    warmup = next(b for b in plan["blocks"] if b["kind"] == "warmup")
    cap = warmup["params"]["max_puzzles"]
    fits = warmup["minutes"] * 60 / sp.DEFAULT_SOLVE_SECONDS
    assert cap > fits, "the count must not bind before the clock does"
    assert cap <= fits * 2, f"{cap} is a target nobody reaches in {warmup['minutes']} minutes"


def test_game_block_carries_the_planner_s_own_per_game_estimate(monkeypatch, tmp_path):
    """The client needs it to answer "is there room for another game?" with the
    same number that decided how many to schedule."""
    from coach.session.timecontrol import TIME_CONTROLS

    sp = _temp_plan_module(monkeypatch, tmp_path)
    plan = sp.build_plan(None, minutes=60)
    game = next(b for b in plan["blocks"] if b["kind"] == "game")
    tc = TIME_CONTROLS[game["params"]["time_control"]]
    assert game["params"]["est_minutes"] == tc.est_minutes


# --------------------------------------------- steering, pace and re-budgeting


def test_the_sparring_bucket_is_not_a_time_control():
    """The profile keeps a "sparring" bucket alongside blitz/rapid/classical.

    Treating it as a speed answers "am I worse against humans than against a bot
    pitched below me" - always yes - and then steers the session on it. For a
    player whose whole online history is blitz those were the only two buckets,
    so every day came back blitz and the slow pillars never got a clock.
    """
    from coach.session.timecontrol import _worst_speed_for_pillar, plan_game_block

    profile = {
        "speeds": {
            "blitz": {"n": 900, "pillars": {"endgame": {"n": 400, "avg_cp_loss": 79.0}}},
            "sparring": {"n": 260, "pillars": {"endgame": {"n": 40, "avg_cp_loss": 20.0}}},
        }
    }
    assert _worst_speed_for_pillar(profile, "endgame") == ("blitz", 0.0)
    # ...so the endgame block keeps the clock its pillar actually needs.
    assert plan_game_block("endgame", 25, profile).time_control.speed == "rapid"


def test_game_length_comes_from_the_players_own_clock_data():
    """A generic table said a 5+0 takes eleven minutes; this player finishes one
    in six and a half, so the block was scheduling two games for twenty-two
    minutes and then had nothing to do with the rest."""
    from coach.session.timecontrol import TIME_CONTROLS, observed_minutes, plan_game_block

    observed = {"5+0": 6.4, "_pace": 0.58}
    assert observed_minutes(TIME_CONTROLS["5+0"], observed) == 6
    # A control with no history of its own still gets their pace applied.
    assert observed_minutes(TIME_CONTROLS["10+5"], observed) == 13
    assert observed_minutes(TIME_CONTROLS["10+5"], None) == 23

    plan = plan_game_block("tactics", 22, observed={"3+2": 4.2, "_pace": 0.55})
    assert plan.est_minutes == 4
    assert plan.games >= 4, "four-minute games leave room for more than two"


def test_pace_correction_is_bounded():
    """One freak game must not turn the block into a single game an hour, nor
    into twenty of them."""
    from coach.session.timecontrol import PACE_MAX, PACE_MIN, TIME_CONTROLS, observed_minutes

    tc = TIME_CONTROLS["10+5"]
    assert observed_minutes(tc, {"_pace": 0.01}) == round(tc.est_minutes * PACE_MIN)
    assert observed_minutes(tc, {"_pace": 9.0}) == round(tc.est_minutes * PACE_MAX)


def test_time_control_keys_are_parsed_from_pgn_headers():
    from coach.session.plan import _tc_key

    assert _tc_key("300") == "5+0"
    assert _tc_key("300+3") == "5+3"
    assert _tc_key("180+2") == "3+2"
    assert _tc_key(None) is None
    assert _tc_key("-") is None


def test_a_block_that_ends_early_hands_its_minutes_on(monkeypatch, tmp_path):
    """The hour is what was asked for. Two games finished in six minutes used to
    vacate a twenty-six-minute slot and end the session forty minutes short."""
    sp = _temp_plan_module(monkeypatch, tmp_path)
    plan = sp.build_plan(None, minutes=60)
    game_idx = next(i for i, b in enumerate(plan["blocks"]) if b["kind"] == "game")
    before = [b["minutes"] for b in plan["blocks"]]

    # The game block was budgeted N minutes and took six.
    assert sp.rebudget(plan, game_idx, spent_s=6 * 60)
    after = [b["minutes"] for b in plan["blocks"]]

    assert after[:game_idx + 1] == before[:game_idx + 1], "settled blocks are left alone"
    later = [i for i in range(game_idx + 1, len(plan["blocks"]))
             if plan["blocks"][i]["kind"] != "close"]
    assert sum(after[i] for i in later) > sum(before[i] for i in later)
    # The closing summary is not training and does not absorb the surplus.
    close = next(i for i, b in enumerate(plan["blocks"]) if b["kind"] == "close")
    assert after[close] == before[close]


def test_rebudgeting_shortens_what_follows_an_overrun(monkeypatch, tmp_path):
    sp = _temp_plan_module(monkeypatch, tmp_path)
    plan = sp.build_plan(None, minutes=60)
    idx = next(i for i, b in enumerate(plan["blocks"]) if b["kind"] == "game")
    drill = next(i for i, b in enumerate(plan["blocks"]) if b["kind"] == "drill")
    before = plan["blocks"][drill]["minutes"]

    sp.rebudget(plan, idx, spent_s=(plan["blocks"][idx]["minutes"] + 15) * 60)
    assert plan["blocks"][drill]["minutes"] < before


def test_no_block_balloons_however_much_time_is_handed_on(monkeypatch, tmp_path):
    """Skipping the warm-up in ten seconds must not triple everything after it."""
    sp = _temp_plan_module(monkeypatch, tmp_path)
    plan = sp.build_plan(None, minutes=60)
    planned = [b["minutes"] for b in plan["blocks"]]
    sp.rebudget(plan, 0, spent_s=10)
    for i, b in enumerate(plan["blocks"]):
        assert b["minutes"] <= max(sp.MIN_BLOCK_MINUTES, planned[i] * sp.REBUDGET_CAP) + 1


# --------------------------------------------- the game block's own bookkeeping


def test_the_games_a_block_played_survive_skipping_out_of_it(monkeypatch, tmp_path):
    """The ids used to live in the browser until the block ended by itself, so
    skipping out of it - or reloading, or closing the lid - took the games with
    them and the review block that followed had nothing to review. They are
    written through as each game ends now, and `advance` merges rather than
    replaces, so a skip keeps them."""
    sp = _temp_plan_module(monkeypatch, tmp_path)
    started = sp.start_session(None, minutes=60)
    sp.advance(started["id"], {"skipped": True})              # past the warm-up

    sp.note_progress(started["id"], {"game_ids": [41], "games": 1, "game_secs": [360]})
    mid = sp.note_progress(started["id"], {"game_ids": [41, 42], "games": 2,
                                           "game_secs": [360, 300]})
    assert mid["progress"]["1"]["game_ids"] == [41, 42]

    after = sp.advance(started["id"], {"skipped": True})
    block = after["progress"]["1"]
    assert block["game_ids"] == [41, 42], "a skip must not discard the games played"
    assert block["skipped"] is True and block["seconds"] >= 0


def test_a_reload_can_pick_the_block_up_where_it_left_off(monkeypatch, tmp_path):
    """What the front end needs to carry on counting after a reload: the ids,
    the count, and how long each game actually took - which is what decides
    whether the block has room for another."""
    sp = _temp_plan_module(monkeypatch, tmp_path)
    started = sp.start_session(None, minutes=60)
    sp.advance(started["id"], {"skipped": True})
    sp.note_progress(started["id"], {"game_ids": [7], "games": 1, "game_secs": [400]})

    resumed = sp.as_dict(sp.current_session(None))
    block = resumed["progress"][str(resumed["block_index"])]
    assert block["game_ids"] == [7] and block["game_secs"] == [400]


def test_a_speed_override_does_not_contradict_the_pillars_usual_clock():
    from coach.session.plan import _game_detail
    from coach.session.timecontrol import plan_game_block

    profile = {"speeds": {
        "blitz": {"n": 900, "pillars": {"positional": {"n": 300, "avg_cp_loss": 130.0}}},
        "rapid": {"n": 900, "pillars": {"positional": {"n": 300, "avg_cp_loss": 40.0}}},
    }}
    overridden = _game_detail(plan_game_block("positional", minutes=25, profile=profile))
    assert overridden.endswith("today is blitz") and "long clock" not in overridden
    assert _game_detail(plan_game_block("positional", minutes=25)).startswith("a long clock")
