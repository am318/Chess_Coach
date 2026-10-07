"""Sequential Probability Ratio Test for engine A/B testing.

Trinomial (win/draw/loss) model with the standard log-likelihood-ratio using the
BayesElo-style pentanomial simplification dropped for clarity. Good enough to
gate "is build B stronger than build A" decisions during development.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _elo_to_score(elo: float) -> float:
    return 1.0 / (1.0 + 10 ** (-elo / 400.0))


@dataclass
class SPRTResult:
    llr: float
    lower_bound: float
    upper_bound: float
    decision: str  # "accept-H1" | "accept-H0" | "continue"
    elo: float
    elo_ci95: tuple[float, float]
    wins: int
    draws: int
    losses: int


def sprt(
    wins: int,
    draws: int,
    losses: int,
    elo0: float = 0.0,
    elo1: float = 10.0,
    alpha: float = 0.05,
    beta: float = 0.05,
) -> SPRTResult:
    n = wins + draws + losses
    lower = math.log(beta / (1.0 - alpha))
    upper = math.log((1.0 - beta) / alpha)

    if n == 0:
        return SPRTResult(0.0, lower, upper, "continue", 0.0, (0.0, 0.0), 0, 0, 0)

    w, d, ll = wins / n, draws / n, losses / n
    s0, s1 = _elo_to_score(elo0), _elo_to_score(elo1)

    # Model draws as fixed; split the remaining probability by the hypothesis score.
    def probs(s: float) -> tuple[float, float, float]:
        pw = max(1e-9, s - d / 2.0)
        pl = max(1e-9, (1.0 - s) - d / 2.0)
        pd = max(1e-9, 1.0 - pw - pl)
        tot = pw + pd + pl
        return pw / tot, pd / tot, pl / tot

    pw0, pd0, pl0 = probs(s0)
    pw1, pd1, pl1 = probs(s1)
    llr = (
        wins * math.log(pw1 / pw0)
        + draws * math.log(pd1 / pd0)
        + losses * math.log(pl1 / pl0)
    )

    score = w + d / 2.0
    score = min(max(score, 1e-6), 1 - 1e-6)
    elo = -400.0 * math.log10(1.0 / score - 1.0)
    # Wald CI on the score, mapped to Elo.
    var = (w * (1 - score) ** 2 + d * (0.5 - score) ** 2 + ll * (0 - score) ** 2) / n
    se = math.sqrt(max(var, 1e-12))
    lo_s = min(max(score - 1.96 * se, 1e-6), 1 - 1e-6)
    hi_s = min(max(score + 1.96 * se, 1e-6), 1 - 1e-6)
    elo_lo = -400.0 * math.log10(1.0 / lo_s - 1.0)
    elo_hi = -400.0 * math.log10(1.0 / hi_s - 1.0)

    decision = "continue"
    if llr >= upper:
        decision = "accept-H1"
    elif llr <= lower:
        decision = "accept-H0"

    return SPRTResult(llr, lower, upper, decision, elo, (elo_lo, elo_hi), wins, draws, losses)
